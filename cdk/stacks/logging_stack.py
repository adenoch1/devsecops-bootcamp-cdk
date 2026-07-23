"""
Terraform equivalent: infra/modules/logging/

Two purpose-built KMS keys (S3 log buckets, CloudWatch Logs) and a chain of
three S3 buckets for access-log delivery:
  alb_logs -> server_access_logs -> ultimate_sink
The chain terminates at ultimate_sink to avoid an infinite logging loop
(each bucket logs access to the next one down, and the sink logs to no one).
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_kms as kms,
    aws_s3 as s3,
    aws_iam as iam,
    Duration,
)
from constructs import Construct

import config


class LoggingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- KMS keys ----

        self.s3_logs_key = kms.Key(
            self, "S3LogsKey",
            description="KMS key for S3 server access logs / sink buckets (SSE-KMS)",
            enable_key_rotation=True,
            alias=f"{config.NAME_PREFIX}-alb-logs",
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )

        self.cloudwatch_logs_key = kms.Key(
            self, "CloudWatchLogsKey",
            description="KMS key for CloudWatch Logs encryption",
            enable_key_rotation=True,
            alias=f"{config.NAME_PREFIX}-cloudwatch-logs",
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        # CloudWatch Logs won't use a CMK to encrypt a log group unless the
        # key's own policy explicitly allows the regional logs service
        # principal — the default kms.Key() policy only grants the account
        # root. Matches the Terraform sibling's cloudwatch_logs key policy
        # ("AllowCloudWatchLogsUse" statement).
        self.cloudwatch_logs_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogsUse",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        # ---- S3 bucket chain (terminal sink first) ----

        self.ultimate_sink_bucket = s3.Bucket(
            self, "UltimateSinkBucket",
            bucket_name=f"{config.NAME_PREFIX}-s3-ultimate-sink-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.s3_logs_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(config.LIFECYCLE_GLACIER_DAYS),
                        )
                    ],
                    expiration=Duration.days(config.LIFECYCLE_EXPIRE_DAYS),
                )
            ],
            # Terminal sink intentionally has no server_access_logs_bucket of
            # its own — logging it to itself would be an infinite loop.
        )

        self.server_access_logs_bucket = s3.Bucket(
            self, "ServerAccessLogsBucket",
            bucket_name=f"{config.NAME_PREFIX}-s3-server-access-logs-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.s3_logs_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            server_access_logs_bucket=self.ultimate_sink_bucket,
            server_access_logs_prefix="server-access-logs/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(config.LIFECYCLE_GLACIER_DAYS),
                        )
                    ],
                    expiration=Duration.days(config.LIFECYCLE_EXPIRE_DAYS),
                )
            ],
        )

        # ALB access log delivery only supports SSE-S3 (AES256), not SSE-KMS —
        # same constraint documented (and checkov-skipped) in the Terraform
        # sibling's infra/modules/logging/main.tf.
        self.alb_logs_bucket = s3.Bucket(
            self, "AlbLogsBucket",
            bucket_name=f"{config.NAME_PREFIX}-alb-logs-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            server_access_logs_bucket=self.server_access_logs_bucket,
            server_access_logs_prefix="alb-logs/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(config.LIFECYCLE_GLACIER_DAYS),
                        )
                    ],
                    expiration=Duration.days(config.LIFECYCLE_EXPIRE_DAYS),
                )
            ],
        )

        # ELB's log-delivery service account needs to write into this bucket;
        # CDK's ApplicationLoadBalancer.log_access_logs() grants this
        # automatically when wired up in EcsStack, so no manual bucket
        # policy is added here.
