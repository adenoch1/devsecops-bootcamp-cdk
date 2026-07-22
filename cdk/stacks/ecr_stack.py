"""
Terraform equivalent: infra/modules/ecr/

A single KMS-encrypted, immutable-tag ECR repository with scan-on-push.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_ecr as ecr,
    aws_kms as kms,
)
from constructs import Construct

import config


class EcrStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.key = kms.Key(
            self, "EcrKey",
            description=f"KMS key for {config.NAME_PREFIX} ECR repository",
            enable_key_rotation=True,
            alias=f"{config.NAME_PREFIX}-ecr",
        )

        self.repository = ecr.Repository(
            self, "Repository",
            repository_name=f"{config.NAME_PREFIX}-repo",
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=self.key,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )
