#!/usr/bin/env python3
"""
AWS Waste Finder — a single-file, dependency-free-except-boto3 script that
finds the most common silent AWS cost drains in one account/region:

  - Unattached EBS volumes (billed with nothing attached)
  - Unassociated Elastic IPs (billed hourly once unattached)
  - Stopped EC2 instances (still billed for attached EBS storage)
  - Old EBS snapshots (default: >90 days, often forgotten)
  - NAT Gateways (a classic silent ~$32/mo-each cost driver)
  - Idle/orphaned Load Balancers (no registered healthy targets)
  - Oversized/idle RDS instances (flagged for manual review, not deleted)

Nothing is deleted or modified — this is read-only, report-only. By design
it never calls a mutating API. Point it at a profile/region and get a plain
report of what's probably costing you money for nothing.

By Errant Solutions (errant.solutions), makers of Rustinion
(https://rustinion.com, device provisioning & monitoring), Big Double D
(https://oops.codes, security awareness training), and SSHerpa
(https://ssherpa.app, Android SSH client).

Usage:
    python3 aws_waste_finder.py [--profile NAME] [--region REGION] [--days N] [--json]

Requires: boto3 (pip install boto3), AWS credentials configured normally
(profile, env vars, or instance role). Needs read-only IAM permissions:
ec2:Describe*, elasticloadbalancing:Describe*, rds:Describe*.

License: MIT (see LICENSE)
"""
import argparse
import datetime
import json
import sys

try:
    import boto3
except ImportError:
    print("This tool requires boto3. Install with: pip install boto3", file=sys.stderr)
    sys.exit(2)


def find_waste(profile, region, snapshot_days):
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    ec2 = session.client("ec2")
    elbv2 = session.client("elbv2")
    rds = session.client("rds")

    findings = {
        "unattached_volumes": [],
        "unassociated_eips": [],
        "stopped_instances": [],
        "old_snapshots": [],
        "nat_gateways": [],
        "idle_load_balancers": [],
        "rds_instances_for_review": [],
    }

    # Unattached EBS volumes
    for v in ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]:
        findings["unattached_volumes"].append({
            "id": v["VolumeId"], "size_gb": v["Size"], "type": v["VolumeType"],
            "created": v["CreateTime"].isoformat(),
        })

    # Unassociated Elastic IPs
    for a in ec2.describe_addresses()["Addresses"]:
        if "AssociationId" not in a:
            findings["unassociated_eips"].append({
                "ip": a.get("PublicIp"), "allocation_id": a.get("AllocationId"),
            })

    # Stopped instances (still billed for EBS)
    for r in ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])["Reservations"]:
        for i in r["Instances"]:
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)
            findings["stopped_instances"].append({
                "id": i["InstanceId"], "type": i["InstanceType"], "name": name,
            })

    # Old snapshots
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=snapshot_days)
    for s in ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]:
        if s["StartTime"] <= cutoff:
            findings["old_snapshots"].append({
                "id": s["SnapshotId"], "size_gb": s["VolumeSize"],
                "started": s["StartTime"].isoformat(), "description": s.get("Description", ""),
            })

    # NAT Gateways
    for n in ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])["NatGateways"]:
        findings["nat_gateways"].append({"id": n["NatGatewayId"], "vpc_id": n["VpcId"]})

    # Idle Load Balancers (no healthy targets in any target group)
    try:
        for lb in elbv2.describe_load_balancers()["LoadBalancers"]:
            tgs = elbv2.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
            has_healthy = False
            for tg in tgs:
                health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
                if any(h["TargetHealth"]["State"] == "healthy" for h in health):
                    has_healthy = True
                    break
            if not has_healthy:
                findings["idle_load_balancers"].append({
                    "name": lb["LoadBalancerName"], "type": lb["Type"], "arn": lb["LoadBalancerArn"],
                })
    except Exception as e:
        findings["idle_load_balancers_error"] = str(e)

    # RDS — just list for manual review, never auto-flag as waste (too risky to guess)
    try:
        for db in rds.describe_db_instances()["DBInstances"]:
            findings["rds_instances_for_review"].append({
                "id": db["DBInstanceIdentifier"], "class": db["DBInstanceClass"],
                "storage_gb": db.get("AllocatedStorage"), "multi_az": db.get("MultiAZ"),
            })
    except Exception as e:
        findings["rds_instances_for_review_error"] = str(e)

    return findings


def print_report(findings):
    def section(title, items, fmt):
        print(f"\n=== {title} ({len(items)}) ===")
        if not items:
            print("  (none found)")
            return
        for it in items:
            print("  " + fmt(it))

    section("Unattached EBS volumes", findings["unattached_volumes"],
             lambda v: f"{v['id']}  {v['size_gb']}GB {v['type']}  created {v['created']}")
    section("Unassociated Elastic IPs", findings["unassociated_eips"],
             lambda a: f"{a['ip']}  ({a['allocation_id']})")
    section("Stopped EC2 instances (still billed for EBS)", findings["stopped_instances"],
             lambda i: f"{i['id']}  {i['type']}  name={i['name']}")
    section("Old EBS snapshots", findings["old_snapshots"],
             lambda s: f"{s['id']}  {s['size_gb']}GB  started {s['started']}  {s['description']}")
    section("NAT Gateways (~$32/mo each + data)", findings["nat_gateways"],
             lambda n: f"{n['id']}  vpc={n['vpc_id']}")
    section("Idle Load Balancers (no healthy targets)", findings["idle_load_balancers"],
             lambda l: f"{l['name']}  ({l['type']})")
    section("RDS instances (review manually — never auto-flagged)", findings["rds_instances_for_review"],
             lambda d: f"{d['id']}  {d['class']}  {d['storage_gb']}GB  multi_az={d['multi_az']}")


def main():
    p = argparse.ArgumentParser(description="Find common silent AWS cost drains (read-only, report only).")
    p.add_argument("--profile", default=None, help="AWS named profile (default: default credentials chain)")
    p.add_argument("--region", default=None, help="AWS region (default: from profile/env)")
    p.add_argument("--days", type=int, default=90, help="Flag snapshots older than N days (default: 90)")
    p.add_argument("--json", action="store_true", help="Output JSON instead of a human report")
    args = p.parse_args()

    findings = find_waste(args.profile, args.region, args.days)

    if args.json:
        print(json.dumps(findings, indent=2, default=str))
    else:
        print_report(findings)

    total_flags = sum(len(v) for k, v in findings.items() if isinstance(v, list) and k != "rds_instances_for_review")
    if total_flags > 0:
        sys.exit(1)  # non-zero exit for CI/cron use: "found something worth checking"
    sys.exit(0)


if __name__ == "__main__":
    main()
