"""
Terraform equivalent: infra/envs/dev/observability.tf (Week 4 on the
Terraform sibling). Kept as its own stack here for the same reason it's a
separate file there: it needs dimensions from both the ECS/ALB/WAF resources
and the shared CloudWatch KMS key, so it sits above both rather than inside
either.
"""

from aws_cdk import (
    Stack,
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_logs as logs,
    aws_kms as kms,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_wafv2 as wafv2,
    aws_codedeploy as codedeploy,
)
from constructs import Construct
from cdk_nag import NagSuppressions

import config


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cloudwatch_logs_key: kms.IKey,
        app_log_group: logs.ILogGroup,
        cluster: ecs.ICluster,
        service: ecs.FargateService,
        alb: elbv2.ApplicationLoadBalancer,
        target_group: elbv2.ApplicationTargetGroup,
        green_target_group: elbv2.ApplicationTargetGroup,
        listener: elbv2.ApplicationListener,
        web_acl: wafv2.CfnWebACL,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.alerts_topic = sns.Topic(
            self, "AlertsTopic",
            topic_name=f"{config.NAME_PREFIX}-alerts",
            master_key=cloudwatch_logs_key,
        )
        if config.ALERT_EMAIL:
            self.alerts_topic.add_subscription(
                sns_subs.EmailSubscription(config.ALERT_EMAIL)
            )

        # ---- Structured-log-based custom metric ----
        # app/app.py emits JSON log lines with a top-level "level" field.

        error_metric_filter = logs.MetricFilter(
            self, "AppErrorMetricFilter",
            log_group=app_log_group,
            metric_namespace=f"{config.NAME_PREFIX}/App",
            metric_name="AppErrorCount",
            filter_pattern=logs.FilterPattern.string_value(
                json_field="$.level", comparison="=", value="ERROR"
            ),
            metric_value="1",
            default_value=0,
        )
        app_error_metric = error_metric_filter.metric(statistic="sum")

        # ---- Alarms ----

        # Named separately (not just indexed out of the list below) because
        # Week 5 Stage 4's CodeDeploy deployment group needs to reference
        # these two specifically as alarm-gated auto-rollback triggers.
        app_error_rate_alarm = cw.Alarm(
            self, "AppErrorRateAlarm",
            alarm_description="More than 5 application ERROR log lines in 5 minutes.",
            metric=app_error_metric,
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alb_5xx_alarm = cw.Alarm(
            self, "Alb5xxAlarm",
            alarm_description="ALB target 5xx responses exceeded threshold.",
            metric=alb.metrics.custom(
                "HTTPCode_Target_5XX_Count",
                statistic="sum",
                period=Duration.minutes(5),
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        alarms = [
            app_error_rate_alarm,
            alb_5xx_alarm,
            cw.Alarm(
                self, "TargetUnhealthyAlarm",
                alarm_description="One or more ALB targets are unhealthy.",
                metric=target_group.metrics.unhealthy_host_count(),
                threshold=0,
                evaluation_periods=2,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
            cw.Alarm(
                self, "EcsRunningBelowDesiredAlarm",
                alarm_description="ECS service is running fewer tasks than desired.",
                metric=cw.Metric(
                    namespace="ECS/ContainerInsights",
                    metric_name="RunningTaskCount",
                    dimensions_map={
                        "ClusterName": cluster.cluster_name,
                        "ServiceName": service.service_name,
                    },
                    statistic="avg",
                ),
                threshold=config.DESIRED_COUNT,
                evaluation_periods=2,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
        ]

        for alarm in alarms:
            alarm.add_alarm_action(cw_actions.SnsAction(self.alerts_topic))
            alarm.add_ok_action(cw_actions.SnsAction(self.alerts_topic))

        # ---- CodeDeploy Blue/Green (Week 5 Stage 4) ----
        # Lives here, not in EcsStack, for the same reason it lives at the
        # env level (not inside the ecs module) on the Terraform sibling:
        # it needs a cross-stack wire this stack already has (the alarms
        # above) plus what EcsStack exposes (service, both target groups,
        # listener). EcsStack is instantiated before this stack specifically
        # so this wiring is possible.

        codedeploy_app = codedeploy.EcsApplication(
            self, "CodeDeployApplication",
            application_name=f"{config.NAME_PREFIX}-app",
        )

        deployment_group = codedeploy.EcsDeploymentGroup(
            self, "CodeDeployDeploymentGroup",
            application=codedeploy_app,
            deployment_group_name=f"{config.NAME_PREFIX}-dg",
            service=service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=target_group,
                green_target_group=green_target_group,
                listener=listener,
                # Reroute traffic to green as soon as its tasks are
                # healthy — no manual "continue-deployment" gate. Same
                # reasoning as the Terraform sibling: no on-call human to
                # gate on, the alarm-triggered auto-rollback below is what
                # keeps this safe unattended.
                termination_wait_time=Duration.minutes(5),
            ),
            # Predefined AWS deployment config, referenced by name — CDK
            # has no named constant for this one (only ALL_AT_ONCE is a
            # static member; canary/linear configs are referenced by their
            # AWS-predefined name), same config the Terraform sibling uses.
            deployment_config=codedeploy.EcsDeploymentConfig.from_ecs_deployment_config_name(
                self, "LinearDeploymentConfig",
                "CodeDeployDefault.ECSLinear10PercentEvery1Minutes",
            ),
            auto_rollback=codedeploy.AutoRollbackConfig(
                failed_deployment=True,
                deployment_in_alarm=True,
            ),
            alarms=[app_error_rate_alarm, alb_5xx_alarm],
        )
        NagSuppressions.add_resource_suppressions(
            deployment_group,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "EcsDeploymentGroup auto-creates its CodeDeploy service role with AWSCodeDeployRoleForECS, the AWS-documented standard managed policy for ECS blue/green deployment groups — same policy the Terraform sibling's hand-rolled codedeploy IAM role attaches.",
                },
            ],
            apply_to_children=True,
        )

        # ---- Dashboard ----

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{config.NAME_PREFIX}-platform")

        dashboard.add_widgets(
            cw.GraphWidget(
                title="ECS CPU / Memory Utilization",
                left=[
                    cw.Metric(
                        namespace="ECS/ContainerInsights",
                        metric_name="CpuUtilized",
                        dimensions_map={"ClusterName": cluster.cluster_name, "ServiceName": service.service_name},
                    ),
                    cw.Metric(
                        namespace="ECS/ContainerInsights",
                        metric_name="MemoryUtilized",
                        dimensions_map={"ClusterName": cluster.cluster_name, "ServiceName": service.service_name},
                    ),
                ],
            ),
            cw.GraphWidget(
                title="ALB Requests / Errors",
                left=[
                    alb.metrics.request_count(),
                    alb.metrics.custom("HTTPCode_Target_4XX_Count", statistic="sum"),
                    alb.metrics.custom("HTTPCode_Target_5XX_Count", statistic="sum"),
                ],
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="ALB Target Response Time",
                left=[alb.metrics.target_response_time()],
            ),
            cw.GraphWidget(
                title="WAF Allowed / Blocked Requests",
                left=[
                    cw.Metric(
                        namespace="AWS/WAFV2",
                        metric_name="AllowedRequests",
                        dimensions_map={"WebACL": web_acl.name, "Region": config.AWS_REGION, "Rule": "ALL"},
                        statistic="sum",
                    ),
                    cw.Metric(
                        namespace="AWS/WAFV2",
                        metric_name="BlockedRequests",
                        dimensions_map={"WebACL": web_acl.name, "Region": config.AWS_REGION, "Rule": "ALL"},
                        statistic="sum",
                    ),
                ],
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Application Error Rate", left=[app_error_metric]),
        )
