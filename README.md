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

### Setting up the bootstrap infrastructure from scratch

This is the step-by-step version of the above — everything needed to go
from a fresh AWS account to a working `04-cdk-deploy.yml`. Run these from
any machine/terminal with AWS CLI credentials for account `476532114555`
(these are plain `aws`/`cdk` commands, not tied to any local directory).

**Prerequisites**
- The account already has an OIDC identity provider for
  `token.actions.githubusercontent.com` with client ID `sts.amazonaws.com`
  (shared, account-wide — check with
  `aws iam list-open-id-connect-providers`; the Terraform sibling already
  depends on this existing).
- `aws-cdk` CLI installed (`npm install -g aws-cdk`) and this repo's
  `cdk/requirements.txt` installed into a venv.

**Step 1 — Create the OIDC-trusted deploy role**

This is the *only* role GitHub Actions assumes directly. Save as
`trust-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsOIDC",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::476532114555:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:adenoch1*/devsecops-bootcamp-cdk*:ref:refs/heads/main",
            "repo:adenoch1*/devsecops-bootcamp-cdk*:pull_request"
          ]
        }
      }
    }
  ]
}
```

```bash
aws iam create-role \
  --role-name GitHubActions-CDK-DevSecOps-Role \
  --assume-role-policy-document file://trust-policy.json \
  --description "GitHub OIDC role for devsecops-bootcamp-cdk CI/CD"
```

> **Why the wildcards (`adenoch1*`, `devsecops-bootcamp-cdk*`) instead of
> exact names:** GitHub's OIDC `sub` claim format isn't uniform across
> repos in the same account. Older repos (like the Terraform sibling)
> produce `repo:adenoch1/devsecops-bootcamp:ref:refs/heads/main`. This repo
> — created later — produces
> `repo:adenoch1@101899552/devsecops-bootcamp-cdk@1309088841:ref:refs/heads/main`
> instead, with GitHub's org/repo numeric IDs embedded. An exact-string
> trust policy silently rejects the token with a generic `Not authorized to
> perform sts:AssumeRoleWithWebIdentity` — no hint about *why*. If you hit
> that, check CloudTrail for the actual rejected `sub`
> (`aws cloudtrail lookup-events --lookup-attributes
> AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity`) rather
> than guessing — that's how this was actually diagnosed.

Attach the only permission this role needs — assuming the CDK bootstrap
roles created in Step 3 (their ARNs are predictable ahead of time: account
+ region + the default `hnb659fds` qualifier):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeCdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::476532114555:role/cdk-hnb659fds-deploy-role-476532114555-ca-central-1",
        "arn:aws:iam::476532114555:role/cdk-hnb659fds-file-publishing-role-476532114555-ca-central-1",
        "arn:aws:iam::476532114555:role/cdk-hnb659fds-image-publishing-role-476532114555-ca-central-1",
        "arn:aws:iam::476532114555:role/cdk-hnb659fds-lookup-role-476532114555-ca-central-1"
      ]
    }
  ]
}
```

```bash
aws iam put-role-policy \
  --role-name GitHubActions-CDK-DevSecOps-Role \
  --policy-name AssumeCdkBootstrapRoles \
  --policy-document file://assume-bootstrap-roles-policy.json
