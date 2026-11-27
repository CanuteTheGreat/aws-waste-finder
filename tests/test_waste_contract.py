"""In-process contract tests for find_waste(), _check(), pages(), enabled_regions() and main().

The moto end-to-end suite runs the tool as a subprocess against an emulator, which never produces the
awkward shapes real accounts do (a page without its list, an instance without tags or block-device
mappings, a volume of an unknown type, a check that fails halfway, a ClientError without a Code).
These tests hand the tool a scripted session and pin the exact finding dicts, error keys and
messages, the API arguments (Owners / OwnerIds / Filters ...) and the argparse help body, so a mutated
key, default, literal, operator or help string changes an assertion.
"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import unittest
from unittest import mock

import botocore.exceptions

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aws_waste_finder.py")
UTC = datetime.timezone.utc


def load_tool():
    spec = importlib.util.spec_from_file_location("aws_waste_finder", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def client_error(code=None, message=None, op="Op"):
    err = {}
    if code is not None:
        err["Code"] = code
    if message is not None:
        err["Message"] = message
    return botocore.exceptions.ClientError({"Error": err}, op)


class Fake:
    """A boto3 client: plain calls and paginators return scripted data (or raise it / call it with the kwargs)."""

    def __init__(self, **ops):
        self.ops = ops
        self.calls = []

    def _result(self, op, kw):
        self.calls.append((op, kw))
        r = self.ops[op]
        if callable(r):
            r = r(kw)
        if isinstance(r, BaseException):
            raise r
        return r

    def get_paginator(self, op):
        fake = self

        class Paginator:
            def paginate(self, **kw):
                return fake._result(op, kw)
        return Paginator()

    def __getattr__(self, op):
        if op.startswith("__"):
            raise AttributeError(op)
        return lambda **kw: self._result(op, kw)

    def kwargs(self, op):
        return [kw for o, kw in self.calls if o == op]


class Session:
    region_name = "us-east-1"

    def __init__(self, **clients):
        self.clients = clients
        self.made = []

    def client(self, service, region_name=None):
        self.made.append((service, region_name))
        return self.clients[service]


def empty_session(**over):
    """A session whose every API answers 'nothing'; keyword args replace a whole client."""
    clients = {
        "ec2": Fake(describe_volumes=[{}], describe_addresses={"Addresses": []}, describe_instances=[{}],
                    describe_images={"Images": []}, describe_snapshots=[{}], describe_nat_gateways=[{}],
                    describe_network_interfaces=[{}]),
        "elbv2": Fake(describe_load_balancers=[{}]),
        "elb": Fake(describe_load_balancers=[{}]),
        "rds": Fake(describe_db_instances=[{}]),
        "cloudwatch": Fake(),
        "logs": Fake(describe_log_groups=[{}]),
    }
    clients.update(over)
    return Session(**clients)


def days_ago(n):
    return datetime.datetime.now(UTC) - datetime.timedelta(days=n)


class TestFindWasteGolden(unittest.TestCase):
    """One account holding every awkward shape; the exact findings come back."""

    def setUp(self):
        self.m = load_tool()
        self.created = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        self.old_start = days_ago(200)
        self.mid_start = days_ago(100)
        ec2 = Fake(
            describe_volumes=[
                {"Volumes": [
                    {"VolumeId": "vol-a", "Size": 10, "VolumeType": "gp3", "State": "available", "CreateTime": self.created},
                    {"VolumeId": "vol-b", "Size": 7, "VolumeType": "mystery", "State": "available", "CreateTime": self.created},
                    {"VolumeId": "vol-c", "Size": 100, "VolumeType": "gp2", "State": "in-use", "CreateTime": self.created},
                    {"VolumeId": "vol-d", "Size": 50, "VolumeType": "gp3", "State": "in-use", "CreateTime": self.created},
                    {"VolumeId": "vol-e", "Size": 20, "VolumeType": "gp2", "State": "in-use", "CreateTime": self.created},
                ]},
                {},  # a page without the list
            ],
            describe_addresses={"Addresses": [
                {"PublicIp": "1.2.3.4", "AllocationId": "eipalloc-1"},
                {"PublicIp": "5.6.7.8", "AllocationId": "eipalloc-2", "AssociationId": "eipassoc-2"},
            ]},
            describe_instances=[
                {"Reservations": [{"Instances": [
                    {"InstanceId": "i-term", "ImageId": "ami-term", "State": {"Name": "terminated"}},
                    {"InstanceId": "i-run", "ImageId": "ami-run", "State": {"Name": "running"}},
                    {"InstanceId": "i-stop", "ImageId": "ami-stop", "InstanceType": "t3.micro", "State": {"Name": "stopped"},
                     "Tags": [{"Key": "Env", "Value": "prod"}, {"Key": "Name", "Value": "web"}],
                     "BlockDeviceMappings": [{"DeviceName": "/dev/sdb"},
                                             {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-e"}},
                                             {"DeviceName": "/dev/sdc", "Ebs": {"VolumeId": "vol-gone"}}]},
                    {"InstanceId": "i-bare", "State": {"Name": "stopped"}, "InstanceType": "m5.large"},
                ]}]},
                {},
            ],
            describe_images={"Images": [
                {"ImageId": "ami-term", "Name": "gone", "CreationDate": "2026-01-01T00:00:00.000Z",
                 "BlockDeviceMappings": [{"DeviceName": "/dev/sdb"}, {"Ebs": {}},
                                         {"Ebs": {"SnapshotId": "snap-old"}}, {"Ebs": {"SnapshotId": "snap-unlisted"}}]},
                {"ImageId": "ami-run", "Name": "in use", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-mid"}}]},
                {"ImageId": "ami-stop", "Name": "stopped-one"},
                {"ImageId": "ami-bare"},
            ]},
            describe_snapshots=[
                {"Snapshots": [
                    {"SnapshotId": "snap-old", "VolumeSize": 30, "StartTime": self.old_start, "Description": "nightly"},
                    {"SnapshotId": "snap-mid", "VolumeSize": 8, "StartTime": self.mid_start},
                    {"SnapshotId": "snap-new", "VolumeSize": 9, "StartTime": days_ago(10), "Description": "recent"},
                ]},
                {},
            ],
            describe_nat_gateways=[{"NatGateways": [{"NatGatewayId": "nat-1", "VpcId": "vpc-1"}]}, {}],
            describe_network_interfaces=[{"NetworkInterfaces": [
                {"NetworkInterfaceId": "eni-1", "Description": "lambda", "Association": {"PublicIp": "9.9.9.9"}},
                {"NetworkInterfaceId": "eni-2"},
                {"NetworkInterfaceId": "eni-3", "Description": "priv", "Association": {}},
            ]}, {}],
        )

        def target_groups(kw):
            arn = kw["LoadBalancerArn"]
            if arn == "arn:none":
                return client_error("TargetGroupNotFound", "no groups", "DescribeTargetGroups")
            return [{"TargetGroups": [{"TargetGroupArn": "tg:" + arn}]}, {}]

        def target_health(kw):
            arn = kw["TargetGroupArn"]
            state = {"tg:arn:ok": "healthy", "tg:arn:sick": "unhealthy"}[arn]
            return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "draining"}},
                                                 {"TargetHealth": {"State": state}}]}

        elbv2 = Fake(
            describe_load_balancers=[{"LoadBalancers": [
                {"LoadBalancerName": "ok", "Type": "application", "LoadBalancerArn": "arn:ok"},
                {"LoadBalancerName": "sick", "Type": "network", "LoadBalancerArn": "arn:sick"},
                {"LoadBalancerName": "none", "Type": "gateway", "LoadBalancerArn": "arn:none"},
                {"LoadBalancerName": "odd", "Type": "weird", "LoadBalancerArn": "arn:none"},
            ]}, {}],
            describe_target_groups=target_groups,
            describe_target_health=lambda kw: target_health({"TargetGroupArn": kw["TargetGroupArn"].replace("tg:arn:none", "tg:arn:sick")}),
        )
        elb = Fake(
            describe_load_balancers=[{"LoadBalancerDescriptions": [{"LoadBalancerName": "c-up"}, {"LoadBalancerName": "c-down"}]}, {}],
            describe_instance_health=lambda kw: {"InstanceStates": [{"State": "OutOfService"}] + (
                [{"State": "InService"}] if kw["LoadBalancerName"] == "c-up" else [])},
        )
        dbs = {
            "db-idle": {"DBInstanceIdentifier": "db-idle", "DBInstanceClass": "db.t3.micro", "AllocatedStorage": 20,
                        "MultiAZ": False, "DBInstanceStatus": "available"},
            "db-nometrics": {"DBInstanceIdentifier": "db-nometrics", "DBInstanceClass": "db.t3.small", "AllocatedStorage": 5,
                             "MultiAZ": True, "DBInstanceStatus": "available"},
            "db-young": {"DBInstanceIdentifier": "db-young", "DBInstanceClass": "db.t3.small", "AllocatedStorage": 5,
                         "MultiAZ": False, "DBInstanceStatus": "available"},
            "db-busy": {"DBInstanceIdentifier": "db-busy", "DBInstanceClass": "db.m5.large", "AllocatedStorage": 100,
                        "MultiAZ": True, "DBInstanceStatus": "available"},
            "db-stopped": {"DBInstanceIdentifier": "db-stopped", "DBInstanceClass": "db.t3.micro", "AllocatedStorage": 10,
                           "MultiAZ": False, "DBInstanceStatus": "stopped"},
            "db-bare": {"DBInstanceIdentifier": "db-bare", "DBInstanceClass": "db.t3.micro", "DBInstanceStatus": "available"},
            "db-nostatus": {"DBInstanceIdentifier": "db-nostatus", "DBInstanceClass": "db.t3.micro"},
        }
        now = datetime.datetime.now(UTC)
        points = {
            "db-idle": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=5)}, {"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=4)}],
            "db-nometrics": [],
            "db-young": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=1)}],
            "db-busy": [{"Maximum": 1.0, "Timestamp": now - datetime.timedelta(days=5)}, {"Maximum": 5.0, "Timestamp": now - datetime.timedelta(days=4)},
                        {"Maximum": 2.0, "Timestamp": now - datetime.timedelta(days=3)}],
            "db-stopped": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=6)}],
            "db-bare": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=6)}],
            "db-nostatus": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(days=6)}],
        }
        rds = Fake(describe_db_instances=[{"DBInstances": [dbs["db-idle"], dbs["db-nometrics"], dbs["db-young"]]},
                                          {"DBInstances": [dbs["db-busy"], dbs["db-stopped"], dbs["db-bare"], dbs["db-nostatus"]]}, {}])
        cw = Fake(get_metric_statistics=lambda kw: {"Datapoints": points[kw["Dimensions"][0]["Value"]]})
        logs = Fake(describe_log_groups=[{"logGroups": [
            {"logGroupName": "/empty0", "storedBytes": 0},
            {"logGroupName": "/empty-missing"},
            {"logGroupName": "/forever", "storedBytes": 5000000000},
            {"logGroupName": "/kept", "storedBytes": 2500000000, "retentionInDays": 30},
            {"logGroupName": "/edge", "storedBytes": 833333334},
            {"logGroupName": "/zero-retention", "storedBytes": 1234567891, "retentionInDays": 0},
        ]}, {}])
        self.session = Session(ec2=ec2, elbv2=elbv2, elb=elb, rds=rds, cloudwatch=cw, logs=logs)
        self.clients = dict(ec2=ec2, elbv2=elbv2, elb=elb, rds=rds, cloudwatch=cw, logs=logs)
        self.f = self.m.find_waste(self.session, "eu-west-3", 90, 3)

    def test_clients_are_made_for_the_requested_region(self):
        self.assertEqual(sorted(self.session.made),
                         sorted([("ec2", "eu-west-3"), ("elbv2", "eu-west-3"), ("elb", "eu-west-3"),
                                 ("rds", "eu-west-3"), ("cloudwatch", "eu-west-3"), ("logs", "eu-west-3")]))

    def test_every_category_key_is_present_and_no_check_failed(self):
        self.assertEqual(sorted(k for k in self.f if k.endswith("_error")), [])
        self.assertEqual(sorted(self.f), sorted([k for k, _, _ in self.m.CATEGORIES] + ["estimated_monthly_usd"]))

    def test_unattached_volumes(self):
        self.assertEqual(self.f["unattached_volumes"], [
            {"id": "vol-a", "size_gb": 10, "type": "gp3", "created": "2026-03-04T05:06:07+00:00", "estimated_monthly_usd": 0.8},
            {"id": "vol-b", "size_gb": 7, "type": "mystery", "created": "2026-03-04T05:06:07+00:00", "estimated_monthly_usd": 0.7},
        ])

    def test_gp2_volumes_only_in_use_gp2_and_marked_when_a_stopped_instance_owns_them(self):
        self.assertEqual(self.f["gp2_volumes"], [
            {"id": "vol-c", "size_gb": 100, "state": "in-use", "estimated_monthly_usd": 2.0},
            {"id": "vol-e", "size_gb": 20, "state": "in-use", "estimated_monthly_usd": 0.4, "counted_in": "stopped_instances"},
        ])

    def test_unassociated_eips(self):
        self.assertEqual(self.f["unassociated_eips"],
                         [{"ip": "1.2.3.4", "allocation_id": "eipalloc-1", "estimated_monthly_usd": 3.65}])

    def test_stopped_instances_skip_terminated_ones_without_stopping_the_scan(self):
        self.assertEqual(self.f["stopped_instances"], [
            {"id": "i-stop", "type": "t3.micro", "name": "web", "volumes": ["vol-e", "vol-gone"], "estimated_monthly_usd": 2.0},
            {"id": "i-bare", "type": "m5.large", "name": None, "volumes": [], "estimated_monthly_usd": 0},
        ])

    def test_old_snapshots_and_ami_marking(self):
        self.assertEqual(self.f["old_snapshots"], [
            {"id": "snap-old", "size_gb": 30, "started": self.old_start.isoformat(), "description": "nightly",
             "used_by_ami": "ami-term", "estimated_monthly_usd": 1.5, "counted_in": "unused_amis"},
            {"id": "snap-mid", "size_gb": 8, "started": self.mid_start.isoformat(), "description": "",
             "used_by_ami": "ami-run", "estimated_monthly_usd": 0.4},
        ])

    def test_unused_amis_exclude_images_a_live_instance_uses(self):
        self.assertEqual(self.f["unused_amis"], [
            {"id": "ami-term", "name": "gone", "snapshots": ["snap-old", "snap-unlisted"],
             "created": "2026-01-01T00:00:00.000Z", "estimated_monthly_usd": 1.5},
            {"id": "ami-bare", "name": None, "snapshots": [], "created": None, "estimated_monthly_usd": 0},
        ])

    def test_nat_gateways(self):
        self.assertEqual(self.f["nat_gateways"], [{"id": "nat-1", "vpc_id": "vpc-1", "estimated_monthly_usd": 32.85}])

    def test_unattached_network_interfaces(self):
        self.assertEqual(self.f["unattached_network_interfaces"], [
            {"id": "eni-1", "description": "lambda", "public_ip": "9.9.9.9", "estimated_monthly_usd": 3.65},
            {"id": "eni-2", "description": "", "public_ip": None, "estimated_monthly_usd": 0},
            {"id": "eni-3", "description": "priv", "public_ip": None, "estimated_monthly_usd": 0},
        ])

    def test_idle_load_balancers(self):
        self.assertEqual(self.f["idle_load_balancers"], [
            {"name": "sick", "type": "network", "arn": "arn:sick", "estimated_monthly_usd": 16.43},
            {"name": "none", "type": "gateway", "arn": "arn:none", "estimated_monthly_usd": 9.12},
            {"name": "odd", "type": "weird", "arn": "arn:none", "estimated_monthly_usd": 16.43},
            {"name": "c-down", "type": "classic", "arn": None, "estimated_monthly_usd": 18.25},
        ])

    def test_idle_rds_and_the_ones_left_for_review(self):
        self.assertEqual(self.f["idle_rds_instances"], [
            {"id": "db-idle", "class": "db.t3.micro", "storage_gb": 20, "multi_az": False, "status": "available",
             "max_connections": 0.0, "estimated_monthly_usd": None, "storage_monthly_usd": 2.0, "idle_days": 3},
            {"id": "db-bare", "class": "db.t3.micro", "storage_gb": None, "multi_az": None, "status": "available",
             "max_connections": 0.0, "estimated_monthly_usd": None, "storage_monthly_usd": 0, "idle_days": 3},
        ])
        self.assertEqual(self.f["rds_instances_for_review"], [
            {"id": "db-nometrics", "class": "db.t3.small", "storage_gb": 5, "multi_az": True, "status": "available",
             "max_connections": 0, "note": "no DatabaseConnections metrics of the last 3"},
            {"id": "db-young", "class": "db.t3.small", "storage_gb": 5, "multi_az": False, "status": "available",
             "max_connections": 0.0, "note": "metrics only cover 1.0 day(s) of the last 3"},
            {"id": "db-busy", "class": "db.m5.large", "storage_gb": 100, "multi_az": True, "status": "available",
             "max_connections": 5.0},
            {"id": "db-stopped", "class": "db.t3.micro", "storage_gb": 10, "multi_az": False, "status": "stopped",
             "max_connections": 0.0},
            {"id": "db-nostatus", "class": "db.t3.micro", "storage_gb": None, "multi_az": None, "status": None,
             "max_connections": 0.0},
        ])

    def test_cloudwatch_is_asked_for_the_daily_peak_of_each_database(self):
        calls = self.clients["cloudwatch"].kwargs("get_metric_statistics")
        self.assertEqual(len(calls), 7)
        first = calls[0]
        self.assertEqual({k: v for k, v in first.items() if k not in ("StartTime", "EndTime")},
                         {"Namespace": "AWS/RDS", "MetricName": "DatabaseConnections", "Period": 86400,
                          "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "db-idle"}], "Statistics": ["Maximum"]})
        self.assertEqual(first["EndTime"] - first["StartTime"], datetime.timedelta(days=3, hours=1))

    def test_log_groups(self):
        self.assertEqual(self.f["empty_log_groups"], [{"name": "/empty0", "estimated_monthly_usd": 0},
                                                      {"name": "/empty-missing", "estimated_monthly_usd": 0}])
        self.assertEqual(self.f["log_groups_without_retention"], [
            {"name": "/forever", "stored_gb": 5.0, "estimated_monthly_usd": 0.15},
            {"name": "/edge", "stored_gb": 0.833, "estimated_monthly_usd": 0.03},
            {"name": "/zero-retention", "stored_gb": 1.235, "estimated_monthly_usd": 0.04},
        ])

    def test_total_leaves_out_what_is_already_counted_elsewhere(self):
        # gated categories only (rds_instances_for_review / eni / log groups are not), skipping 'counted_in' items:
        # 0.8 + 0.7 (volumes) + 2.0 (vol-c gp2; vol-e is in its stopped instance) + 3.65 (eip)
        # + 2.0 (stopped web) + 0 + 0.4 (snap-mid; snap-old is in its AMI) + 1.5 + 0 (amis) + 32.85 (nat)
        # + 16.43 + 9.12 + 16.43 + 18.25 (load balancers) + 2.0 + 0.0 (idle rds storage is not part of estimated_monthly_usd: None)
        expected = round(0.8 + 0.7 + 2.0 + 3.65 + 2.0 + 0 + 0.4 + 1.5 + 0 + 32.85 + 16.43 + 9.12 + 16.43 + 18.25, 2)
        self.assertEqual(self.f["estimated_monthly_usd"], expected)

    def test_api_arguments(self):
        ec2 = self.clients["ec2"]
        self.assertEqual(ec2.kwargs("describe_images"), [{"Owners": ["self"]}])
        self.assertEqual(ec2.kwargs("describe_snapshots"), [{"OwnerIds": ["self"]}])
        self.assertEqual(ec2.kwargs("describe_nat_gateways"), [{"Filter": [{"Name": "state", "Values": ["available"]}]}])
        self.assertEqual(ec2.kwargs("describe_network_interfaces"),
                         [{"Filters": [{"Name": "status", "Values": ["available"]}]}])
        self.assertEqual(self.clients["elbv2"].kwargs("describe_target_groups"),
                         [{"LoadBalancerArn": a} for a in ("arn:ok", "arn:sick", "arn:none", "arn:none")])


class TestFindWasteIdleDaysBoundary(unittest.TestCase):
    def test_one_idle_day_needs_metrics_back_to_now_not_back_a_day(self):
        m = load_tool()
        now = datetime.datetime.now(UTC)
        rds = Fake(describe_db_instances=[{"DBInstances": [{"DBInstanceIdentifier": "d", "DBInstanceClass": "c",
                                                           "AllocatedStorage": 1, "DBInstanceStatus": "available"}]}])
        cw = Fake(get_metric_statistics={"Datapoints": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(hours=2)}]})
        f = m.find_waste(empty_session(rds=rds, cloudwatch=cw), "us-east-1", 90, 1)
        self.assertEqual([d["id"] for d in f["idle_rds_instances"]], ["d"])
        self.assertEqual(f["rds_instances_for_review"], [])

    def test_zero_idle_days_never_goes_negative(self):
        m = load_tool()
        now = datetime.datetime.now(UTC)
        rds = Fake(describe_db_instances=[{"DBInstances": [{"DBInstanceIdentifier": "d", "DBInstanceClass": "c",
                                                           "AllocatedStorage": 1, "DBInstanceStatus": "available"}]}])
        cw = Fake(get_metric_statistics={"Datapoints": [{"Maximum": 0.0, "Timestamp": now - datetime.timedelta(hours=2)}]})
        f = m.find_waste(empty_session(rds=rds, cloudwatch=cw), "us-east-1", 90, 0)
        self.assertEqual([d["id"] for d in f["idle_rds_instances"]], ["d"])


class TestCheckFailuresAreRecordedUnderTheirOwnKey(unittest.TestCase):
    def test_each_failing_check_is_named_and_the_rest_still_run(self):
        m = load_tool()
        ec2 = Fake(describe_volumes=[{"Volumes": [{"VolumeId": "vol-1", "Size": 3, "VolumeType": "gp3", "State": "available",
                                                   "CreateTime": datetime.datetime(2026, 1, 1, tzinfo=UTC)}]}],
                   describe_addresses=RuntimeError("eips down"),
                   describe_instances=RuntimeError("instances down"),
                   describe_images=RuntimeError("images down"),
                   describe_nat_gateways=RuntimeError("nat down"),
                   describe_network_interfaces=RuntimeError("enis down"))
        session = empty_session(ec2=ec2,
                                elbv2=Fake(describe_load_balancers=RuntimeError("elbv2 down")),
                                rds=Fake(describe_db_instances=RuntimeError("rds down")),
                                logs=Fake(describe_log_groups=RuntimeError("logs down")))
        f = m.find_waste(session, "us-east-1", 90)
        self.assertEqual({k: v for k, v in f.items() if k.endswith("_error")}, {
            "unassociated_eips_error": "eips down",
            "stopped_instances_error": "instances down",
            "old_snapshots_error": "images down",
            "nat_gateways_error": "nat down",
            "unattached_network_interfaces_error": "enis down",
            "idle_load_balancers_error": "elbv2 down",
            "idle_rds_instances_error": "rds down",
            "empty_log_groups_error": "logs down",
        })
        self.assertEqual([v["id"] for v in f["unattached_volumes"]], ["vol-1"])
        self.assertEqual(f["estimated_monthly_usd"], 0.24)

    def test_classic_lb_failure_after_v2_ones_were_found_keeps_the_error(self):
        m = load_tool()
        session = empty_session(elb=Fake(describe_load_balancers=RuntimeError("classic down")))
        f = m.find_waste(session, "us-east-1", 90)
        self.assertEqual(f["idle_load_balancers_error"], "classic down")

    def test_non_client_error_while_listing_target_groups_is_a_failed_check_with_its_own_message(self):
        m = load_tool()
        elbv2 = Fake(describe_load_balancers=[{"LoadBalancers": [{"LoadBalancerName": "x", "Type": "application",
                                                                  "LoadBalancerArn": "arn:x"}]}],
                     describe_target_groups=ValueError("tg boom"))
        f = m.find_waste(empty_session(elbv2=elbv2), "us-east-1", 90)
        self.assertEqual(f["idle_load_balancers_error"], "tg boom")
        self.assertEqual(f["idle_load_balancers"], [])

    def test_other_client_errors_listing_target_groups_are_failed_checks_too(self):
        m = load_tool()
        err = client_error("AccessDenied", "nope", "DescribeTargetGroups")
        elbv2 = Fake(describe_load_balancers=[{"LoadBalancers": [{"LoadBalancerName": "x", "Type": "application",
                                                                  "LoadBalancerArn": "arn:x"}]}],
                     describe_target_groups=err)
        f = m.find_waste(empty_session(elbv2=elbv2), "us-east-1", 90)
        self.assertEqual(f["idle_load_balancers_error"], str(err))

    def test_a_client_error_without_response_code_is_not_mistaken_for_target_group_not_found(self):
        m = load_tool()
        err = client_error(None, None, "DescribeTargetGroups")
        elbv2 = Fake(describe_load_balancers=[{"LoadBalancers": [{"LoadBalancerName": "x", "Type": "application",
                                                                  "LoadBalancerArn": "arn:x"}]}],
                     describe_target_groups=err)
        f = m.find_waste(empty_session(elbv2=elbv2), "us-east-1", 90)
        self.assertEqual(f["idle_load_balancers_error"], str(err))

    def test_classic_lb_with_an_in_service_instance_is_not_idle_and_state_key_is_read(self):
        m = load_tool()
        elb = Fake(describe_load_balancers=[{"LoadBalancerDescriptions": [{"LoadBalancerName": "c"}]}],
                   describe_instance_health={"InstanceStates": [{"State": "InService"}]})
        f = m.find_waste(empty_session(elb=elb), "us-east-1", 90)
        self.assertEqual((f["idle_load_balancers"], f.get("idle_load_balancers_error")), ([], None))
        self.assertEqual(elb.kwargs("describe_instance_health"), [{"LoadBalancerName": "c"}])


class TestCheckAndPages(unittest.TestCase):
    def test_check_records_the_message_under_key_error(self):
        m = load_tool()
        f = {}

        def boom():
            raise botocore.exceptions.BotoCoreError()
        m._check(f, "k", boom)
        self.assertEqual(f, {"k_error": str(botocore.exceptions.BotoCoreError())})
        g = {}
        m._check(g, "ok", lambda: None)
        self.assertEqual(g, {})

    def test_check_lets_missing_credentials_and_region_stop_the_scan(self):
        m = load_tool()
        for exc in (botocore.exceptions.NoCredentialsError(), botocore.exceptions.NoRegionError()):
            def boom(exc=exc):
                raise exc
            with self.assertRaises(type(exc)):
                m._check({}, "k", boom)

    def test_pages_yields_every_item_across_pages_and_tolerates_a_page_without_the_key(self):
        m = load_tool()
        c = Fake(describe_things=[{"Things": [1, 2]}, {}, {"Things": [3]}])
        self.assertEqual(list(m.pages(c, "describe_things", "Things", Foo="bar")), [1, 2, 3])
        self.assertEqual(c.kwargs("describe_things"), [{"Foo": "bar"}])


class TestEnabledRegions(unittest.TestCase):
    def setUp(self):
        self.m = load_tool()
        self.ec2 = Fake(describe_regions={"Regions": [{"RegionName": "us-west-2"}, {"RegionName": "eu-west-1"},
                                                      {"RegionName": "ap-south-1"}]})

    def test_sorted_names_and_opt_in_filter(self):
        s = Session(ec2=self.ec2)
        self.assertEqual(self.m.enabled_regions(s, "eu-west-1"), ["ap-south-1", "eu-west-1", "us-west-2"])
        self.assertEqual(s.made, [("ec2", "eu-west-1")])
        self.assertEqual(self.ec2.kwargs("describe_regions"),
                         [{"Filters": [{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]}])

    def test_no_region_asks_us_east_1(self):
        s = Session(ec2=self.ec2)
        self.m.enabled_regions(s, None)
        self.assertEqual(s.made, [("ec2", "us-east-1")])


def run_main(m, argv, session=None, find=None, regions=None):
    out, err = io.StringIO(), io.StringIO()
    session = session or Session()
    patches = [mock.patch.object(m.boto3, "Session", lambda **kw: session)]
    if find is not None:
        patches.append(mock.patch.object(m, "find_waste", find))
    if regions is not None:
        patches.append(mock.patch.object(m, "enabled_regions", regions))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main(argv)
    return code, out.getvalue(), err.getvalue()


def findings_for(m, usd=0.0, flagged=0):
    f = {k: [] for k, _, _ in m.CATEGORIES}
    if flagged:
        f["unattached_volumes"] = [{"id": "vol-%d" % i, "size_gb": 1, "type": "gp3", "created": "t",
                                    "estimated_monthly_usd": usd / flagged} for i in range(flagged)]
    f["estimated_monthly_usd"] = usd
    return f


class TestMainRegionsAndErrors(unittest.TestCase):
    def setUp(self):
        self.m = load_tool()

    def test_all_regions_starts_from_the_first_given_region_else_the_profile_region(self):
        seen = []

        def regions(session, region):
            seen.append(region)
            return ["a-1"]
        find = lambda *a, **k: findings_for(self.m)
        run_main(self.m, ["--all-regions", "--region", "eu-west-1", "--region", "x"], find=find, regions=regions)
        run_main(self.m, ["--all-regions"], find=find, regions=regions)
        s = Session()
        s.region_name = None
        run_main(self.m, ["--all-regions"], session=s, find=find, regions=regions)
        self.assertEqual(seen, ["eu-west-1", "us-east-1", None])

    def test_every_region_failing_is_exit_2_naming_each_region_and_its_error(self):
        def find(session, region, *a):
            raise client_error("Code-" + region, "Msg " + region, "DescribeVolumes")
        code, out, err = run_main(self.m, ["--region", "r2", "--region", "r1"], find=find)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "ERROR: every region failed: r1 (Code-r1: Msg r1); r2 (Code-r2: Msg r2)\n")

    def test_a_region_error_without_code_or_message_is_unknown_with_the_exception_text(self):
        bad = client_error(None, None, "DescribeVolumes")
        no_error_key = botocore.exceptions.ClientError({}, "DescribeVolumes")

        def find(session, region, *a):
            if region == "good":
                return findings_for(self.m)
            raise bad if region == "bad1" else no_error_key
        code, out, err = run_main(self.m, ["--json", "--region", "good", "--region", "bad1", "--region", "bad2"], find=find)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["region_errors"],
                         {"bad1": "Unknown: " + str(bad), "bad2": "Unknown: " + str(no_error_key)})

    def test_region_errors_are_printed_in_the_human_report(self):
        def find(session, region, *a):
            if region == "good":
                return findings_for(self.m)
            raise client_error("AccessDenied", "no way", "DescribeVolumes")
        code, out, err = run_main(self.m, ["--region", "good", "--region", "bad"], find=find)
        self.assertEqual(code, 0)
        self.assertIn("\n##### bad: scan FAILED (AccessDenied: no way)\n", out)

    def test_single_region_client_error_is_exit_2_with_code_and_message(self):
        def find(*a):
            raise client_error("AccessDenied", "not allowed", "DescribeVolumes")
        code, out, err = run_main(self.m, ["--region", "r"], find=find)
        self.assertEqual((code, out, err), (2, "", "ERROR: AWS API call failed (AccessDenied): not allowed\n"))

    def test_single_region_client_error_without_code_or_message(self):
        bad = client_error(None, None, "DescribeVolumes")

        def find(*a):
            raise bad
        code, out, err = run_main(self.m, ["--region", "r"], find=find)
        self.assertEqual((code, out, err), (2, "", "ERROR: AWS API call failed (Unknown): %s\n" % bad))

    def test_no_credentials_and_no_region_messages(self):
        def find(*a):
            raise botocore.exceptions.NoCredentialsError()
        code, out, err = run_main(self.m, ["--region", "r"], find=find)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "ERROR: no AWS credentials found. Configure them first, e.g. "
                              "`aws configure` (or --profile NAME / env vars / an instance role).\n")
        s = Session()
        s.region_name = None
        code, out, err = run_main(self.m, [], session=s, find=lambda *a: findings_for(self.m))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "ERROR: no AWS region configured. Pass --region us-east-1 (or whichever region you use), "
                              "--all-regions, or set one in your AWS profile/config/env (AWS_DEFAULT_REGION).\n")


class TestMainOutput(unittest.TestCase):
    def setUp(self):
        self.m = load_tool()

    def test_total_line_and_exit_code(self):
        code, out, err = run_main(self.m, ["--region", "r"], find=lambda *a: findings_for(self.m, 12.5, 2))
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("\nTotal: 2 finding(s), estimated waste ~$12.50/month across 1 region(s) "
                                     "(us-east-1 list prices; RDS instance hours not included)\n"), out[-200:])

    def test_min_savings_threshold(self):
        find = lambda *a: findings_for(self.m, 10.0, 1)
        self.assertEqual(run_main(self.m, ["--region", "r", "--min-savings", "10"], find=find)[0], 1)
        self.assertEqual(run_main(self.m, ["--region", "r", "--min-savings", "10.01"], find=find)[0], 0)

    def test_help_body(self):
        env = dict(os.environ, COLUMNS="80")
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                self.m._main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = re.sub(r"^(options|optional arguments):", "options:", out.getvalue(), flags=re.M)
        self.assertEqual(" ".join(text.split()), HELP)


HELP = (
    'usage: aws-waste-finder [-h] [--version] [--profile PROFILE] [--region REGION] [--all-regions] '
    '[--days DAYS] [--idle-days IDLE_DAYS] [--min-savings USD] [--prices FILE] [--json] [-q] Find common '
    'silent AWS cost drains (read-only, report only). options: -h, --help show this help message and exit '
    "--version show program's version number and exit --profile PROFILE AWS named profile (default: "
    'default credentials chain) --region REGION AWS region to scan (repeatable; default: from '
    'profile/env) --all-regions scan every region enabled for the account --days DAYS Flag snapshots '
    'older than N days (default: 90) --idle-days IDLE_DAYS RDS with no connections for N days counts as '
    'idle (default: 7) --min-savings USD exit 1 only when the estimated monthly waste is at least USD '
    '(default: any finding) --prices FILE JSON file overriding the bundled us-east-1 prices --json Output '
    'JSON instead of a human report -q, --quiet print nothing on stdout; the exit code (and errors on '
    'stderr) are the result Exit codes: 0 nothing found, 1 waste found, 2 usage or configuration error or '
    'every region failed, 3 unexpected error.'
)
