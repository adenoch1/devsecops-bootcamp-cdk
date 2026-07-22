# DevSecOps Bootcamp — CDK Edition

This is a **parallel AWS CDK (Python) implementation** of the infrastructure
in [`devsecops-bootcamp`](https://github.com/adenoch1/devsecops-bootcamp) —
same Flask application, same security posture and CI gates, same AWS
architecture, different IaC tool. The two repos are independent and
intentionally not kept in perfect lockstep; this one exists to show the same
system built with AWS CDK instead of Terraform.

If you want the Terraform version (the original, more mature repo — weekly
build notes, a live deployment history, OPA/Conftest policy gates): see
[devsecops-bootcamp](https://github.com/adenoch1/devsecops-bootcamp).

---

## Why two repos instead of one

Keeping them separate means either can be run/deployed independently, cloned
independently, and neither README has to explain "ignore this half of the
repo." A reader who wants "how would this look in CDK" gets a clean answer
without wading through Terraform files, and vice versa.

## Architecture (same as the Terraform sibling)

- VPC across 2 AZs, public + private subnets, single NAT gateway, VPC Flow
  Logs to CloudWatch
- ECS Fargate service behind an ALB, HTTPS-only (ACM cert, TLS 1.3), WAFv2
  with 3 AWS managed rule groups, WAF access logs via Kinesis Firehose to S3
- ECR repository, KMS-encrypted, immutable tags, scan-on-push
- CloudWatch dashboard, 4 alarms (app error rate, ALB 5xx, unhealthy
  targets, ECS running-task count), SNS email alerting, a structured-log
  metric filter
- Every S3 bucket: blocked public access, `BucketOwnerEnforced`, SSL-only,
  KMS-encrypted where the AWS service supports it, lifecycle rules

## Repo structure

```
app/            Flask app (identical in spirit to the Terraform sibling —
                dashboard UI, /health, /version, structured JSON logging)
docker/         Dockerfile
cdk/            The CDK app
  app.py        Entry point — wires all 5 stacks together
  config.py     Dev environment config (the CDK equivalent of terraform.tfvars)
  stacks/
    logging_stack.py        KMS keys + S3 log-bucket chain
    network_stack.py        VPC, subnets, NAT, flow logs
    ecr_stack.py             ECR repository
    ecs_stack.py             ALB, WAF, Fargate cluster/service/task, IAM roles
    observability_stack.py   Dashboard, alarms, metric filter, SNS
.github/workflows/
  01-ci.yml                        pytest (copied from the Terraform sibling, unchanged)
  02-security.yml                  Bandit, pip-audit, Trivy fs scan (unchanged)
  03-cdk-synth-security.yml        cdk synth + cdk-nag (the CDK equivalent of
                                   tfsec/Checkov/OPA on the Terraform side)
```

## Terraform → CDK mapping

| Terraform module | CDK stack | Notes |
|---|---|---|
| `infra/modules/network` | `NetworkStack` | 1:1 |
| `infra/modules/logging` | `LoggingStack` | 1:1 |
| `infra/modules/ecr` | `EcrStack` | 1:1 |
| `infra/modules/iam` | folded into `EcsStack` | see below |
| `infra/modules/ecs` | `EcsStack` | 1:1, plus IAM roles |
| `infra/envs/dev/observability.tf` (Week 4) | `ObservabilityStack` | 1:1 |
| `infra/bootstrap/` | *(none — see below)* | not ported |

### Why IAM roles live in `EcsStack`, not a separate stack

The Terraform sibling has a standalone `iam` module. A first pass at this
port had a matching standalone `IamStack` — it doesn't work in CDK. The ECS
log driver's automatic `grantWrite()` call adds an inline policy onto the
execution role referencing the app's CloudWatch log group ARN. If the role
lived in a different stack, that grant would create a second, opposite
cross-stack dependency on top of the one already needed to hand the role
into `EcsStack` — CloudFormation stacks must form a DAG, and CDK rejects the
resulting cycle at synth time. Terraform doesn't hit this because a single
state file has no such per-stack dependency-direction constraint. This is a
real, structural difference between the two tools, not a shortcut.

### What's intentionally not ported

- **`infra/bootstrap/`** (Terraform state bucket, DynamoDB lock table, the
  bootstrap-only SNS topic): CDK bootstraps itself (`cdk bootstrap`) and has
  no equivalent need for a hand-rolled state backend. `ObservabilityStack`
  creates its own SNS topic instead of trying to share the Terraform side's.
- **GitHub OIDC role for CI**: it isn't in the Terraform repo either — it
  was created out-of-band there too. Whoever deploys this needs to create
  one (or reuse an existing one) and wire its ARN into repo secrets.
- **A deploy pipeline** (`cdk deploy` wired into CI, mirroring
  `terraform-release.yml`'s build-image → deploy → health-check flow):
  not built yet. Today, `03-cdk-synth-security.yml` only runs `cdk synth`
  on pull requests — there's no release workflow that actually deploys.
  This is the single largest gap versus the Terraform sibling.

### Known gaps versus the Terraform sibling

- **Default VPC security group lockdown**: the Terraform side explicitly
  empties the VPC's default security group's rules
  (`aws_default_security_group`). CloudFormation has no first-class
  resource for a VPC's implicit default SG — doing this properly needs a
  custom resource calling `ec2:RevokeSecurityGroupIngress/Egress`. Not
  implemented; every workload here gets its own purpose-built SG instead,
  so the default SG being permissive is unused rather than unsafe, but it's
  a real gap.
- **One cdk-nag finding resists suppression**: `AwsSolutions-IAM5` on the
  ECS task execution role's policy, caused by `ecr:GetAuthorizationToken`
  requiring `Resource: '*'` — a hard AWS API constraint true for every AWS
  account, not an actual over-broad grant. All three documented cdk-nag
  suppression mechanisms were tried (see the comment in `ecs_stack.py`);
  none took effect against this specific finding in cdk-nag 2.38.2, despite
  the suppression metadata being correctly attached in the synthesized
  template. `cdk synth`'s exit code 1 for this one line is a known,
  reviewed tooling gap — not an unreviewed security finding.
- Two things fixed *here* that the Terraform sibling doesn't have yet
  (found via cdk-nag while porting, worth backporting): `enforce_ssl` on the
  WAF logs S3 buckets, and Firehose stream-level SSE
  (`DeliveryStreamEncryptionConfigurationInput`) in addition to destination-
  level S3 SSE-KMS.

## Local development

```bash
cd cdk
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cdk synth                       # no AWS credentials needed — nothing here uses context lookups
```

Set `ACM_CERTIFICATE_ARN` and `ALERT_EMAIL` as environment variables before a
real `cdk deploy` (see `cdk/config.py` — deliberately not hardcoded or
committed to keep this reusable and avoid putting a real email address in a
public repo).

## App

Same Flask app as the Terraform sibling: `/health`, `/version` (build
metadata — service, environment, git SHA, image tag, build time, injected
via container environment variables), `/` (the same dashboard UI, labeled
"CDK Edition").
