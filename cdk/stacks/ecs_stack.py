"""
Terraform equivalent: infra/modules/ecs/ AND infra/modules/iam/

The largest module on the Terraform side, ported here as one stack to keep
the same "everything that makes the app reachable and observable" boundary:
security groups, ALB + HTTPS listener + target group, WAFv2 (3 AWS managed
rule groups) with Firehose->S3 logging, ECS Fargate cluster/service/task
definition (read-only root FS + writable tmp volume), and the app's
CloudWatch log group.

The two ECS IAM roles live here too, not in a separate IamStack, unlike the
Terraform sibling. Reason: the log driver's automatic grantWrite() call adds
an inline policy onto the execution role referencing this stack's log group
ARN. If the role were defined in another stack, that grant would create a
second, opposite cross-stack dependency (IamStack -> EcsStack) on top of the
one already needed to hand the role into this stack (EcsStack -> IamStack) —
CloudFormation stacks must form a DAG, so CDK rejects the cycle at synth
time. Terraform doesn't hit this because a single state file has no such
per-stack dependency-direction constraint. Keeping task-specific roles in the
same stack as the workload that uses them sidesteps the cycle entirely.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    Annotations,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_wafv2 as wafv2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_logs as logs,
    aws_kinesisfirehose as firehose,
    aws_certificatemanager as acm,
)
from constructs import Construct
from cdk_nag import NagSuppressions, NagPackSuppression

import config


class EcsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        ecr_repository,
        alb_logs_bucket: s3.IBucket,
        cloudwatch_logs_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- IAM roles (Terraform equivalent: infra/modules/iam/) ----

        self.ecs_task_execution_role = iam.Role(
            self, "EcsTaskExecutionRole",
            role_name=f"{config.NAME_PREFIX}-ecs-task-exec",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.ecs_task_execution_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AmazonECSTaskExecutionRolePolicy is the AWS-documented standard policy for ECS task execution (ECR pull + CloudWatch Logs write) — same role/policy the Terraform sibling attaches.",
                },
            ],
        )
        # The DefaultPolicy (IAM5, ecr:GetAuthorizationToken's mandatory
        # Resource: '*') is suppressed near the end of __init__ instead — it
        # doesn't exist as a construct until add_container()'s grants run.

        self.ecs_task_role = iam.Role(
            self, "EcsTaskRole",
            role_name=f"{config.NAME_PREFIX}-ecs-task-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # Least-privilege equivalent of the AWSXRayDaemonWriteAccess managed
        # policy — only what the daemon sidecar actually calls: segment/
        # telemetry writes, plus sampling-rule reads (the SDK polls these to
        # decide what to trace; without read access it silently falls back
        # to its built-in default rule, so this isn't strictly required, but
        # denying it would spam CloudWatch with quiet AccessDenied noise on
        # every poll interval). Matches the Terraform sibling's xray_write
        # policy document exactly.
        self.ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "xray:GetSamplingStatisticSummaries",
                ],
                resources=["*"],  # X-Ray API actions do not support resource-level scoping
            )
        )

        # ---- CloudWatch Logs (app container) ----

        self.app_log_group = logs.LogGroup(
            self, "AppLogGroup",
            log_group_name=f"/ecs/{config.NAME_PREFIX}",
            retention=config.LOG_RETENTION,
            encryption_key=cloudwatch_logs_key,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- Security groups ----

        self.alb_sg = ec2.SecurityGroup(
            self, "AlbSg",
            vpc=vpc,
            description="ALB security group - HTTPS from anywhere",
            allow_all_outbound=False,
        )
        self.alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
        self.alb_sg.add_ingress_rule(ec2.Peer.any_ipv6(), ec2.Port.tcp(443))
        self.alb_sg.add_egress_rule(
            ec2.Peer.ipv4(config.VPC_CIDR), ec2.Port.tcp(config.APP_PORT)
        )
        NagSuppressions.add_resource_suppressions(
            self.alb_sg,
            [
                {
                    "id": "AwsSolutions-EC23",
                    "reason": "This is the public-facing ALB — it must accept HTTPS from the internet by design. Restricted to port 443 only, with WAFv2 (3 AWS managed rule groups) attached in front of it.",
                }
            ],
        )

        self.ecs_sg = ec2.SecurityGroup(
            self, "EcsTasksSg",
            vpc=vpc,
            description="ECS tasks security group - app port from ALB only",
            allow_all_outbound=False,
        )
        self.ecs_sg.add_ingress_rule(
            ec2.Peer.security_group_id(self.alb_sg.security_group_id),
            ec2.Port.tcp(config.APP_PORT),
        )
        # DNS + HTTPS egress (ECR pulls, AWS API calls via NAT)
        self.ecs_sg.add_egress_rule(ec2.Peer.ipv4(config.VPC_CIDR), ec2.Port.udp(53))
        self.ecs_sg.add_egress_rule(ec2.Peer.ipv4(config.VPC_CIDR), ec2.Port.tcp(53))
        self.ecs_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
        self.ecs_sg.add_egress_rule(ec2.Peer.any_ipv6(), ec2.Port.tcp(443))

        # ---- ALB ----

        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=self.alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            deletion_protection=True,
            drop_invalid_header_fields=True,
        )
        self.alb.log_access_logs(alb_logs_bucket, prefix="alb-access")

        # "Blue" target group (Week 5 Stage 4 terminology) — attribute name
        # kept as `target_group` rather than renamed to `blue_target_group`,
        # matching the Terraform sibling's reasoning for not renaming its
        # `aws_lb_target_group.app` resource: this is where the listener's
        # default action points on initial creation; CodeDeploy takes over
        # routing between this and `green_target_group` on every deployment
        # after that.
        self.target_group = elbv2.ApplicationTargetGroup(
            self, "TargetGroup",
            vpc=vpc,
            port=config.APP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path=config.HEALTH_CHECK_PATH,
                healthy_http_codes="200-399",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=3,
                unhealthy_threshold_count=3,
            ),
        )

        # "Green" target group (Week 5 Stage 4) — CodeDeploy registers each
        # new deployment's tasks here first, health-checks them, then shifts
        # listener traffic from blue to green. Identical shape to the blue
        # target group.
        self.green_target_group = elbv2.ApplicationTargetGroup(
            self, "GreenTargetGroup",
            vpc=vpc,
            port=config.APP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path=config.HEALTH_CHECK_PATH,
                healthy_http_codes="200-399",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=3,
                unhealthy_threshold_count=3,
            ),
        )

        certificate = acm.Certificate.from_certificate_arn(
            self, "Certificate", config.ACM_CERTIFICATE_ARN
        )

        self.listener = self.alb.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            ssl_policy=elbv2.SslPolicy.TLS13_RES,
            certificates=[certificate],
            default_target_groups=[self.target_group],
        )
        # No :80 listener/redirect — HTTPS-only, matching the Terraform sibling.

        # ---- WAFv2 (3 AWS managed rule groups) ----

        managed_rules = [
            "AWSManagedRulesCommonRuleSet",
            "AWSManagedRulesKnownBadInputsRuleSet",
            "AWSManagedRulesAmazonIpReputationList",
        ]

        self.web_acl = wafv2.CfnWebACL(
            self, "WebAcl",
            name=f"{config.NAME_PREFIX}-waf",
            description="WAF for ALB",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{config.NAME_PREFIX}-waf",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name=rule_name,
                    priority=priority,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            name=rule_name, vendor_name="AWS"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=rule_name,
                        sampled_requests_enabled=True,
                    ),
                )
                for priority, rule_name in enumerate(managed_rules, start=1)
            ],
        )

        wafv2.CfnWebACLAssociation(
            self, "WebAclAssociation",
            resource_arn=self.alb.load_balancer_arn,
            web_acl_arn=self.web_acl.attr_arn,
        )

        # ---- WAF logging: Firehose -> S3 (encrypted) ----

        waf_s3_key = kms.Key(
            self, "WafS3LogsKey",
            description="KMS CMK for S3 logging buckets (WAF logs + access logs)",
            enable_key_rotation=True,
            alias=f"{config.NAME_PREFIX}-s3-waf-logs",
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        # Firehose's own service principal needs key access for stream-level
        # SSE (DeliveryStreamEncryptionConfigurationInput below), separate
        # from the firehose_role's object-level S3/KMS grants further down —
        # matches the Terraform sibling's more elaborate waf_logs KMS policy.
        waf_s3_key.grant_encrypt_decrypt(iam.ServicePrincipal("firehose.amazonaws.com"))

        # NOTE: enforce_ssl=True here is stricter than the Terraform sibling —
        # its equivalent waf_logs/waf_logs_access buckets (infra/modules/ecs/
        # main.tf) don't have an explicit deny-insecure-transport policy.
        # Found via cdk-nag (AwsSolutions-S10) while porting; fixed here since
        # it's a one-line, zero-cost improvement. Worth backporting.
        waf_logs_access_bucket = s3.Bucket(
            self, "WafLogsAccessBucket",
            bucket_name=f"{config.NAME_PREFIX}-waf-logs-access-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=waf_s3_key,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    expiration=Duration.days(30),
                )
            ],
        )

        waf_logs_bucket = s3.Bucket(
            self, "WafLogsBucket",
            bucket_name=f"{config.NAME_PREFIX}-waf-logs-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=waf_s3_key,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            server_access_logs_bucket=waf_logs_access_bucket,
            server_access_logs_prefix="access-logs/",
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

        firehose_role = iam.Role(
            self, "FirehoseWafRole",
            role_name=f"{config.NAME_PREFIX}-firehose-waf",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
        )
        # Scoped to exactly what Firehose's S3 destination needs (matches the
        # narrower policy on the Terraform sibling's firehose_waf role),
        # rather than a blanket grant_read_write/grant_encrypt_decrypt.
        waf_logs_bucket.grant_put(firehose_role)
        firehose_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:AbortMultipartUpload",
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                ],
                resources=[waf_logs_bucket.bucket_arn, f"{waf_logs_bucket.bucket_arn}/*"],
            )
        )
        waf_s3_key.grant_encrypt(firehose_role)
        NagSuppressions.add_resource_suppressions(
            firehose_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "S3 object-level actions (PutObject/AbortMultipartUpload/etc.) require a '/*' resource suffix, and KMS GenerateDataKey/Encrypt have no per-object ARN to scope to — both are the minimal expressible grant for Firehose writing WAF logs to this bucket.",
                }
            ],
            apply_to_children=True,
        )

        # Name is required to start with "aws-waf-logs-" for WAFv2 to accept
        # it as a logging destination — same convention as the Terraform side.
        self.waf_firehose = firehose.CfnDeliveryStream(
            self, "WafFirehose",
            delivery_stream_name=f"aws-waf-logs-{config.NAME_PREFIX}-waf-logs",
            delivery_stream_type="DirectPut",
            # Stream-level SSE (data in transit through Firehose, before it
            # lands in S3) — distinct from the destination-level SSE-KMS
            # below, which encrypts the delivered S3 objects. The Terraform
            # sibling sets both too.
            delivery_stream_encryption_configuration_input=firehose.CfnDeliveryStream.DeliveryStreamEncryptionConfigurationInputProperty(
                key_type="CUSTOMER_MANAGED_CMK",
                key_arn=waf_s3_key.key_arn,
            ),
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=waf_logs_bucket.bucket_arn,
                role_arn=firehose_role.role_arn,
                prefix="AWSLogs/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    size_in_m_bs=5, interval_in_seconds=300
                ),
                compression_format="GZIP",
                encryption_configuration=firehose.CfnDeliveryStream.EncryptionConfigurationProperty(
                    kms_encryption_config=firehose.CfnDeliveryStream.KMSEncryptionConfigProperty(
                        awskms_key_arn=waf_s3_key.key_arn
                    )
                ),
            ),
        )

        wafv2.CfnLoggingConfiguration(
            self, "WafLoggingConfiguration",
            resource_arn=self.web_acl.attr_arn,
            log_destination_configs=[self.waf_firehose.attr_arn],
        )

        # ---- ECS cluster ----

        self.cluster = ecs.Cluster(
            self, "Cluster",
            cluster_name=f"{config.NAME_PREFIX}-cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # ---- Task definition ----

        self.task_definition = ecs.FargateTaskDefinition(
            self, "TaskDefinition",
            family=f"{config.NAME_PREFIX}-task",
            cpu=config.TASK_CPU,
            memory_limit_mib=config.TASK_MEMORY,
            execution_role=self.ecs_task_execution_role,
            task_role=self.ecs_task_role,
        )

        # Ephemeral writable volume — the container runs with a read-only
        # root filesystem, so /tmp, /var/tmp, /usr/tmp are bind-mounted onto
        # this Fargate-safe (no EFS) volume instead.
        self.task_definition.add_volume(name="tmp")

        # X-Ray daemon sidecar (Week 5 Stage 3). Defined before the app
        # container so the app can declare a START dependency on it.
        # essential=False: if the daemon dies, the app keeps serving traffic
        # instead of the whole task cycling — tracing is an observability
        # nice-to-have here, not a reason to take an outage.
        xray_container = self.task_definition.add_container(
            "xray-daemon",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/xray/aws-xray-daemon:latest"),
            essential=False,
            cpu=32,
            memory_reservation_mib=256,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="xray-daemon", log_group=self.app_log_group
            ),
        )
        xray_container.add_port_mappings(
            ecs.PortMapping(container_port=2000, protocol=ecs.Protocol.UDP)
        )

        container = self.task_definition.add_container(
            "app",
            image=ecs.ContainerImage.from_ecr_repository(
                ecr_repository, config.CONTAINER_IMAGE_TAG
            ),
            readonly_root_filesystem=True,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs", log_group=self.app_log_group
            ),
            environment={
                "SERVICE_NAME": config.PROJECT,
                "APP_ENV": config.APP_ENV,
                "GIT_SHA": config.GIT_SHA,
                "IMAGE_TAG": config.CONTAINER_IMAGE_TAG,
                "BUILD_TIME": config.BUILD_TIME,
                "TMPDIR": "/tmp",
                "GUNICORN_CMD_ARGS": "--worker-tmp-dir /tmp",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPYCACHEPREFIX": "/tmp/pycache",
                "AWS_XRAY_DAEMON_ADDRESS": "127.0.0.1:2000",
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=config.APP_PORT))
        container.add_container_dependencies(
            ecs.ContainerDependency(
                container=xray_container, condition=ecs.ContainerDependencyCondition.START
            )
        )
        NagSuppressions.add_resource_suppressions(
            self.task_definition,
            [
                {
                    "id": "AwsSolutions-ECS2",
                    "reason": "All environment variables here are non-sensitive build/deploy metadata (git SHA, image tag, build time, service/env name) and runtime tuning flags — no credentials or secrets. Nothing here would benefit from Secrets Manager/SSM.",
                }
            ],
        )

        for mount_path in ("/tmp", "/var/tmp", "/usr/tmp"):
            container.add_mount_points(
                ecs.MountPoint(
                    source_volume="tmp",
                    container_path=mount_path,
                    read_only=False,
                )
            )

        # ---- Service ----

        # Week 5 Stage 4: deployment_controller=CODE_DEPLOY replaces
        # min_healthy_percent/max_healthy_percent/circuit_breaker — all
        # three only apply to ECS's own rolling-update controller and are
        # either meaningless or outright rejected once CodeDeploy owns the
        # rollout. Not a capability regression: CodeDeploy's
        # auto_rollback_configuration (ObservabilityStack) covers the same
        # failed-deployment case the circuit breaker did, plus the new
        # alarm-triggered case it never could.
        #
        # Unlike the Terraform sibling, no CDK-side equivalent of
        # `lifecycle.ignore_changes` is needed here or on the listener
        # below. Two different reasons: (1) CloudFormation has documented,
        # built-in special-case behavior for `AWS::ECS::Service` with
        # `DeploymentController.Type=CODE_DEPLOY` — it does not attempt to
        # reconcile TaskDefinition/LoadBalancers on stack updates the way
        # Terraform's plan/apply refresh cycle does. (2) More fundamentally,
        # CloudFormation change sets are computed by diffing the new
        # template against CloudFormation's own stored record of the last-
        # deployed template, not by re-reading the live resource's current
        # AWS state first (that's a separate, manually-invoked feature —
        # "drift detection" — precisely because CFN doesn't do this by
        # default). Terraform's `-refresh=true` plan step does read live
        # state first, which is exactly what caused the real outage this
        # session on the Terraform side: it saw CodeDeploy's live listener
        # change as drift and "corrected" it back. CloudFormation has
        # nothing to notice here in the first place.
        self.service = ecs.FargateService(
            self, "Service",
            service_name=f"{config.NAME_PREFIX}-svc",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=config.DESIRED_COUNT,
            security_groups=[self.ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            assign_public_ip=False,
            health_check_grace_period=Duration.seconds(60),
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
        )
        self.service.attach_to_application_target_group(self.target_group)

        # Acknowledges the "minHealthyPercent has not been configured"
        # warning cdk synth prints for this construct — deliberately not
        # set (see the comment above this service), since it's an ECS-
        # native-rolling-update field this CODE_DEPLOY-controlled service
        # doesn't use.
        Annotations.of(self.service).acknowledge_warning(
            "@aws-cdk/aws-ecs:minHealthyPercent"
        )

        # Applied last (by exact path, not apply_to_children) so it targets
        # the DefaultPolicy created by the ECR-pull and log-write grants
        # above, which don't exist as constructs until add_container() runs.
        # KNOWN ISSUE: `cdk synth` still reports these findings
        # (AwsSolutions-IAM5 on EcsTaskExecutionRole/DefaultPolicy, caused by
        # ecr:GetAuthorizationToken requiring Resource: '*'; and on
        # EcsTaskRole/DefaultPolicy, caused by the X-Ray write actions above
        # — both hard AWS API constraints true for every AWS account, not
        # real over-broad grants) despite trying all three documented
        # suppression mechanisms: add_resource_suppressions(apply_to_
        # children=True) called both before and after the grant existed,
        # add_resource_suppressions_by_path, and add_stack_suppressions —
        # each attaches correct metadata (verified in the synthesized
        # template) but cdk-nag 2.38.2's Aspect still flags it. Kept below
        # as documentation of the accepted, reviewed risk rather than
        # silently dropped; `cdk synth`'s exit code 1 for these two lines is
        # a known tooling gap, not an unreviewed security finding.
        NagSuppressions.add_stack_suppressions(
            self,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Two IAM5 sources in this stack, both a required Resource: '*' with no resource-level alternative: ecr:GetAuthorizationToken on the ECS task execution role, and the X-Ray API write/sampling actions (PutTraceSegments/PutTelemetryRecords/GetSamplingRules/GetSamplingTargets/GetSamplingStatisticSummaries) on the ECS task role.",
                ),
            ],
        )

        # ---- Outputs (consumed by the deploy workflow's health check) ----

        CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "EcsClusterName", value=self.cluster.cluster_name)
        CfnOutput(self, "EcsServiceName", value=self.service.service_name)
        CfnOutput(self, "AlbTargetGroupArn", value=self.target_group.target_group_arn)
