# AWS Waste Finder

A single-file, read-only Python script that finds the most common silent
AWS cost drains in one region, several, or **every enabled region**, with an
**estimated monthly cost** for each finding and a total:

| Finding | Estimate (us-east-1 list price) |
|---|---|
| Unattached EBS volumes | size x $/GB-month for the volume type |
| gp2 volumes (gp3 is ~20% cheaper) | size x $0.02 |
| Unassociated Elastic IPs | $0.005/h = $3.65/mo |
| Stopped EC2 instances (EBS still billed) | their volumes |
| Old EBS snapshots (default > 90 days; AMI-backed ones are marked) | size x $0.05 (upper bound: snapshots are incremental) |
| AMIs you own that no instance uses | their snapshots |
| NAT Gateways | $0.045/h = $32.85/mo + data |
| Idle ALB/NLB/GWLB (no healthy targets) and Classic ELBs (no InService instances) | hourly LB price |
| RDS instances with **no connections** for `--idle-days` (CloudWatch) | storage shown; instance hours depend on class/engine |
| Unattached network interfaces, empty log groups, log groups with no retention | informational |

Other RDS instances are listed for manual review only, never auto-flagged.

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

## Install

```sh
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/aws-waste-finder.git        # latest release branch; puts `aws-waste-finder` on PATH
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/aws-waste-finder.git@v1.2.1  # a pinned release
brew install canutethegreat/errant-tools/aws-waste-finder   # after: brew tap canutethegreat/errant-tools https://git.canutethegreat.com/CanuteTheGreat/homebrew-errant-tools
scoop install errant-tools/aws-waste-finder                 # after: scoop bucket add errant-tools https://git.canutethegreat.com/CanuteTheGreat/scoop-errant-tools
```

Or download the single file (stdlib-only Python 3.8+; needs `boto3`) and run it directly:
`curl -fsSLO https://errant.solutions/downloads/aws_waste_finder.py && python3 aws_waste_finder.py --help`.
Every [release](https://git.canutethegreat.com/CanuteTheGreat/aws-waste-finder/releases) has a changelog, the wheel, the sdist and a `SHA256SUMS` file.
Not on PyPI yet, so plain `pipx install aws-waste-finder` doesn't work until it's published there.

## Usage

```bash
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/aws-waste-finder.git   # brings boto3 along
aws-waste-finder --profile myprofile --region us-west-2
aws-waste-finder --region us-east-1 --region eu-west-1
aws-waste-finder --all-regions --json
aws-waste-finder --all-regions --min-savings 50     # CI/cron: exit 1 only for >= $50/month of waste
```

Options:
- `--profile NAME` — AWS named profile (default: default credentials chain)
- `--region REGION` — region to scan, repeatable (default: from profile/env)
- `--all-regions` — every region enabled for the account (scanned in parallel)
- `--days N` — flag snapshots older than N days (default: 90)
- `--idle-days N` — RDS with no connections for N days is idle (default: 7)
- `--min-savings USD` — exit 1 only when the estimated monthly total is at least USD
- `--prices FILE` — JSON overriding the bundled prices (same keys as `PRICES` in the script), e.g. your region's
- `--json` — `{"version", "regions": {region: findings}, "region_errors", "estimated_monthly_usd"}`

Exit codes: `0` nothing flagged (informational categories and RDS review entries don't count, nor does
waste below `--min-savings`), `1` waste found, `2` AWS can't be reached or isn't configured (no region, no
credentials, unknown profile, API error, or every requested region failed) -- with a one-line explanation,
not a traceback. A region that fails during `--all-regions` is listed in `region_errors` and the others are
still reported; a single check that fails (e.g. a missing IAM permission) is shown as "check FAILED" in its
section instead of "(none found)". Every listing is fully paginated.

## Requires

- Python 3.8+
- `boto3` (the only dependency; pipx/Homebrew install it for you)
- Read-only IAM permissions: `ec2:Describe*`, `elasticloadbalancing:Describe*`, `rds:Describe*`,
  `cloudwatch:GetMetricStatistics`, `logs:DescribeLogGroups`

## What it does NOT do

- Never deletes, stops, or modifies any resource — report only.
- Never guesses about busy RDS instances — only ones with zero connections are flagged.
- Prices are bundled us-east-1 on-demand list prices; other regions, Savings Plans and
  reserved capacity change the real number (use `--prices`).

## Support this work

This tool is free. If it saved you money and you want to say thanks:
[pay what you want via Stripe](https://buy.stripe.com/5kQ28rdyp2Z98LtaNW1VK04).

## License

MIT — see LICENSE.

## Tests

`tests/` exercises every option and check end-to-end (against moto's AWS API server) in Forgejo CI on
Python 3.8 and current Python; `sh ci/run-tests.sh 3.13` reproduces it locally (needs docker).
