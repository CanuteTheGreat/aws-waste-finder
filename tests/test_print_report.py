"""Golden tests for print_report(): the human report's exact text for hand-built findings.

Pins every section title, item format, cost suffix, 'already counted in' note, failed-check
line and region header, so a mutated format string, separator or branch changes the output.
"""
import contextlib
import importlib.util
import io
import os
import unittest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aws_waste_finder.py")


def load_tool():
    spec = importlib.util.spec_from_file_location("aws_waste_finder", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def outputs():
    m = load_tool()
    F = {k: [] for k, _, _ in m.CATEGORIES}
    F["estimated_monthly_usd"] = 1234.5
    F["unattached_volumes"] = [{"id": "vol-1", "size_gb": 100, "type": "gp3", "created": "2026-01-02T03:04:05", "estimated_monthly_usd": 8.0},
                               {"id": "vol-2", "size_gb": 5, "type": "io1", "created": "2025-12-01", "estimated_monthly_usd": None}]
    F["gp2_volumes"] = [{"id": "vol-3", "size_gb": 50, "state": "in-use", "estimated_monthly_usd": 1.0, "counted_in": "unattached_volumes"}]
    F["unassociated_eips"] = [{"ip": "1.2.3.4", "allocation_id": "eipalloc-9", "estimated_monthly_usd": 3.65}]
    F["stopped_instances"] = [{"id": "i-1", "type": "t3.micro", "name": "web", "estimated_monthly_usd": 0}]
    F["old_snapshots"] = [{"id": "snap-1", "size_gb": 20, "started": "2025-01-01", "description": "backup", "estimated_monthly_usd": 1000.0, "used_by_ami": "ami-1"},
                          {"id": "snap-2", "size_gb": 8, "started": "2025-02-02", "description": "", "estimated_monthly_usd": 0.4, "used_by_ami": None, "counted_in": "old_snapshots"}]
    F["unused_amis"] = [{"id": "ami-2", "name": "img", "snapshots": ["snap-a", "snap-b"], "estimated_monthly_usd": 2.0},
                        {"id": "ami-3", "name": "empty", "snapshots": [], "estimated_monthly_usd": None}]
    F["nat_gateways"] = [{"id": "nat-1", "vpc_id": "vpc-1", "estimated_monthly_usd": 32.85}]
    F["idle_load_balancers"] = [{"name": "lb1", "type": "application", "estimated_monthly_usd": 16.2}]
    F["idle_rds_instances"] = [{"id": "db1", "class": "db.t3.micro", "storage_gb": 20, "idle_days": 7, "storage_monthly_usd": 2.3, "estimated_monthly_usd": 2.3}]
    F["rds_instances_for_review"] = [{"id": "db2", "class": "db.m5.large", "storage_gb": 100, "multi_az": True, "max_connections": 3, "note": "check it", "estimated_monthly_usd": None},
                                     {"id": "db3", "class": "db.t3.small", "storage_gb": 10, "multi_az": False, "estimated_monthly_usd": None}]
    F["unattached_network_interfaces"] = [{"id": "eni-1", "description": "desc", "public_ip": "5.6.7.8", "estimated_monthly_usd": 3.6},
                                          {"id": "eni-2", "description": "d2", "public_ip": None, "estimated_monthly_usd": None}]
    F["empty_log_groups"] = [{"name": "/aws/empty", "estimated_monthly_usd": None}]
    F["log_groups_without_retention"] = [{"name": "/aws/big", "stored_gb": 12.5, "estimated_monthly_usd": 0.5}]
    def cap(f, region=None):
        b = io.StringIO()
        with contextlib.redirect_stdout(b): m.print_report(f, region)
        return b.getvalue()
    E = dict(F)
    for k in ["unattached_volumes","unassociated_eips","stopped_instances","old_snapshots","nat_gateways","idle_load_balancers","idle_rds_instances","unattached_network_interfaces","empty_log_groups"]:
        E[k+"_error"] = "Err-"+k
    E2 = {k: [] for k, _, _ in m.CATEGORIES}
    for k in ["unattached_volumes","gp2_volumes","unassociated_eips","stopped_instances","old_snapshots","unused_amis","nat_gateways","idle_load_balancers","idle_rds_instances","rds_instances_for_review","unattached_network_interfaces","empty_log_groups","log_groups_without_retention"]:
        E2[k+"_error"] = "Err-"+k
    E2["estimated_monthly_usd"] = None
    return {"FULL": cap(F), "REGION": cap(F, "eu-west-1"), "ERR_ITEMS": cap(dict(E, **{"empty_log_groups": F["empty_log_groups"]}), "us-east-2"), "ERR_EMPTY": cap(E2, "ap-1"), "EMPTY": cap({k: [] for k, _, _ in m.CATEGORIES})}


GOLDEN = {
    'FULL': (
        '\n'
        '=== Unattached EBS volumes (2) ===\n'
        '  vol-1  100GB gp3  created 2026-01-02T03:04:05   ~$8.00/mo\n'
        '  vol-2  5GB io1  created 2025-12-01\n'
        '\n'
        '=== gp2 volumes (gp3 is ~20% cheaper for the same size) (1) ===\n'
        '  vol-3  50GB gp2 (in-use) -> gp3 saves   ~$1.00/mo (already counted in unattached volumes)\n'
        '\n'
        '=== Unassociated Elastic IPs (1) ===\n'
        '  1.2.3.4  (eipalloc-9)   ~$3.65/mo\n'
        '\n'
        '=== Stopped EC2 instances (still billed for EBS) (1) ===\n'
        '  i-1  t3.micro  name=web\n'
        '\n'
        '=== Old EBS snapshots (2) ===\n'
        '  snap-1  20GB  started 2025-01-01  backup  (AMI ami-1)   ~$1,000.00/mo\n'
        '  snap-2  8GB  started 2025-02-02     ~$0.40/mo (already counted in old snapshots)\n'
        '\n'
        '=== AMIs no instance uses (their snapshots keep billing) (2) ===\n'
        '  ami-2  img  snapshots=snap-a,snap-b   ~$2.00/mo\n'
        '  ami-3  empty  snapshots=-\n'
        '\n'
        '=== NAT Gateways (~$33/mo each + data) (1) ===\n'
        '  nat-1  vpc=vpc-1   ~$32.85/mo\n'
        '\n'
        '=== Idle Load Balancers (no healthy targets / InService instances) (1) ===\n'
        '  lb1  (application)   ~$16.20/mo\n'
        '\n'
        '=== RDS instances with no connections (1) ===\n'
        '  db1  db.t3.micro  20GB  0 connections in 7d  (storage ~$2.30/mo + instance hours)   ~$2.30/mo\n'
        '\n'
        '=== Other RDS instances (review manually) (2) ===\n'
        '  db2  db.m5.large  100GB  multi_az=True  peak connections=3  (check it)\n'
        '  db3  db.t3.small  10GB  multi_az=False  peak connections=None\n'
        '\n'
        '=== Unattached network interfaces (2) ===\n'
        '  eni-1  desc  public 5.6.7.8   ~$3.60/mo\n'
        '  eni-2  d2\n'
        '\n'
        '=== Empty CloudWatch log groups (1) ===\n'
        '  /aws/empty\n'
        '\n'
        '=== Log groups keeping data forever (no retention) (1) ===\n'
        '  /aws/big  12.5GB   ~$0.50/mo\n'
    ),
    'REGION': (
        '\n'
        '##### eu-west-1  (estimated waste ~$1,234.50/month)\n'
        '\n'
        '=== Unattached EBS volumes (2) ===\n'
        '  vol-1  100GB gp3  created 2026-01-02T03:04:05   ~$8.00/mo\n'
        '  vol-2  5GB io1  created 2025-12-01\n'
        '\n'
        '=== gp2 volumes (gp3 is ~20% cheaper for the same size) (1) ===\n'
        '  vol-3  50GB gp2 (in-use) -> gp3 saves   ~$1.00/mo (already counted in unattached volumes)\n'
        '\n'
        '=== Unassociated Elastic IPs (1) ===\n'
        '  1.2.3.4  (eipalloc-9)   ~$3.65/mo\n'
        '\n'
        '=== Stopped EC2 instances (still billed for EBS) (1) ===\n'
        '  i-1  t3.micro  name=web\n'
        '\n'
        '=== Old EBS snapshots (2) ===\n'
        '  snap-1  20GB  started 2025-01-01  backup  (AMI ami-1)   ~$1,000.00/mo\n'
        '  snap-2  8GB  started 2025-02-02     ~$0.40/mo (already counted in old snapshots)\n'
        '\n'
        '=== AMIs no instance uses (their snapshots keep billing) (2) ===\n'
        '  ami-2  img  snapshots=snap-a,snap-b   ~$2.00/mo\n'
        '  ami-3  empty  snapshots=-\n'
        '\n'
        '=== NAT Gateways (~$33/mo each + data) (1) ===\n'
        '  nat-1  vpc=vpc-1   ~$32.85/mo\n'
        '\n'
        '=== Idle Load Balancers (no healthy targets / InService instances) (1) ===\n'
        '  lb1  (application)   ~$16.20/mo\n'
        '\n'
        '=== RDS instances with no connections (1) ===\n'
        '  db1  db.t3.micro  20GB  0 connections in 7d  (storage ~$2.30/mo + instance hours)   ~$2.30/mo\n'
        '\n'
        '=== Other RDS instances (review manually) (2) ===\n'
        '  db2  db.m5.large  100GB  multi_az=True  peak connections=3  (check it)\n'
        '  db3  db.t3.small  10GB  multi_az=False  peak connections=None\n'
        '\n'
        '=== Unattached network interfaces (2) ===\n'
        '  eni-1  desc  public 5.6.7.8   ~$3.60/mo\n'
        '  eni-2  d2\n'
        '\n'
        '=== Empty CloudWatch log groups (1) ===\n'
        '  /aws/empty\n'
        '\n'
        '=== Log groups keeping data forever (no retention) (1) ===\n'
        '  /aws/big  12.5GB   ~$0.50/mo\n'
    ),
    'ERR_ITEMS': (
        '\n'
        '##### us-east-2  (estimated waste ~$1,234.50/month)\n'
        '\n'
        '=== Unattached EBS volumes (2) ===\n'
        '  (check FAILED, results incomplete: Err-unattached_volumes)\n'
        '  vol-1  100GB gp3  created 2026-01-02T03:04:05   ~$8.00/mo\n'
        '  vol-2  5GB io1  created 2025-12-01\n'
        '\n'
        '=== gp2 volumes (gp3 is ~20% cheaper for the same size) (1) ===\n'
        '  vol-3  50GB gp2 (in-use) -> gp3 saves   ~$1.00/mo (already counted in unattached volumes)\n'
        '\n'
        '=== Unassociated Elastic IPs (1) ===\n'
        '  (check FAILED, results incomplete: Err-unassociated_eips)\n'
        '  1.2.3.4  (eipalloc-9)   ~$3.65/mo\n'
        '\n'
        '=== Stopped EC2 instances (still billed for EBS) (1) ===\n'
        '  (check FAILED, results incomplete: Err-stopped_instances)\n'
        '  i-1  t3.micro  name=web\n'
        '\n'
        '=== Old EBS snapshots (2) ===\n'
        '  (check FAILED, results incomplete: Err-old_snapshots)\n'
        '  snap-1  20GB  started 2025-01-01  backup  (AMI ami-1)   ~$1,000.00/mo\n'
        '  snap-2  8GB  started 2025-02-02     ~$0.40/mo (already counted in old snapshots)\n'
        '\n'
        '=== AMIs no instance uses (their snapshots keep billing) (2) ===\n'
        '  (check FAILED, results incomplete: Err-old_snapshots)\n'
        '  ami-2  img  snapshots=snap-a,snap-b   ~$2.00/mo\n'
        '  ami-3  empty  snapshots=-\n'
        '\n'
        '=== NAT Gateways (~$33/mo each + data) (1) ===\n'
        '  (check FAILED, results incomplete: Err-nat_gateways)\n'
        '  nat-1  vpc=vpc-1   ~$32.85/mo\n'
        '\n'
        '=== Idle Load Balancers (no healthy targets / InService instances) (1) ===\n'
        '  (check FAILED, results incomplete: Err-idle_load_balancers)\n'
        '  lb1  (application)   ~$16.20/mo\n'
        '\n'
        '=== RDS instances with no connections (1) ===\n'
        '  (check FAILED, results incomplete: Err-idle_rds_instances)\n'
        '  db1  db.t3.micro  20GB  0 connections in 7d  (storage ~$2.30/mo + instance hours)   ~$2.30/mo\n'
        '\n'
        '=== Other RDS instances (review manually) (2) ===\n'
        '  (check FAILED, results incomplete: Err-idle_rds_instances)\n'
        '  db2  db.m5.large  100GB  multi_az=True  peak connections=3  (check it)\n'
        '  db3  db.t3.small  10GB  multi_az=False  peak connections=None\n'
        '\n'
        '=== Unattached network interfaces (2) ===\n'
        '  (check FAILED, results incomplete: Err-unattached_network_interfaces)\n'
        '  eni-1  desc  public 5.6.7.8   ~$3.60/mo\n'
        '  eni-2  d2\n'
        '\n'
        '=== Empty CloudWatch log groups (1) ===\n'
        '  (check FAILED, results incomplete: Err-empty_log_groups)\n'
        '  /aws/empty\n'
        '\n'
        '=== Log groups keeping data forever (no retention) (1) ===\n'
        '  (check FAILED, results incomplete: Err-empty_log_groups)\n'
        '  /aws/big  12.5GB   ~$0.50/mo\n'
    ),
    'ERR_EMPTY': (
        '\n'
        '##### ap-1  (estimated waste ~$0.00/month)\n'
        '\n'
        '=== Unattached EBS volumes (0) ===\n'
        '  (check FAILED, results incomplete: Err-unattached_volumes)\n'
        '\n'
        '=== gp2 volumes (gp3 is ~20% cheaper for the same size) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Unassociated Elastic IPs (0) ===\n'
        '  (check FAILED, results incomplete: Err-unassociated_eips)\n'
        '\n'
        '=== Stopped EC2 instances (still billed for EBS) (0) ===\n'
        '  (check FAILED, results incomplete: Err-stopped_instances)\n'
        '\n'
        '=== Old EBS snapshots (0) ===\n'
        '  (check FAILED, results incomplete: Err-old_snapshots)\n'
        '\n'
        '=== AMIs no instance uses (their snapshots keep billing) (0) ===\n'
        '  (check FAILED, results incomplete: Err-old_snapshots)\n'
        '\n'
        '=== NAT Gateways (~$33/mo each + data) (0) ===\n'
        '  (check FAILED, results incomplete: Err-nat_gateways)\n'
        '\n'
        '=== Idle Load Balancers (no healthy targets / InService instances) (0) ===\n'
        '  (check FAILED, results incomplete: Err-idle_load_balancers)\n'
        '\n'
        '=== RDS instances with no connections (0) ===\n'
        '  (check FAILED, results incomplete: Err-idle_rds_instances)\n'
        '\n'
        '=== Other RDS instances (review manually) (0) ===\n'
        '  (check FAILED, results incomplete: Err-idle_rds_instances)\n'
        '\n'
        '=== Unattached network interfaces (0) ===\n'
        '  (check FAILED, results incomplete: Err-unattached_network_interfaces)\n'
        '\n'
        '=== Empty CloudWatch log groups (0) ===\n'
        '  (check FAILED, results incomplete: Err-empty_log_groups)\n'
        '\n'
        '=== Log groups keeping data forever (no retention) (0) ===\n'
        '  (check FAILED, results incomplete: Err-empty_log_groups)\n'
    ),
    'EMPTY': (
        '\n'
        '=== Unattached EBS volumes (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== gp2 volumes (gp3 is ~20% cheaper for the same size) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Unassociated Elastic IPs (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Stopped EC2 instances (still billed for EBS) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Old EBS snapshots (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== AMIs no instance uses (their snapshots keep billing) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== NAT Gateways (~$33/mo each + data) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Idle Load Balancers (no healthy targets / InService instances) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== RDS instances with no connections (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Other RDS instances (review manually) (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Unattached network interfaces (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Empty CloudWatch log groups (0) ===\n'
        '  (none found)\n'
        '\n'
        '=== Log groups keeping data forever (no retention) (0) ===\n'
        '  (none found)\n'
    ),
}


class TestPrintReportGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = outputs()

    def check(self, name):
        self.assertEqual(self.out[name], "".join(GOLDEN[name]))

    def test_full_report_without_region(self):
        self.check("FULL")

    def test_region_header_and_total(self):
        self.check("REGION")

    def test_failed_checks_with_items_and_shared_error_keys(self):
        self.check("ERR_ITEMS")

    def test_failed_checks_with_no_items_print_no_none_found(self):
        self.check("ERR_EMPTY")

    def test_empty_report(self):
        self.check("EMPTY")

    def test_region_header_absent_without_region(self):
        self.assertNotIn("#####", self.out["FULL"])
        self.assertTrue(self.out["REGION"].startswith("\n##### eu-west-1  (estimated waste ~$1,234.50/month)\n"))


if __name__ == "__main__":
    unittest.main()
