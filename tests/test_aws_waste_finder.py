"""End-to-end tests for aws_waste_finder.py against a real AWS API emulator.

moto's standalone server (installed by ci/setup.sh) speaks the real EC2 /
ELBv2 / RDS wire protocols on 127.0.0.1; the tool runs unmodified as a
subprocess and boto3 is pointed at the emulator with AWS_ENDPOINT_URL. The
fixtures are created through boto3 exactly as they would exist in a real
account (unattached volumes, idle EIPs, stopped instances, ...).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "aws_waste_finder.py")
REGION = "us-west-2"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def setUpModule():
    global SERVER, ENDPOINT, TMP
    TMP = tempfile.mkdtemp()
    port = _free_port()
    ENDPOINT = f"http://127.0.0.1:{port}"
    log = open(os.path.join(TMP, "moto.log"), "w")
    SERVER = subprocess.Popen([sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
                              stdout=log, stderr=subprocess.STDOUT)
    for _ in range(300):  # importing every moto backend can take a while on a busy runner
        try:
            urllib.request.urlopen(ENDPOINT + "/moto-api/reset", data=b"", timeout=2)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            if SERVER.poll() is not None:
                break
            time.sleep(0.3)
    with open(os.path.join(TMP, "moto.log")) as f:
        raise RuntimeError("moto server did not start:\n" + f.read()[-3000:])


def tearDownModule():
    SERVER.terminate()
    SERVER.wait(10)


def base_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_ENDPOINT_URL=ENDPOINT, AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
               AWS_DEFAULT_REGION=REGION, AWS_EC2_METADATA_DISABLED="true",
               AWS_CONFIG_FILE=os.path.join(TMP, "no-config"),
               AWS_SHARED_CREDENTIALS_FILE=os.path.join(TMP, "no-credentials"))
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def run(*args, env=None, python_args=()):
    return subprocess.run([sys.executable] + list(python_args) + [TOOL] + list(args), capture_output=True,
                          text=True, env=env or base_env(), timeout=180)


def client(service, region=REGION):
    return boto3.client(service, region_name=region, endpoint_url=ENDPOINT,
                        aws_access_key_id="testing", aws_secret_access_key="testing")


class Base(unittest.TestCase):
    def setUp(self):
        urllib.request.urlopen(urllib.request.Request(ENDPOINT + "/moto-api/reset", method="POST"), timeout=10)
        self.ec2 = client("ec2")
        self.ami = self.ec2.describe_images()["Images"][0]["ImageId"]

    def findings(self, *args, expect_code=None):
        p = run("--json", *args)
        if expect_code is not None:
            self.assertEqual(p.returncode, expect_code, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def instance(self, name=None):
        kw = {}
        if name:
            kw["TagSpecifications"] = [{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}]
        return self.ec2.run_instances(ImageId=self.ami, MinCount=1, MaxCount=1, InstanceType="t3.micro",
                                      **kw)["Instances"][0]


class TestFindings(Base):
    def test_clean_account_exits_zero(self):
        f = self.findings(expect_code=0)
        self.assertTrue(all(v == [] for k, v in f.items() if isinstance(v, list)), f)

    def test_unattached_volume_flagged_attached_not(self):
        vol = self.ec2.create_volume(AvailabilityZone=REGION + "a", Size=17, VolumeType="gp3")
        self.instance()  # its root volume is attached (in-use) and must not be flagged
        f = self.findings(expect_code=1)
        self.assertEqual([v["id"] for v in f["unattached_volumes"]], [vol["VolumeId"]])
        self.assertEqual(f["unattached_volumes"][0]["size_gb"], 17)

    def test_unassociated_eip_flagged_associated_not(self):
        idle = self.ec2.allocate_address(Domain="vpc")
        used = self.ec2.allocate_address(Domain="vpc")
        self.ec2.associate_address(AllocationId=used["AllocationId"], InstanceId=self.instance()["InstanceId"])
        f = self.findings(expect_code=1)
        self.assertEqual([a["allocation_id"] for a in f["unassociated_eips"]], [idle["AllocationId"]])

    def test_stopped_instance_flagged_running_not(self):
        stopped = self.instance("batch-box")["InstanceId"]
        self.instance("web")
        self.ec2.stop_instances(InstanceIds=[stopped])
        f = self.findings(expect_code=1)
        self.assertEqual([(i["id"], i["name"]) for i in f["stopped_instances"]], [(stopped, "batch-box")])

    def test_old_snapshots_and_days_option(self):
        vol = self.ec2.create_volume(AvailabilityZone=REGION + "a", Size=5)
        snap = self.ec2.create_snapshot(VolumeId=vol["VolumeId"], Description="nightly")
        self.ec2.delete_volume(VolumeId=vol["VolumeId"])
        ids = lambda f: {s["id"]: s for s in f["old_snapshots"]}  # noqa: E731
        self.assertNotIn(snap["SnapshotId"], ids(self.findings()))  # default --days 90: brand new snapshot
        f = ids(self.findings("--days", "0", expect_code=1))
        self.assertEqual(f[snap["SnapshotId"]]["description"], "nightly")
        self.assertEqual(f[snap["SnapshotId"]]["size_gb"], 5)

    def test_nat_gateway_flagged(self):
        vpc = self.ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
        subnet = self.ec2.create_subnet(VpcId=vpc, CidrBlock="10.9.1.0/24")["Subnet"]["SubnetId"]
        eip = self.ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat = self.ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip)["NatGateway"]["NatGatewayId"]
        f = self.findings(expect_code=1)
        self.assertEqual([(n["id"], n["vpc_id"]) for n in f["nat_gateways"]], [(nat, vpc)])

    def test_idle_load_balancer_flagged_busy_one_not(self):
        elb = client("elbv2")
        vpc = self.ec2.create_vpc(CidrBlock="10.8.0.0/16")["Vpc"]["VpcId"]
        subnets = [self.ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.8.{i}.0/24", AvailabilityZone=REGION + az)
                   ["Subnet"]["SubnetId"] for i, az in ((1, "a"), (2, "b"))]
        idle = elb.create_load_balancer(Name="idle-alb", Subnets=subnets)["LoadBalancers"][0]
        busy = elb.create_load_balancer(Name="busy-alb", Subnets=subnets)["LoadBalancers"][0]
        tg = elb.create_target_group(Name="busy-tg", Protocol="HTTP", Port=80, VpcId=vpc)["TargetGroups"][0]
        elb.create_listener(LoadBalancerArn=busy["LoadBalancerArn"], Protocol="HTTP", Port=80,
                            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}])
        inst = self.ec2.run_instances(ImageId=self.ami, MinCount=1, MaxCount=1, SubnetId=subnets[0])
        elb.register_targets(TargetGroupArn=tg["TargetGroupArn"],
                             Targets=[{"Id": inst["Instances"][0]["InstanceId"]}])
        health = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
        self.assertEqual(health[0]["TargetHealth"]["State"], "healthy")
        f = self.findings(expect_code=1)
        self.assertEqual([lb["name"] for lb in f["idle_load_balancers"]], ["idle-alb"])
        self.assertEqual(f["idle_load_balancers"][0]["arn"], idle["LoadBalancerArn"])
        self.assertNotIn("idle_load_balancers_error", f)

    def test_rds_listed_for_review_only(self):
        client("rds").create_db_instance(DBInstanceIdentifier="orders-db", DBInstanceClass="db.t3.micro",
                                         Engine="postgres", AllocatedStorage=20, MasterUsername="u",
                                         MasterUserPassword="testtesttest")
        f = self.findings(expect_code=0)  # RDS never counts toward the exit code
        self.assertEqual([(d["id"], d["class"]) for d in f["rds_instances_for_review"]],
                         [("orders-db", "db.t3.micro")])

    def test_text_report(self):
        self.ec2.allocate_address(Domain="vpc")
        p = run()
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("=== Unassociated Elastic IPs (1) ===", p.stdout)
        self.assertIn("=== Unattached EBS volumes (0) ===", p.stdout)
        self.assertIn("(none found)", p.stdout)
        self.assertIn("RDS instances (review manually", p.stdout)


class TestOptionsAndErrors(Base):
    def test_region_option_scopes_the_scan(self):
        self.ec2.allocate_address(Domain="vpc")
        p = run("--json", "--region", "us-east-1", env=base_env(AWS_DEFAULT_REGION=None))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["unassociated_eips"], [])
        p = run("--json", "--region", REGION, env=base_env(AWS_DEFAULT_REGION=None))
        self.assertEqual(p.returncode, 1, p.stderr)

    def test_profile_option(self):
        creds = os.path.join(TMP, "credentials")
        conf = os.path.join(TMP, "config")
        with open(creds, "w") as f:
            f.write("[waste]\naws_access_key_id = testing\naws_secret_access_key = testing\n")
        with open(conf, "w") as f:
            f.write(f"[profile waste]\nregion = {REGION}\n")
        self.ec2.allocate_address(Domain="vpc")
        env = base_env(AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None, AWS_DEFAULT_REGION=None,
                       AWS_SHARED_CREDENTIALS_FILE=creds, AWS_CONFIG_FILE=conf)
        p = run("--json", "--profile", "waste", env=env)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)["unassociated_eips"]), 1)
        p = run("--profile", "nope", env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ERROR", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_no_region_is_friendly_error(self):
        p = run(env=base_env(AWS_DEFAULT_REGION=None))
        self.assertEqual(p.returncode, 2)
        self.assertIn("no AWS region configured", p.stderr)

    def test_no_credentials_is_friendly_error(self):
        p = run(env=base_env(AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None))
        self.assertEqual(p.returncode, 2)
        self.assertIn("no AWS credentials found", p.stderr)

    def test_api_error_is_friendly_error(self):
        # Nothing listens on this endpoint: a connection failure, not a traceback.
        p = run(env=base_env(AWS_ENDPOINT_URL=f"http://127.0.0.1:{_free_port()}",
                             AWS_MAX_ATTEMPTS="1", AWS_RETRY_MODE="standard"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("ERROR", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_missing_boto3_message(self):
        # -S: no site-packages, so boto3 is genuinely not importable.
        p = run(python_args=("-S",))
        self.assertEqual(p.returncode, 2)
        self.assertIn("pip install boto3", p.stderr)

    def test_help(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0)
        for opt in ("--profile", "--region", "--days", "--json"):
            self.assertIn(opt, p.stdout)


class TestFailedCheckIsVisible(unittest.TestCase):
    """A check that errors (e.g. missing elasticloadbalancing:Describe* permission)
    must say so in the human report, not print "(none found)" like a clean result."""

    def test_text_report_shows_failed_check(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("aws_waste_finder", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        findings = {k: [] for k in ("unattached_volumes", "unassociated_eips", "stopped_instances",
                                    "old_snapshots", "nat_gateways", "idle_load_balancers",
                                    "rds_instances_for_review")}
        findings["idle_load_balancers_error"] = "AccessDenied: not authorized"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.print_report(findings)
        out = buf.getvalue()
        lb = out.split("=== Idle Load Balancers")[1].split("===")[1]
        self.assertIn("check FAILED", lb)
        self.assertIn("AccessDenied", lb)
        self.assertNotIn("(none found)", lb)
        self.assertIn("(none found)", out.split("=== Unattached EBS volumes")[1])


if __name__ == "__main__":
    unittest.main()
