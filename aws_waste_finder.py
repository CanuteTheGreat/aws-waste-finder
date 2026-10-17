#!/usr/bin/env python3
"""
AWS Waste Finder — a single-file, dependency-free-except-boto3 script that
finds the most common silent AWS cost drains, in one region, several, or
every enabled region, with an estimated monthly cost for each finding:

  - Unattached EBS volumes (billed with nothing attached)
  - gp2 volumes that would be ~20% cheaper as gp3
  - Unassociated Elastic IPs (billed hourly once unattached)
  - Stopped EC2 instances (still billed for attached EBS storage)
  - Old EBS snapshots (default: >90 days, often forgotten)
  - AMIs you own that no instance uses (their snapshots keep billing)
  - NAT Gateways (a classic silent ~$33/mo-each cost driver)
  - Idle load balancers: ALB/NLB/GWLB with no healthy targets and Classic ELBs
    with no InService instances
  - RDS instances with no connections over --idle-days (CloudWatch), the rest
    listed for manual review
  - Unattached network interfaces, empty CloudWatch log groups and log groups
    that keep data forever (hygiene / informational)

Nothing is deleted or modified — this is read-only, report-only. By design
it never calls a mutating API.

Cost estimates use bundled us-east-1 on-demand list prices (see PRICES; other
regions differ by up to ~30%, and snapshot sizes are an upper bound because
snapshots are incremental). Override them with --prices FILE (JSON, same keys).

By Errant Solutions (errant.solutions).

Usage:
    aws-waste-finder [--profile NAME] [--region REGION ...|--all-regions] [--days N] [--json]

Requires: boto3, AWS credentials configured normally (profile, env vars, or
instance role). Needs read-only IAM permissions: ec2:Describe*,
elasticloadbalancing:Describe*, rds:Describe*, cloudwatch:GetMetricStatistics,
logs:DescribeLogGroups.

Exit codes: 0 = nothing found (or total estimated savings below --min-savings),
1 = waste found, 2 = no region/credentials, or every requested region failed.

License: MIT (see LICENSE)
"""
__version__ = "1.2.1"

import argparse
import concurrent.futures
import datetime
import json
import sys

try:
    import boto3
except ImportError:
    print("This tool requires boto3. Install with: pip install boto3", file=sys.stderr)
    sys.exit(2)

HOURS = 730  # AWS bills a month as 730 hours
# us-east-1 on-demand list prices, USD (checked against aws.amazon.com/ebs|vpc|elasticloadbalancing/pricing)
PRICES = {
    "ebs_gb_month": {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015,
                     "standard": 0.05},
    "snapshot_gb_month": 0.05,
    "public_ipv4_hour": 0.005,
    "nat_gateway_hour": 0.045,
    "lb_hour": {"application": 0.0225, "network": 0.0225, "gateway": 0.0125, "classic": 0.025},
    "cloudwatch_logs_gb_month": 0.03,
}
CATEGORIES = [
    # key, title, counts toward the exit code
    ("unattached_volumes", "Unattached EBS volumes", True),
    ("gp2_volumes", "gp2 volumes (gp3 is ~20% cheaper for the same size)", True),
    ("unassociated_eips", "Unassociated Elastic IPs", True),
    ("stopped_instances", "Stopped EC2 instances (still billed for EBS)", True),
    ("old_snapshots", "Old EBS snapshots", True),
    ("unused_amis", "AMIs no instance uses (their snapshots keep billing)", True),
    ("nat_gateways", "NAT Gateways (~$33/mo each + data)", True),
    ("idle_load_balancers", "Idle Load Balancers (no healthy targets / InService instances)", True),
    ("idle_rds_instances", "RDS instances with no connections", True),
    ("rds_instances_for_review", "Other RDS instances (review manually)", False),
    ("unattached_network_interfaces", "Unattached network interfaces", False),
    ("empty_log_groups", "Empty CloudWatch log groups", False),
    ("log_groups_without_retention", "Log groups keeping data forever (no retention)", False),
]


def money(x):
    return None if x is None else round(x, 2)


def pages(client, op, key, **kw):
    """Every item across all result pages (a single Describe call stops at
    the first page -- e.g. 1000 snapshots -- and silently misses the rest)."""
    for page in client.get_paginator(op).paginate(**kw):
        for item in page.get(key, []):
            yield item