```

**Step 2 — Create the least-privilege CloudFormation execution policy**

`cdk bootstrap --trust` *requires* `--cloudformation-execution-policies` to
be specified explicitly — without it, CDK defaults to `AdministratorAccess`
on the role CloudFormation actually uses to provision resources. That's a
real, meaningful grant: anyone who can get a PR merged to `main` can
effectively get account-admin, gated only by GitHub branch protection. This
custom policy scopes it to exactly what these 5 stacks provision instead.

<details>
<summary>Full policy JSON (click to expand)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormationCore",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResource", "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate", "cloudformation:GetTemplateSummary",
        "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
        "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
        "cloudformation:ListStacks", "cloudformation:ValidateTemplate"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "ca-central-1" } }
    },
    {
      "Sid": "ReadOnlyDiscovery",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity", "ec2:Describe*", "elasticloadbalancing:Describe*",
        "ecs:Describe*", "ecs:List*", "ecr:Describe*", "ecr:List*", "iam:Get*", "iam:List*",
        "sns:GetTopicAttributes", "sns:ListTopics", "sns:ListTagsForResource", "sns:ListSubscriptionsByTopic",
        "firehose:DescribeDeliveryStream", "firehose:ListDeliveryStreams", "firehose:ListTagsForDeliveryStream",
        "kms:ListAliases", "kms:ListGrants", "kms:DescribeKey", "kms:GetKeyPolicy",
        "kms:GetKeyRotationStatus", "kms:ListResourceTags",
        "tag:GetResources", "tag:GetTagKeys", "tag:GetTagValues",
        "s3:Get*", "s3:List*", "logs:Describe*", "logs:List*",
        "cloudwatch:Describe*", "cloudwatch:Get*", "cloudwatch:List*",
        "wafv2:Get*", "wafv2:List*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "NetworkEcsAlbWafFirehoseProvisioning",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
        "ec2:CreateInternetGateway", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
        "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
        "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute",
        "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
        "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
        "ec2:AllocateAddress", "ec2:ReleaseAddress", "ec2:AssociateAddress", "ec2:DisassociateAddress",
        "ec2:CreateNatGateway", "ec2:DeleteNatGateway", "ec2:CreateTags", "ec2:DeleteTags",
        "ec2:CreateFlowLogs", "ec2:DeleteFlowLogs",
        "elasticloadbalancing:*", "ecs:*", "wafv2:*", "firehose:*", "logs:*",
        "cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards",
        "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms",
        "sns:CreateTopic", "sns:DeleteTopic", "sns:SetTopicAttributes",
        "sns:TagResource", "sns:UntagResource", "sns:Subscribe", "sns:Unsubscribe",
        "ecr:CreateRepository", "ecr:DeleteRepository", "ecr:TagResource", "ecr:UntagResource",
        "ecr:PutLifecyclePolicy", "ecr:DeleteLifecyclePolicy",
        "ecr:PutImageScanningConfiguration", "ecr:PutImageTagMutability", "ecr:SetRepositoryPolicy"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "ca-central-1" } }
    },
    {
      "Sid": "KmsS3AssetsAndLogging",
      "Effect": "Allow",
      "Action": [
        "kms:CreateKey", "kms:TagResource", "kms:UntagResource",
        "kms:EnableKeyRotation", "kms:DisableKeyRotation",
        "kms:CreateAlias", "kms:DeleteAlias",
        "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion",
        "kms:PutKeyPolicy", "kms:UpdateKeyDescription",
        "kms:RetireGrant", "kms:RevokeGrant", "kms:CreateGrant",
        "kms:Decrypt", "kms:Encrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*",
        "s3:CreateBucket", "s3:DeleteBucket", "s3:Put*",
        "s3:DeleteBucketPolicy", "s3:DeleteBucketWebsite",
        "s3:DeleteObjectVersion", "s3:ListBucketVersions", "s3:GetObject", "s3:DeleteObject"
      ],
      "Resource": "*"
    },
    {
      "Sid": "NamedRoleLifecycle",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:UpdateAssumeRolePolicy",
        "iam:TagRole", "iam:UntagRole",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy"
      ],
      "Resource": "arn:aws:iam::476532114555:role/devsecops-flask-dev-cdk-*"
    },
    {
      "Sid": "NamedRolePassRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::476532114555:role/devsecops-flask-dev-cdk-ecs-task-role",
        "arn:aws:iam::476532114555:role/devsecops-flask-dev-cdk-ecs-task-exec",
        "arn:aws:iam::476532114555:role/devsecops-flask-dev-cdk-firehose-waf",
        "arn:aws:iam::476532114555:role/devsecops-flask-dev-cdk-vpc-flowlogs-role"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": ["ecs-tasks.amazonaws.com", "firehose.amazonaws.com", "vpc-flow-logs.amazonaws.com"]
        }
      }
    },
    {
      "Sid": "CreateServiceLinkedRoleIfNeeded",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "iam:AWSServiceName": ["elasticloadbalancing.amazonaws.com", "ecs.amazonaws.com", "firehose.amazonaws.com"]
        }
      }
    },
    {
      "Sid": "CdkBootstrapVersionCheck",
      "Effect": "Allow",
      "Action": ["ssm:GetParameters", "ssm:GetParameter"],
      "Resource": "arn:aws:ssm:ca-central-1:476532114555:parameter/cdk-bootstrap/hnb659fds/version"
    }
  ]
}
```

