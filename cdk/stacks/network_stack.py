"""
Terraform equivalent: infra/modules/network/

VPC across 2 AZs, public + private (egress-via-NAT) subnets, a single NAT
gateway (cost trade-off — same one the Terraform sibling documents: "later
we can do 2 for HA"), and VPC Flow Logs to a KMS-encrypted CloudWatch log
group.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
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
            removal_policy=RemovalPolicy.DESTROY,
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

        # ---- VPC Endpoints (Week 5 Stage 2) ----
        # Keeps ECS task traffic to AWS services off the NAT/public path.
        # Terraform equivalent: infra/modules/network/endpoints.tf — same
        # honest cost note applies (this likely costs as much or more per
        # month than the single NAT gateway it doesn't replace, at this
        # scale; it's a security-posture improvement, not a cost win).
        #
        # Security group scoped to the VPC CIDR rather than EcsStack's task
        # SG: that SG lives in EcsStack, which depends on this stack's VPC
        # output, so scoping to it here would invert the dependency.
        endpoints_sg = ec2.SecurityGroup(
            self, "VpcEndpointsSg",
            vpc=self.vpc,
            description="Interface VPC endpoints - HTTPS from within the VPC only",
            allow_all_outbound=False,
        )
        endpoints_sg.add_ingress_rule(ec2.Peer.ipv4(config.VPC_CIDR), ec2.Port.tcp(443))
        endpoints_sg.add_egress_rule(ec2.Peer.ipv4(config.VPC_CIDR), ec2.Port.all_traffic())

        # Gateway endpoint for S3 (free) — ECR image layers are actually
        # fetched from S3 under the hood, so this covers image pulls too.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        for endpoint_id, service in {
            "EcrApiEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDkrEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "LogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }.items():
            self.vpc.add_interface_endpoint(
                endpoint_id,
                service=service,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                security_groups=[endpoints_sg],
                private_dns_enabled=True,
                # Without this, CDK adds its own default "allow from VPC
                # CIDR" ingress rule on top of endpoints_sg's explicit one
                # (using an Fn::GetAtt Vpc.CidrBlock reference rather than
                # the literal CIDR string) — functionally redundant, and it
                # crashes cdk-nag's EC23 check, which can't statically
                # analyze a non-primitive CIDR value.
                open=False,
            )