def _check(findings, key, fn):
    """Run one check; a failure (e.g. missing IAM permission) is recorded, not fatal."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - reported to the user per check
        import botocore.exceptions
        if isinstance(e, (botocore.exceptions.NoCredentialsError, botocore.exceptions.NoRegionError)):
            raise
        findings[f"{key}_error"] = str(e)


def find_waste(session, region, snapshot_days, idle_days=7, prices=PRICES):
    ec2 = session.client("ec2", region_name=region)
    f = {k: [] for k, _, _ in CATEGORIES}
    ebs = prices["ebs_gb_month"]
    now = datetime.datetime.now(datetime.timezone.utc)

    volumes = list(pages(ec2, "describe_volumes", "Volumes"))  # the first API call: credential/region errors raise here
    vol_by_id = {v["VolumeId"]: v for v in volumes}

    def vol_cost(v):
        return v["Size"] * ebs.get(v["VolumeType"], ebs["gp2"])

    for v in volumes:
        if v["State"] == "available":
            f["unattached_volumes"].append({"id": v["VolumeId"], "size_gb": v["Size"], "type": v["VolumeType"],
                                            "created": v["CreateTime"].isoformat(),
                                            "estimated_monthly_usd": money(vol_cost(v))})
        elif v["VolumeType"] == "gp2":
            f["gp2_volumes"].append({"id": v["VolumeId"], "size_gb": v["Size"], "state": v["State"],
                                     "estimated_monthly_usd": money(v["Size"] * (ebs["gp2"] - ebs["gp3"]))})

    def eips():
        for a in ec2.describe_addresses()["Addresses"]:
            if "AssociationId" not in a:
                f["unassociated_eips"].append({"ip": a.get("PublicIp"), "allocation_id": a.get("AllocationId"),
                                               "estimated_monthly_usd": money(prices["public_ipv4_hour"] * HOURS)})
    _check(f, "unassociated_eips", eips)

    used_images = set()

    def instances():
        for r in pages(ec2, "describe_instances", "Reservations"):
            for i in r["Instances"]:
                state = i["State"]["Name"]
                if state == "terminated":
                    continue
                used_images.add(i.get("ImageId"))
                if state != "stopped":
                    continue
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)
                vols = [m["Ebs"]["VolumeId"] for m in i.get("BlockDeviceMappings", []) if "Ebs" in m]
                cost = sum(vol_cost(vol_by_id[v]) for v in vols if v in vol_by_id)
                f["stopped_instances"].append({"id": i["InstanceId"], "type": i["InstanceType"], "name": name,
                                               "volumes": vols, "estimated_monthly_usd": money(cost)})
    _check(f, "stopped_instances", instances)

    def amis_and_snapshots():
        images = list(ec2.describe_images(Owners=["self"])["Images"])
        snap_to_ami = {}
        for img in images:
            snaps = [m["Ebs"]["SnapshotId"] for m in img.get("BlockDeviceMappings", []) if m.get("Ebs", {}).get("SnapshotId")]
            for s in snaps:
                snap_to_ami[s] = img["ImageId"]
            img["_snaps"] = snaps
        snapshots = list(pages(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]))
        size = {s["SnapshotId"]: s["VolumeSize"] for s in snapshots}
        cutoff = now - datetime.timedelta(days=snapshot_days)
        for s in snapshots:
            if s["StartTime"] <= cutoff:
                f["old_snapshots"].append({
                    "id": s["SnapshotId"], "size_gb": s["VolumeSize"], "started": s["StartTime"].isoformat(),
                    "description": s.get("Description", ""), "used_by_ami": snap_to_ami.get(s["SnapshotId"]),
                    "estimated_monthly_usd": money(s["VolumeSize"] * prices["snapshot_gb_month"])})
        for img in images:
            if img["ImageId"] in used_images:
                continue
            gb = sum(size.get(s, 0) for s in img["_snaps"])
            f["unused_amis"].append({"id": img["ImageId"], "name": img.get("Name"), "snapshots": img["_snaps"],
                                     "created": img.get("CreationDate"),
                                     "estimated_monthly_usd": money(gb * prices["snapshot_gb_month"])})
    _check(f, "old_snapshots", amis_and_snapshots)

    def nat():
        for n in pages(ec2, "describe_nat_gateways", "NatGateways", Filter=[{"Name": "state", "Values": ["available"]}]):
            f["nat_gateways"].append({"id": n["NatGatewayId"], "vpc_id": n["VpcId"],
                                      "estimated_monthly_usd": money(prices["nat_gateway_hour"] * HOURS)})
    _check(f, "nat_gateways", nat)

    def enis():
        for n in pages(ec2, "describe_network_interfaces", "NetworkInterfaces",
                       Filters=[{"Name": "status", "Values": ["available"]}]):
            public = (n.get("Association") or {}).get("PublicIp")
            f["unattached_network_interfaces"].append({
                "id": n["NetworkInterfaceId"], "description": n.get("Description", ""), "public_ip": public,
                "estimated_monthly_usd": money(prices["public_ipv4_hour"] * HOURS if public else 0)})
    _check(f, "unattached_network_interfaces", enis)

    def load_balancers():
        elbv2 = session.client("elbv2", region_name=region)
        for lb in pages(elbv2, "describe_load_balancers", "LoadBalancers"):
            try:
                tgs = list(pages(elbv2, "describe_target_groups", "TargetGroups", LoadBalancerArn=lb["LoadBalancerArn"]))
            except Exception as e:
                # A load balancer with no target groups attached at all is the most
                # idle kind there is; some API implementations answer that with
                # TargetGroupNotFound instead of an empty list.
                if getattr(e, "response", {}).get("Error", {}).get("Code") != "TargetGroupNotFound":
                    raise
                tgs = []
            healthy = any(h["TargetHealth"]["State"] == "healthy" for tg in tgs
                          for h in elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"])
            if not healthy:
                f["idle_load_balancers"].append({
                    "name": lb["LoadBalancerName"], "type": lb["Type"], "arn": lb["LoadBalancerArn"],
                    "estimated_monthly_usd": money(prices["lb_hour"].get(lb["Type"], 0.0225) * HOURS)})
        elb = session.client("elb", region_name=region)
        for lb in pages(elb, "describe_load_balancers", "LoadBalancerDescriptions"):
            states = elb.describe_instance_health(LoadBalancerName=lb["LoadBalancerName"])["InstanceStates"]
            if not any(s["State"] == "InService" for s in states):
                f["idle_load_balancers"].append({
                    "name": lb["LoadBalancerName"], "type": "classic", "arn": None,
                    "estimated_monthly_usd": money(prices["lb_hour"]["classic"] * HOURS)})
    _check(f, "idle_load_balancers", load_balancers)

    def rds_instances():
        rds = session.client("rds", region_name=region)
        cw = session.client("cloudwatch", region_name=region)
        for db in pages(rds, "describe_db_instances", "DBInstances"):
            entry = {"id": db["DBInstanceIdentifier"], "class": db["DBInstanceClass"],
                     "storage_gb": db.get("AllocatedStorage"), "multi_az": db.get("MultiAZ"),
                     "status": db.get("DBInstanceStatus")}
            points = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName="DatabaseConnections",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db["DBInstanceIdentifier"]}],
                StartTime=now - datetime.timedelta(days=idle_days), EndTime=now + datetime.timedelta(hours=1), Period=86400,
                Statistics=["Maximum"])["Datapoints"]
            peak = max((p["Maximum"] for p in points), default=0)
            entry["max_connections"] = peak
            # "no connections for N days" needs metrics that cover the N days: a database created
            # yesterday, or one CloudWatch has no data for, has not been idle for N days
            first = min((p["Timestamp"] for p in points), default=None)
            covered = first is not None and first <= now - datetime.timedelta(days=max(idle_days - 1, 0))
            if peak == 0 and not covered:
                entry["note"] = ("no DatabaseConnections metrics" if first is None else
                                 "metrics only cover %.1f day(s)" % ((now - first).total_seconds() / 86400)) + \
                                " of the last %d" % idle_days
            if peak == 0 and covered and db.get("DBInstanceStatus") == "available":
                storage = (db.get("AllocatedStorage") or 0) * ebs["gp2"]
                entry["estimated_monthly_usd"] = None  # instance hours depend on class/engine: see the RDS price list
                entry["storage_monthly_usd"] = money(storage)
                entry["idle_days"] = idle_days
                f["idle_rds_instances"].append(entry)
            else:
                f["rds_instances_for_review"].append(entry)
    _check(f, "idle_rds_instances", rds_instances)

    def log_groups():
        logs = session.client("logs", region_name=region)
        for g in pages(logs, "describe_log_groups", "logGroups"):
            stored = g.get("storedBytes", 0) or 0
            if stored == 0:
                f["empty_log_groups"].append({"name": g["logGroupName"], "estimated_monthly_usd": 0})
            elif not g.get("retentionInDays"):
                f["log_groups_without_retention"].append({
                    "name": g["logGroupName"], "stored_gb": round(stored / 1e9, 3),
                    "estimated_monthly_usd": money(stored / 1e9 * prices["cloudwatch_logs_gb_month"])})
    _check(f, "empty_log_groups", log_groups)

    # the same dollars must not be counted twice in the total: a stopped instance's gp2 volume
    # (its whole cost is already in stopped_instances) and an old snapshot that belongs to an
    # unused AMI (already in unused_amis) stay listed but are marked and left out of the sum
    stopped_vols = {v for i in f["stopped_instances"] for v in i["volumes"]}
    for v in f["gp2_volumes"]:
        if v["id"] in stopped_vols:
            v["counted_in"] = "stopped_instances"
    ami_snaps = {s for a in f["unused_amis"] for s in a["snapshots"]}
    for snap in f["old_snapshots"]:
        if snap["id"] in ami_snaps:
            snap["counted_in"] = "unused_amis"
    f["estimated_monthly_usd"] = money(sum(i.get("estimated_monthly_usd") or 0
                                           for k, _, gate in CATEGORIES if gate for i in f[k]
                                           if "counted_in" not in i))
    return f


def enabled_regions(session, region):
    ec2 = session.client("ec2", region_name=region or "us-east-1")
    regs = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    return sorted(r["RegionName"] for r in regs["Regions"])


def print_report(findings, region=None):
    if region:
        print(f"\n##### {region}  (estimated waste ~${findings.get('estimated_monthly_usd') or 0:,.2f}/month)")

    def section(title, items, fmt, key=None):
        print(f"\n=== {title} ({len(items)}) ===")
        err = findings.get(f"{key}_error") if key else None
        if err:
            # The check itself failed (e.g. missing IAM permission): say so instead
            # of printing "(none found)", which would read as a clean result.
            print(f"  (check FAILED, results incomplete: {err})")
        if not items:
            if not err:
                print("  (none found)")
            return
        for it in items:
            cost = it.get("estimated_monthly_usd")
            note = f" (already counted in {it['counted_in'].replace('_', ' ')})" if it.get("counted_in") else ""
            print("  " + fmt(it) + (f"   ~${cost:,.2f}/mo{note}" if cost else ""))

    fmts = {
        "unattached_volumes": lambda v: f"{v['id']}  {v['size_gb']}GB {v['type']}  created {v['created']}",
        "gp2_volumes": lambda v: f"{v['id']}  {v['size_gb']}GB gp2 ({v['state']}) -> gp3 saves",
        "unassociated_eips": lambda a: f"{a['ip']}  ({a['allocation_id']})",
        "stopped_instances": lambda i: f"{i['id']}  {i['type']}  name={i['name']}",
        "old_snapshots": lambda s: f"{s['id']}  {s['size_gb']}GB  started {s['started']}  {s['description']}"
                                   + (f"  (AMI {s['used_by_ami']})" if s.get("used_by_ami") else ""),
        "unused_amis": lambda a: f"{a['id']}  {a['name']}  snapshots={','.join(a['snapshots']) or '-'}",
        "nat_gateways": lambda n: f"{n['id']}  vpc={n['vpc_id']}",
        "idle_load_balancers": lambda lb: f"{lb['name']}  ({lb['type']})",
        "idle_rds_instances": lambda d: f"{d['id']}  {d['class']}  {d['storage_gb']}GB  0 connections in {d['idle_days']}d"
                                        f"  (storage ~${d['storage_monthly_usd']:,.2f}/mo + instance hours)",
        "rds_instances_for_review": lambda d: f"{d['id']}  {d['class']}  {d['storage_gb']}GB  multi_az={d['multi_az']}"
                                              f"  peak connections={d.get('max_connections')}"
                                              + (f"  ({d['note']})" if d.get("note") else ""),
        "unattached_network_interfaces": lambda n: f"{n['id']}  {n['description']}" + (f"  public {n['public_ip']}" if n.get("public_ip") else ""),
        "empty_log_groups": lambda g: g["name"],
        "log_groups_without_retention": lambda g: f"{g['name']}  {g['stored_gb']}GB",
    }
    errkey = {"gp2_volumes": None, "unused_amis": "old_snapshots", "rds_instances_for_review": "idle_rds_instances",
              "log_groups_without_retention": "empty_log_groups"}
    for key, title, _ in CATEGORIES:
        section(title, findings[key], fmts[key], key=errkey.get(key, key))


def main(argv=None):
    p = argparse.ArgumentParser(prog="aws-waste-finder",
                                description="Find common silent AWS cost drains (read-only, report only).",
                                epilog="Exit codes: 0 nothing found, 1 waste found, 2 configuration error or every region failed.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("--profile", default=None, help="AWS named profile (default: default credentials chain)")
    p.add_argument("--region", action="append", default=None,
                   help="AWS region to scan (repeatable; default: from profile/env)")
    p.add_argument("--all-regions", action="store_true", help="scan every region enabled for the account")
    p.add_argument("--days", type=int, default=90, help="Flag snapshots older than N days (default: 90)")
    p.add_argument("--idle-days", type=int, default=7,
                   help="RDS with no connections for N days counts as idle (default: 7)")
    p.add_argument("--min-savings", type=float, default=0, metavar="USD",
                   help="exit 1 only when the estimated monthly waste is at least USD (default: any finding)")
    p.add_argument("--prices", metavar="FILE", help="JSON file overriding the bundled us-east-1 prices")
    p.add_argument("--json", action="store_true", help="Output JSON instead of a human report")
    args = p.parse_args(argv)

    prices = PRICES
    if args.prices:
        try:
            with open(args.prices) as fh:
                prices = dict(PRICES, **json.load(fh))
        except (OSError, ValueError) as e:
            p.error(f"--prices: {e}")

    import botocore.exceptions
    try:
        session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
        if args.all_regions:
            regions = enabled_regions(session, (args.region or [None])[0] or session.region_name)
        else:
            regions = args.region or [session.region_name]
        if not regions or regions == [None]:
            raise botocore.exceptions.NoRegionError()

        def scan(region):
            return find_waste(session, region, args.days, args.idle_days, prices)

        results, errors = {}, {}
        if len(regions) == 1:
            results[regions[0]] = scan(regions[0])
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(scan, r): r for r in regions}
                for fut in concurrent.futures.as_completed(futs):
                    r = futs[fut]
                    try:
                        results[r] = fut.result()
                    except botocore.exceptions.ClientError as e:
                        errors[r] = "%s: %s" % (e.response.get("Error", {}).get("Code", "Unknown"),
                                                e.response.get("Error", {}).get("Message", str(e)))
            if not results:
                raise RuntimeError("every region failed: " + "; ".join(f"{r} ({e})" for r, e in sorted(errors.items())))
    except botocore.exceptions.NoRegionError:
        print("ERROR: no AWS region configured. Pass --region us-east-1 (or whichever region you use), "
              "--all-regions, or set one in your AWS profile/config/env (AWS_DEFAULT_REGION).", file=sys.stderr)
        return 2
    except botocore.exceptions.NoCredentialsError:
        print("ERROR: no AWS credentials found. Configure them first, e.g. "
              "`aws configure` (or --profile NAME / env vars / an instance role).", file=sys.stderr)
        return 2
    except botocore.exceptions.ProfileNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        msg = e.response.get("Error", {}).get("Message", str(e))
        print(f"ERROR: AWS API call failed ({code}): {msg}", file=sys.stderr)
        return 2
    except botocore.exceptions.BotoCoreError as e:
        print(f"ERROR: AWS SDK error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    total = money(sum(r["estimated_monthly_usd"] or 0 for r in results.values()))
    flagged = sum(len(r[k]) for r in results.values() for k, _, gate in CATEGORIES if gate)
    out = {"version": __version__, "regions": {k: results[k] for k in sorted(results)},
           "region_errors": errors, "estimated_monthly_usd": total,
           "prices": "bundled us-east-1 list prices" if prices is PRICES else "from " + args.prices}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        for r in sorted(results):
            print_report(results[r], r)
        for r, e in sorted(errors.items()):
            print(f"\n##### {r}: scan FAILED ({e})")
        print(f"\nTotal: {flagged} finding(s), estimated waste ~${total:,.2f}/month across {len(results)} region(s) "
              "(us-east-1 list prices; RDS instance hours not included)")
    return 1 if flagged and total >= args.min_savings else 0


if __name__ == "__main__":
    sys.exit(main())
