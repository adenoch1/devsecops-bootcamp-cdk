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
  04-cdk-deploy.yml                 manual (workflow_dispatch) deploy/destroy —
                                   deploy-ecr -> build-and-push-image ->
                                   deploy-app, or a destroy path
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
- **An automatic, push-triggered deploy pipeline** (mirroring
  `terraform-release.yml`'s trigger-on-push-to-main): not built.
  `04-cdk-deploy.yml` deploys/destroys, but only via manual
  `workflow_dispatch` — it does not run on every push to `main`. Turning
  it into a real continuous pipeline (plus a GitHub Environment approval
  gate, matching the Terraform sibling's `dev`/`dns`/`destroy` environments)
  is a reasonable next step once this has been exercised a few times
  manually.

## Deploying it (manually, for real)

`04-cdk-deploy.yml`, triggered via the Actions tab (`workflow_dispatch`,
choose `deploy` or `destroy`):

1. **`deploy-ecr`** — `cdk deploy EcrStack` only, so there's a repository for
   an image to land in before anything else happens.
2. **`build-and-push-image`** — builds `docker/Dockerfile` and pushes it to
   that repo, tagged with the commit SHA (using `github-ecr-role`, shared
   with the Terraform sibling).
3. **`deploy-app`** — `cdk deploy --all` with `CONTAINER_IMAGE_TAG` set to
   that same SHA, so the ECS task definition references an image that
   actually exists. Waits for the ECS service to stabilize, then prints the
   ALB's DNS name and a `curl -k` command to the job summary.
4. **`destroy`** (separate path, `action: destroy`) — `cdk destroy --all`.

Note on the ACM certificate: this reuses the Terraform sibling's certificate
(`app.clevernews.org`) purely for TLS termination — real DNS is **not**
repointed at this stack's ALB. Verifying the deployed app means hitting the
ALB's own AWS-assigned DNS name directly over HTTPS, which will show a
certificate hostname mismatch (expected — bypass it with `curl -k` or an
"advanced/proceed" click in a browser). This is fine for a short-lived
deploy → verify → destroy cycle; it would need a real certificate/DNS entry
for anything longer-lived.

## AWS access for CI/CD

Unlike the Terraform sibling's roles (which are tightly coupled to
Terraform's own S3+DynamoDB state mechanics and have zero CloudFormation
permissions), this repo has its own purpose-built setup:

- **`GitHubActions-CDK-DevSecOps-Role`** — the only role GitHub Actions
  assumes directly (via OIDC, trust scoped to
  `repo:adenoch1/devsecops-bootcamp-cdk`'s `main` branch and pull requests).
  Its *only* permission is `sts:AssumeRole` on the 4 roles `cdk bootstrap`
  creates (deploy, lookup, file-publishing, image-publishing) — it has no
  direct AWS permissions of its own.
- **`cdk bootstrap`** was run with `--trust` pointing at that role and
  `--cloudformation-execution-policies` pointing at a **custom managed
  policy** (`CDK-DevSecOps-CfnExec-Policy`), not CDK's `AdministratorAccess`
  default. That policy is scoped to exactly what these stacks provision
  (VPC/ECS/ALB/WAF/Firehose/KMS/S3/ECR/CloudWatch/SNS/CloudFormation), and
  IAM role creation/`PassRole` is scoped to the `devsecops-flask-dev-cdk-*`
  name prefix only.
- **`github-ecr-role`** (shared with the Terraform sibling, reused as-is —
  no changes) already trusts any `adenoch1/*` repo and has generic ECR
  push/pull permissions, so it needed no modification.
- Repo secrets: `AWS_REGION`, `AWS_ROLE_ARN_DEPLOY`, `AWS_ROLE_ARN_ECR`.

### Why every IAM role here has an explicit name

This scoping only works because every role the CDK stacks create has a
predictable `role_name=...` (matching the Terraform sibling's naming
convention) instead of letting CloudFormation auto-generate one. Two CDK
convenience features were turned off specifically to avoid needing
unpredictably-named helper roles in the execution policy:

- `auto_delete_objects=True` on S3 buckets (removed) — CDK implements this
  via a framework-managed Lambda + auto-generated role. Buckets still get
  `RemovalPolicy.DESTROY`, but `cdk destroy` will fail on non-empty buckets
  until they're emptied manually. Matches the same "protect logs from
  accidental deletion" caution the Terraform tfvars already applies via
  `logs_bucket_force_destroy = false`.
- `@aws-cdk/aws-ec2:restrictDefaultSecurityGroup` (disabled in `cdk.json`) —
  CDK can auto-lock-down the VPC's default security group via another
  framework-managed custom resource. Disabling it means the default-SG gap
  noted below is real, not just theoretical.

The alternative — granting broad `iam:CreateRole`/`PutRolePolicy` plus
`iam:PassRole` to Lambda plus `lambda:CreateFunction`/`InvokeFunction` so
CDK's internal helpers could deploy — is a well-known AWS privilege-
escalation combination (create a role, attach admin, pass it to a new
Lambda, invoke it). Not worth it for two convenience features.

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
