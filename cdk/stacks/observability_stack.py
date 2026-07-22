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
)
from constructs import Construct

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

        alarms = [
            cw.Alarm(
                self, "AppErrorRateAlarm",
                alarm_description="More than 5 application ERROR log lines in 5 minutes.",
                metric=app_error_metric,
                threshold=5,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
            cw.Alarm(
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
            ),
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
