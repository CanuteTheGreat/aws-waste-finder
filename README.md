# AWS Waste Finder

A single-file, read-only Python script that finds the most common silent
AWS cost drains in one account/region: unattached EBS volumes, unassociated
Elastic IPs, stopped-but-still-billed EC2 instances, old snapshots, NAT
Gateways, and idle load balancers. RDS instances are listed for manual
review only — never auto-flagged, since deciding whether an RDS instance
is "wasted" needs human judgment.

By **Errant Solutions** (errant.solutions), makers of [Rustinion](https://rustinion.com)
(device provisioning & monitoring), [/Dev/Out Digital](https://devout.digital) (security
awareness training), [OOPS!](https://oops.codes) (hands-on security labs), and [SSHerpa](https://ssherpa.app) (Android SSH client).

## Why

AWS bills accumulate small silent charges — an EBS volume nobody deleted
after terminating an instance, an Elastic IP sitting unattached, a NAT
Gateway from a torn-down VPC that never got cleaned up. Each one is a few
dollars a month, but they add up, and nobody notices until the bill is
already high. This tool is **read-only** — it never deletes or modifies
anything, it just tells you what to go look at.

We built this after running it against our own AWS account and finding
real waste (see below).

## Usage

```bash
pip install boto3
python3 aws_waste_finder.py --profile myprofile --region us-west-2
```

Options:
- `--profile NAME` — AWS named profile (default: default credentials chain)
- `--region REGION` — AWS region (default: from profile/env)
- `--days N` — flag snapshots older than N days (default: 90)
- `--json` — output JSON instead of a human report (for scripting/CI)

Exit code is non-zero if anything is flagged, so it's safe to drop into a
weekly cron job or CI pipeline and alert on failure.

## Requires

- Python 3.8+
- `boto3` (the only dependency)
- Read-only IAM permissions: `ec2:Describe*`, `elasticloadbalancing:Describe*`, `rds:Describe*`

## What it does NOT do

- Never deletes, stops, or modifies any resource — report only.
- Never guesses whether an RDS instance is "wasted" — lists it for you to
  judge, since that call needs business context a script doesn't have.
- Only checks one region per run — run it once per region you use.

## Support this work

This tool is free. If it saved you money and you want to say thanks:
[pay what you want via Stripe](https://buy.stripe.com/5kQ28rdyp2Z98LtaNW1VK04).

## License

MIT — see LICENSE.
