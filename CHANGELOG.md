# Changelog

All notable changes. Versions are git tags; each tag has a Forgejo release with checksums.

## 1.2.1 - 2026-10-17

- The estimated total no longer counts the same dollars twice: a stopped instance's gp2 volume (already in the stopped instance's cost) and an old snapshot that belongs to an unused AMI (already in the AMI's cost) stay listed, marked `counted_in`, but are left out of the sum.
- An RDS instance is only reported idle when its CloudWatch DatabaseConnections metrics cover the whole `--idle-days` window and are all zero; a database with no metrics yet (e.g. created yesterday) goes to the review list with a note instead of being flagged.
- Tests: accuracy suite (a mixed account where the flagged IDs must match exactly and the total must equal the list-price sum; read-only API/IAM policy check), mutation testing (nightly), claims ledger.

## 1.2.0 - 2026-10-16

- **Multi-region**: `--region` is repeatable and `--all-regions` scans every enabled region in parallel; a failing
  region is reported in `region_errors` without stopping the others.
- **Estimated monthly cost** on every finding and in total (bundled us-east-1 list prices, `--prices` to
  override) and `--min-savings` as a CI/cron gate.
- New checks: gp2 -> gp3 candidates, AMIs no instance uses, Classic ELBs with no InService instances, RDS with no
  connections over `--idle-days` (CloudWatch), unattached network interfaces, empty log groups and log groups
  without retention. Old snapshots note which AMI uses them; stopped instances list their volumes.
- **Breaking:** `--json` output is now `{"version", "regions": {region: findings}, "region_errors",
  "estimated_monthly_usd"}`; RDS instances with zero connections are now flagged (exit 1) instead of only listed.
- Installable as a package: `pipx install git+https://git.canutethegreat.com/CanuteTheGreat/aws-waste-finder.git`
  puts a `aws-waste-finder` command on PATH (pyproject.toml, MIT license metadata).
- `--version` flag.
- Exit codes follow the shared convention: 0 = clean, 1 = findings at/above the gate, 2 = error or bad usage.
- Every tag now publishes a Forgejo release with this changelog, the wheel, the sdist and SHA256SUMS.
- CI installs the package with pip in a clean venv and checks `--version`/`--help` on every push.

## 1.1.0 - 2026-10-16

- Don't let one target-group-less load balancer abort the idle-LB check; show failed checks in the text report
- Fix NAT gateway query that crashed every run, paginate, add CI tests
- Catch NoRegionError/NoCredentialsError/ProfileNotFound with a friendly message instead of a raw traceback

## 1.0.0 - 2026-09-15

- First public release.
