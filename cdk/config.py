"""
Dev environment configuration.

This is the CDK equivalent of infra/envs/dev/terraform.tfvars in the
Terraform sibling repo (devsecops-bootcamp). The two IaC implementations are
independent by design — there is no shared source of truth between them —
so if you change one, update the other by hand if you want them to stay
equivalent.
"""

import os

from aws_cdk import aws_logs as logs

AWS_REGION = "ca-central-1"
PROJECT = "devsecops-flask"
ENVIRONMENT = "dev"
OWNER = "enoch"

# "-cdk" suffix keeps every resource name/ARN distinct from the Terraform
# stack's, so the two can coexist in the same AWS account without colliding.
NAME_PREFIX = f"{PROJECT}-{ENVIRONMENT}-cdk"

TAGS = {
    "Project": PROJECT,
    "Environment": ENVIRONMENT,
    "Owner": OWNER,
    "ManagedBy": "CDK",
}

VPC_CIDR = "192.168.0.0/16"

APP_PORT = 5000
HEALTH_CHECK_PATH = "/health"

TASK_CPU = 256
TASK_MEMORY = 512
DESIRED_COUNT = 1

LOG_RETENTION = logs.RetentionDays.ONE_YEAR
FLOW_LOG_RETENTION = logs.RetentionDays.ONE_YEAR

LIFECYCLE_GLACIER_DAYS = 30
LIFECYCLE_EXPIRE_DAYS = 365

# Pre-existing ACM certificate for the HTTPS listener — this stack does not
# create or validate a certificate (same "bring your own cert" approach as
# the Terraform sibling). Deliberately NOT hardcoded to a specific AWS
# account/cert here, unlike the Terraform tfvars: pass it in per-deployment
# so this config stays reusable rather than tied to one account.
ACM_CERTIFICATE_ARN = os.getenv("ACM_CERTIFICATE_ARN", "")

# Email subscribed to CloudWatch alarms. Same rule as the Terraform sibling:
# never hardcode a real address in committed source for a public repo.
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# Build/deploy metadata — the CDK equivalent of Terraform's
# `-var="container_image_tag=..."` etc., supplied per-deploy by CI rather
# than committed. Defaults keep `cdk synth` usable locally with no setup.
CONTAINER_IMAGE_TAG = os.getenv("CONTAINER_IMAGE_TAG", "bootstrap")
GIT_SHA = os.getenv("GIT_SHA", "dev")
BUILD_TIME = os.getenv("BUILD_TIME", "unknown")
APP_ENV = os.getenv("APP_ENV", ENVIRONMENT)