</details>

> **Why `NamedRoleLifecycle`/`NamedRolePassRole` are scoped to a name
> prefix, not `Resource: "*"`:** an early draft of this policy granted
> broad `iam:CreateRole`/`PutRolePolicy` (to let CDK's own auto-generated
> helper Lambdas — for `auto_delete_objects`, default-SG restriction —
> deploy). That combination, plus `iam:PassRole` to Lambda and
> `lambda:CreateFunction`/`InvokeFunction`, is a documented AWS privilege-
> escalation chain (create a role, attach admin, pass it to a new Lambda,
> invoke it). Fixed by giving every IAM role in the stacks an explicit,
> predictable `role_name=...` and disabling the two CDK convenience
> features that relied on unpredictable auto-generated roles (see below).
>
> **Why `CdkBootstrapVersionCheck` (`ssm:GetParameters`) is there:** every
> `cdk deploy`, even of a single trivial stack, checks the bootstrap
> version via an SSM parameter lookup. Miss this and the very first deploy
> fails with `AccessDeniedException` on `ssm:GetParameters` — easy to miss
> since nothing in application code references SSM at all.

```bash
aws iam create-policy \
  --policy-name CDK-DevSecOps-CfnExec-Policy \
  --description "Least-privilege CloudFormation execution policy for devsecops-bootcamp-cdk deploys" \
  --policy-document file://cdk-exec-policy.json
```

**Step 3 — Bootstrap CDK itself**

```bash
cdk bootstrap aws://476532114555/ca-central-1 \
  --trust arn:aws:iam::476532114555:role/GitHubActions-CDK-DevSecOps-Role \
  --trust-for-lookup arn:aws:iam::476532114555:role/GitHubActions-CDK-DevSecOps-Role \
  --cloudformation-execution-policies arn:aws:iam::476532114555:policy/CDK-DevSecOps-CfnExec-Policy
```

This deploys the `CDKToolkit` CloudFormation stack: a staging S3 bucket, an
ECR repo for image assets (unused here — this app pushes to its own
`EcrStack` repo directly instead), and 5 IAM roles (`deploy`, `lookup`,
`file-publishing`, `image-publishing`, `cfn-exec`) whose trust policies
only allow `GitHubActions-CDK-DevSecOps-Role` to assume them.

**Step 4 — Add the ECS health-check permission**

The deploy workflow's post-deploy health check
(`aws ecs wait services-stable`) runs directly under
`GitHubActions-CDK-DevSecOps-Role`'s credentials — it doesn't go through
CDK's own role-assumption chain, so the role needs its own narrow,
read-only grant. Cluster/service names are deterministic from
`config.NAME_PREFIX`, so this can be added before the first deploy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcsHealthCheckReadOnly",
      "Effect": "Allow",
      "Action": ["ecs:DescribeServices", "ecs:DescribeClusters"],
      "Resource": [
        "arn:aws:ecs:ca-central-1:476532114555:service/devsecops-flask-dev-cdk-cluster/devsecops-flask-dev-cdk-svc",
        "arn:aws:ecs:ca-central-1:476532114555:cluster/devsecops-flask-dev-cdk-cluster"
      ]
    }
  ]
}
```

```bash
aws iam put-role-policy \
  --role-name GitHubActions-CDK-DevSecOps-Role \
  --policy-name EcsHealthCheckReadOnly \
  --policy-document file://ecs-healthcheck-policy.json
