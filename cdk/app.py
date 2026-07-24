#!/usr/bin/env python3
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

import config
from stacks.network_stack import NetworkStack
from stacks.logging_stack import LoggingStack
from stacks.ecr_stack import EcrStack
from stacks.ecs_stack import EcsStack
from stacks.observability_stack import ObservabilityStack

app = cdk.App()

env = cdk.Environment(region=config.AWS_REGION)

logging_stack = LoggingStack(app, "LoggingStack", env=env, tags=config.TAGS)

network_stack = NetworkStack(
    app, "NetworkStack", env=env, tags=config.TAGS,
    cloudwatch_logs_key=logging_stack.cloudwatch_logs_key,
)
network_stack.add_dependency(logging_stack)

ecr_stack = EcrStack(app, "EcrStack", env=env, tags=config.TAGS)

ecs_stack = EcsStack(
    app, "EcsStack", env=env, tags=config.TAGS,
    vpc=network_stack.vpc,
    ecr_repository=ecr_stack.repository,
    alb_logs_bucket=logging_stack.alb_logs_bucket,
    cloudwatch_logs_key=logging_stack.cloudwatch_logs_key,
)

observability_stack = ObservabilityStack(
    app, "ObservabilityStack", env=env, tags=config.TAGS,
    cloudwatch_logs_key=logging_stack.cloudwatch_logs_key,
    app_log_group=ecs_stack.app_log_group,
    cluster=ecs_stack.cluster,
    service=ecs_stack.service,
    alb=ecs_stack.alb,
    target_group=ecs_stack.target_group,
    green_target_group=ecs_stack.green_target_group,
    listener=ecs_stack.listener,
    web_acl=ecs_stack.web_acl,
)

# cdk-nag: AWS Solutions rule pack, the CDK equivalent of tfsec/Checkov
# running against every `cdk synth`.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
