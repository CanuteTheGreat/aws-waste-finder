"""Detection accuracy on a realistic mixed account (moto, real AWS wire protocol).

One region holds a known set of wasted resources next to resources that are in use and must
NOT be flagged. The flagged IDs per category must equal the expected set exactly (anything
extra is a false positive, anything missing a false negative), and the total must equal the
independently computed list-price sum with nothing counted twice.
"""
import datetime as dt
import unittest

import test_aws_waste_finder as base
from test_aws_waste_finder import REGION, client

HOURS = 730


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


class TestMixedAccount(base.Base):
    def test_exact_findings_and_total(self):
        ec2 = self.ec2
        expect = {k: set() for k in ("unattached_volumes", "gp2_volumes", "unassociated_eips", "stopped_instances",
                                     "nat_gateways", "idle_load_balancers", "idle_rds_instances")}
        total = 0.0

        # EBS: two forgotten volumes, one gp3 in use (clean), one gp2 in use (gp3 saving only)
        for size, vtype, price in ((50, "gp3", 0.08), (10, "io1", 0.125)):
            kw = {"Iops": 100} if vtype == "io1" else {}
            v = ec2.create_volume(AvailabilityZone=REGION + "a", Size=size, VolumeType=vtype, **kw)["VolumeId"]
            expect["unattached_volumes"].add(v)
            total += round(size * price, 2)  # each finding is shown rounded; the total adds those
        web = self.instance("web")["InstanceId"]
        web_root = ec2.describe_instances(InstanceIds=[web])["Reservations"][0]["Instances"][0][
            "BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
        root = ec2.describe_volumes(VolumeIds=[web_root])["Volumes"][0]
        if root["VolumeType"] == "gp2":  # moto's default root volume: in use, so only the gp3 saving counts
            expect["gp2_volumes"].add(web_root)
            total += round(root["Size"] * 0.02, 2)
        for vtype in ("gp3", "gp2"):
            v = ec2.create_volume(AvailabilityZone=REGION + "a", Size=40, VolumeType=vtype)["VolumeId"]
            ec2.attach_volume(VolumeId=v, InstanceId=web, Device="/dev/sd" + ("f" if vtype == "gp3" else "g"))
            if vtype == "gp2":
                expect["gp2_volumes"].add(v)
                total += round(40 * 0.02, 2)

        # a stopped instance: its gp2 root volume shows in gp2_volumes too, but its dollars are
        # already in stopped_instances and must not be added to the total a second time
        batch = self.instance("batch")["InstanceId"]
        ec2.stop_instances(InstanceIds=[batch])
        broot = ec2.describe_instances(InstanceIds=[batch])["Reservations"][0]["Instances"][0][
            "BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
        bvol = ec2.describe_volumes(VolumeIds=[broot])["Volumes"][0]
        expect["stopped_instances"].add(batch)
        total += round(bvol["Size"] * {"gp2": 0.10, "gp3": 0.08}[bvol["VolumeType"]], 2)
        if bvol["VolumeType"] == "gp2":
            expect["gp2_volumes"].add(broot)

        # Elastic IPs: one idle, one on the running instance
        idle_ip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        used_ip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        ec2.associate_address(AllocationId=used_ip, InstanceId=web)
        expect["unassociated_eips"].add(idle_ip)
        total += round(0.005 * HOURS, 2)

        # NAT gateway (its own EIP is associated with it, so not an idle EIP)
        vpc = ec2.create_vpc(CidrBlock="10.20.0.0/16")["Vpc"]["VpcId"]
        subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock="10.20.%d.0/24" % i, AvailabilityZone=REGION + az)
                   ["Subnet"]["SubnetId"] for i, az in ((1, "a"), (2, "b"))]
        nat_ip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        expect["nat_gateways"].add(ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=nat_ip)
                                   ["NatGateway"]["NatGatewayId"])
        total += round(0.045 * HOURS, 2)
        if not any(a.get("AssociationId") for a in ec2.describe_addresses(AllocationIds=[nat_ip])["Addresses"]):
            expect["unassociated_eips"].add(nat_ip)  # an emulator that doesn't associate it: still idle there
            total += round(0.005 * HOURS, 2)

        # load balancers: one idle ALB, one serving a healthy target
        elb = client("elbv2")
        idle = elb.create_load_balancer(Name="old-alb", Subnets=subnets)["LoadBalancers"][0]
        busy = elb.create_load_balancer(Name="live-alb", Subnets=subnets)["LoadBalancers"][0]
        tg = elb.create_target_group(Name="live-tg", Protocol="HTTP", Port=80, VpcId=vpc)["TargetGroups"][0]
        elb.create_listener(LoadBalancerArn=busy["LoadBalancerArn"], Protocol="HTTP", Port=80,
                            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg["TargetGroupArn"]}])
        target = ec2.run_instances(ImageId=self.ami, MinCount=1, MaxCount=1, SubnetId=subnets[0])["Instances"][0]
        elb.register_targets(TargetGroupArn=tg["TargetGroupArn"], Targets=[{"Id": target["InstanceId"]}])
        troot = target["BlockDeviceMappings"][0]["Ebs"]["VolumeId"] if target.get("BlockDeviceMappings") else \
            ec2.describe_instances(InstanceIds=[target["InstanceId"]])["Reservations"][0]["Instances"][0][
                "BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
        tvol = ec2.describe_volumes(VolumeIds=[troot])["Volumes"][0]
        if tvol["VolumeType"] == "gp2":  # the running target's root volume: gp3 saving only
            expect["gp2_volumes"].add(troot)
            total += round(tvol["Size"] * 0.02, 2)
        expect["idle_load_balancers"].add("old-alb")
        total += round(0.0225 * HOURS, 2)

        # RDS: idle for the whole window (zero metrics), busy, and brand new with no metrics yet
        rds, cw = client("rds"), client("cloudwatch")
        now = dt.datetime.now(dt.timezone.utc)
        for name in ("idle-db", "busy-db", "new-db"):
            rds.create_db_instance(DBInstanceIdentifier=name, DBInstanceClass="db.t3.micro", Engine="postgres",
                                   AllocatedStorage=20, MasterUsername="u", MasterUserPassword="testtesttest")
        for name, value in (("idle-db", 0), ("busy-db", 3)):
            cw.put_metric_data(Namespace="AWS/RDS", MetricData=[{
                "MetricName": "DatabaseConnections", "Value": value, "Timestamp": now - dt.timedelta(days=d, hours=1),
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": name}]} for d in range(7)])
        expect["idle_rds_instances"].add("idle-db")  # RDS instance hours aren't estimated: no $ added

        f = self.findings(expect_code=1)
        got = {
            "unattached_volumes": {v["id"] for v in f["unattached_volumes"]},
            "gp2_volumes": {v["id"] for v in f["gp2_volumes"]},
            "unassociated_eips": {a["allocation_id"] for a in f["unassociated_eips"]},
            "stopped_instances": {i["id"] for i in f["stopped_instances"]},
            "nat_gateways": {n["id"] for n in f["nat_gateways"]},
            "idle_load_balancers": {lb["name"] for lb in f["idle_load_balancers"]},
            "idle_rds_instances": {d["id"] for d in f["idle_rds_instances"]},
        }
        for k in expect:
            with self.subTest(category=k):
                self.assertEqual(got[k], expect[k], "false %s" % ("positive" if got[k] - expect[k] else "negative"))
        review = {d["id"]: d for d in f["rds_instances_for_review"]}
        self.assertEqual(set(review), {"busy-db", "new-db"})
        self.assertEqual(review["new-db"]["note"], "no DatabaseConnections metrics of the last 7")
        self.assertEqual(f["old_snapshots"], [])
        self.assertAlmostEqual(f["estimated_monthly_usd"], round(total, 2), places=2)
        if broot in got["gp2_volumes"]:
            entry = next(v for v in f["gp2_volumes"] if v["id"] == broot)
            self.assertEqual(entry["counted_in"], "stopped_instances")

    def test_snapshot_of_unused_ami_counted_once(self):
        inst = self.instance()["InstanceId"]
        image = self.ec2.create_image(InstanceId=inst, Name="golden")["ImageId"]
        self.ec2.terminate_instances(InstanceIds=[inst])
        f = self.findings("--days", "0", expect_code=1)
        ami = next(a for a in f["unused_amis"] if a["id"] == image)
        snaps = [s for s in f["old_snapshots"] if s["id"] in ami["snapshots"]]
        self.assertTrue(snaps)
        self.assertTrue(all(s["counted_in"] == "unused_amis" for s in snaps))
        counted = sum(i["estimated_monthly_usd"] or 0 for k in ("old_snapshots", "unused_amis") for i in f[k]
                      if "counted_in" not in i)
        self.assertAlmostEqual(f["estimated_monthly_usd"], round(counted, 2), places=2)
        self.assertAlmostEqual(counted, ami["estimated_monthly_usd"]
                               + sum(s["estimated_monthly_usd"] for s in f["old_snapshots"]
                                     if s["id"] not in ami["snapshots"]), places=2)


class TestReadOnly(unittest.TestCase):
    """README: "Never deletes, stops, or modifies any resource" and the read-only IAM policy it
    lists. Every AWS API the tool can call is found in its source (direct client calls and
    paginator operation names) and must be a read-only operation the documented policy allows."""
    ALLOWED = {"ec2": "Describe", "elbv2": "Describe", "elb": "Describe", "rds": "Describe",
               "cw": "GetMetricStatistics", "logs": "DescribeLogGroups"}

    def test_only_documented_read_only_calls(self):
        import os
        import re
        with open(base.TOOL) as fh:
            src = fh.read()
        calls = set(re.findall(r"\b(ec2|elbv2|elb|rds|cw|logs)\.([a-z_]+)\(", src))
        calls |= set(re.findall(r"pages\((ec2|elbv2|elb|rds|cw|logs), \"([a-z_]+)\"", src))
        self.assertGreaterEqual(len(calls), 15)
        for svc, op in sorted(calls):
            with self.subTest(call="%s.%s" % (svc, op)):
                action = "".join(w.capitalize() for w in op.split("_"))
                self.assertTrue(action.startswith(self.ALLOWED[svc]), action)
        with open(os.path.join(os.path.dirname(base.TOOL), "README.md")) as fh:
            readme = fh.read()
        for perm in ("ec2:Describe*", "elasticloadbalancing:Describe*", "rds:Describe*",
                     "cloudwatch:GetMetricStatistics", "logs:DescribeLogGroups"):
            self.assertIn(perm, readme)


if __name__ == "__main__":
    unittest.main()