```

**Step 5 — Confirm `github-ecr-role`'s trust covers this repo**

Reused as-is from the Terraform sibling for image push/pull — but check
its trust condition matches the wildcard subject-format note from Step 1
(`repo:adenoch1*/*`, not the older `repo:adenoch1/*`). If it only has the
old pattern, `build-and-push-image` will fail the same
`AssumeRoleWithWebIdentity` way Step 1 describes, and needs the same fix:

```bash
aws iam get-role --role-name github-ecr-role \
  --query "Role.AssumeRolePolicyDocument.Statement[0].Condition.StringLike"
```

**Step 6 — Set GitHub repo secrets**

```bash
gh secret set AWS_REGION --repo adenoch1/devsecops-bootcamp-cdk --body "ca-central-1"
gh secret set AWS_ROLE_ARN_DEPLOY --repo adenoch1/devsecops-bootcamp-cdk \
  --body "arn:aws:iam::476532114555:role/GitHubActions-CDK-DevSecOps-Role"
gh secret set AWS_ROLE_ARN_ECR --repo adenoch1/devsecops-bootcamp-cdk \
  --body "arn:aws:iam::476532114555:role/github-ecr-role"
gh secret set ACM_CERTIFICATE_ARN --repo adenoch1/devsecops-bootcamp-cdk \
  --body "<your ACM certificate ARN>"
gh secret set ALERT_EMAIL --repo adenoch1/devsecops-bootcamp-cdk \
  --body "<email for CloudWatch alarm notifications>"
```

At this point `04-cdk-deploy.yml` → `workflow_dispatch` → `action: deploy`
should work end to end.

### Tearing down the bootstrap infrastructure

This is separate from `04-cdk-deploy.yml`'s `destroy` action — that only
removes the 5 *application* stacks. The bootstrap layer is meant to be
reusable across many deploy/destroy cycles, so there's no automated
teardown for it; do this only when you're sure you won't deploy again soon.

```bash
# 1. Empty the CDK staging bucket (bucket delete requires it to be empty)
aws s3api list-object-versions --bucket cdk-hnb659fds-assets-476532114555-ca-central-1 \
  --query "{Objects: Versions[].{Key:Key,VersionId:VersionId}}" > versions.json
aws s3api delete-objects --bucket cdk-hnb659fds-assets-476532114555-ca-central-1 \
  --delete file://versions.json

# 2. Delete the bootstrap stack (staging bucket, asset ECR repo, SSM
#    parameter, and the 5 CDK-managed roles all go with it)
aws cloudformation delete-stack --stack-name CDKToolkit --region ca-central-1
aws cloudformation describe-stacks --stack-name CDKToolkit --region ca-central-1
# ^ repeat until this errors "does not exist" — that means it's done

# 3. Only after step 2 finishes — the policy is attached to a role step 2 deletes
aws iam delete-policy --policy-arn arn:aws:iam::476532114555:policy/CDK-DevSecOps-CfnExec-Policy

# 4. The custom deploy role (inline policies must go before the role itself)
aws iam delete-role-policy --role-name GitHubActions-CDK-DevSecOps-Role --policy-name AssumeCdkBootstrapRoles
aws iam delete-role-policy --role-name GitHubActions-CDK-DevSecOps-Role --policy-name EcsHealthCheckReadOnly
aws iam delete-role --role-name GitHubActions-CDK-DevSecOps-Role
```

Leave `github-ecr-role` alone — it's shared with the Terraform sibling and
not part of this repo's bootstrap.

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
