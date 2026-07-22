"""
Terraform equivalent: infra/modules/network/

VPC across 2 AZs, public + private (egress-via-NAT) subnets, a single NAT
gateway (cost trade-off — same one the Terraform sibling documents: "later
we can do 2 for HA"), and VPC Flow Logs to a KMS-encrypted CloudWatch log
group.
"""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_kms as kms,
    aws_iam as iam,
)
from constructs import Construct

import config


class NetworkStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cloudwatch_logs_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        flow_log_group = logs.LogGroup(
            self, "VpcFlowLogGroup",
            log_group_name=f"/vpc/flowlogs/{config.NAME_PREFIX}",
            retention=config.FLOW_LOG_RETENTION,
            encryption_key=cloudwatch_logs_key,
        )

        self.vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDR),
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
            ],
        )

        # Explicit, named role (rather than the Vpc(flow_logs=...) shorthand,
        # which lets CloudFormation auto-generate an unpredictable role name)
        # so the GitHub Actions deploy role's IAM permissions can be scoped
        # to an exact resource ARN instead of a wildcard.
        flow_log_role = iam.Role(
            self, "VpcFlowLogsRole",
            role_name=f"{config.NAME_PREFIX}-vpc-flowlogs-role",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
        )

        ec2.FlowLog(
            self, "VpcFlowLog",
            resource_type=ec2.FlowLogResourceType.from_vpc(self.vpc),
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group, flow_log_role),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # NOTE: the Terraform sibling also locks down the VPC's default
        # security group (empty ingress/egress) via aws_default_security_group.
        # CloudFormation has no first-class resource for a VPC's implicit
        # default SG — doing this properly needs a custom resource calling
        # ec2:RevokeSecurityGroupIngress/Egress. Not implemented here; every
        # workload in this stack gets its own purpose-built SG in EcsStack,
        # so the default SG being permissive is unused rather than unsafe,
        # but it's a real gap relative to the Terraform side worth closing.
