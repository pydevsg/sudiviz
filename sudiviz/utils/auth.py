"""AWS authentication helpers.

We never accept credentials as CLI arguments. Authentication relies on the
boto3 default chain (env vars, shared credentials file, instance profile,
SSO, etc.). This module wraps session construction and exposes account/region
metadata that the visualizer surfaces in the status bar.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AwsIdentity:
    """Resolved caller identity — shown in the TUI/web status bar."""

    account_id: str
    arn: str
    user_id: str
    region: str


def build_botocore_config(
    max_attempts: int = 8,
    connect_timeout: int = 5,
    read_timeout: int = 30,
) -> Config:
    """Construct a botocore Config with adaptive retries + jittered backoff.

    The "adaptive" retry mode applies exponential backoff with jitter and
    automatically throttles in response to TooManyRequestsException, which is
    exactly what we want during parallel discovery of large VPCs.
    """
    return Config(
        retries={"max_attempts": max_attempts, "mode": "adaptive"},
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        user_agent_extra="sudiviz/0.3.0",
    )


@lru_cache(maxsize=8)
def get_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    """Return a memoized boto3 Session.

    `profile` and `region` are optional — when None, boto3 falls back to its
    standard credential resolution (env, ~/.aws/credentials, IMDS, SSO, etc.).
    """
    # Guard against Typer passing OptionInfo objects instead of resolved values.
    safe_profile = profile if isinstance(profile, str) else None
    safe_region = region if isinstance(region, str) else None
    kwargs: dict = {}
    if safe_profile:
        kwargs["profile_name"] = safe_profile
    if safe_region:
        kwargs["region_name"] = safe_region
    return boto3.Session(**kwargs)


def whoami(session: Optional[boto3.Session] = None) -> AwsIdentity:
    """Resolve the caller identity using STS.

    Raises a clear error message if credentials are missing or invalid — this
    is the first call sudiviz makes, so a good error here saves debugging.
    """
    sess = session or get_session()
    try:
        sts = sess.client("sts", config=build_botocore_config())
        ident = sts.get_caller_identity()
    except NoCredentialsError as exc:
        raise RuntimeError(
            "No AWS credentials found. Configure via env vars, ~/.aws/credentials, "
            "or an instance profile. sudiviz never accepts credentials as flags."
        ) from exc
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to resolve AWS identity: {exc}") from exc

    region = sess.region_name or "us-east-1"
    return AwsIdentity(
        account_id=ident["Account"],
        arn=ident["Arn"],
        user_id=ident["UserId"],
        region=region,
    )


def console_url(resource_type: str, resource_id: str, region: str) -> str:
    """Build an AWS Console deep link for a resource.

    Used by the web visualization so clicking a node jumps straight to the
    relevant Console page. `resource_type` is the sudiviz-internal kind:
    'alb', 'target_group', 'instance', 'security_group', 'vpc'.
    """
    base = f"https://{region}.console.aws.amazon.com"
    rt = resource_type.lower()
    if rt == "alb":
        return f"{base}/ec2/home?region={region}#LoadBalancer:loadBalancerArn={resource_id}"
    if rt == "target_group":
        return f"{base}/ec2/home?region={region}#TargetGroup:targetGroupArn={resource_id}"
    if rt == "instance":
        return f"{base}/ec2/home?region={region}#InstanceDetails:instanceId={resource_id}"
    if rt == "security_group":
        return f"{base}/ec2/home?region={region}#SecurityGroup:groupId={resource_id}"
    if rt == "vpc":
        return f"{base}/vpc/home?region={region}#VpcDetails:VpcId={resource_id}"
    if rt == "ecs_cluster":
        # ARN: arn:aws:ecs:<region>:<account>:cluster/<name>
        # Split on "cluster/" to handle names with slashes safely.
        cluster_name = resource_id.split("cluster/")[-1] if "cluster/" in resource_id else resource_id.split("/")[-1]
        return f"{base}/ecs/v2/clusters/{cluster_name}/services?region={region}"
    if rt == "ecs_service":
        # ARN: arn:aws:ecs:<region>:<account>:service/<cluster-name>/<service-name>
        # e.g. arn:aws:ecs:us-east-1:123456:service/my-cluster/my-service
        svc_part = resource_id.split("service/")[-1] if "service/" in resource_id else resource_id
        parts = svc_part.split("/")
        cluster_name = parts[0] if len(parts) >= 2 else "unknown"
        service_name = parts[-1]
        return f"{base}/ecs/v2/clusters/{cluster_name}/services/{service_name}?region={region}"
    if rt == "eks_cluster":
        cluster_name = resource_id.split("/")[-1]
        return f"{base}/eks/home?region={region}#/clusters/{cluster_name}"
    if rt == "eks_nodegroup":
        # ARN: arn:aws:eks:<region>:<account>:nodegroup/<cluster>/<ng>/<id>
        parts = resource_id.split("/")
        cluster_name = parts[-3] if len(parts) >= 3 else "unknown"
        ng_name = parts[-2] if len(parts) >= 2 else resource_id
        return f"{base}/eks/home?region={region}#/clusters/{cluster_name}/nodegroups/{ng_name}"
    if rt == "rds":
        db_id = resource_id.split(":")[-1]
        return f"{base}/rds/home?region={region}#database:id={db_id};is-cluster=false"
    if rt == "lambda":
        fn_name = resource_id.split(":")[-1]
        return f"{base}/lambda/home?region={region}#/functions/{fn_name}"
    if rt == "s3":
        bucket_name = resource_id.replace("arn:aws:s3:::", "")
        return f"https://s3.console.aws.amazon.com/s3/buckets/{bucket_name}?region={region}"
    return f"{base}/console/home?region={region}"


def cloudwatch_metrics_url(resource_type: str, resource_id: str, region: str) -> str | None:
    """Build a CloudWatch metrics dashboard URL for a resource.

    Returns None for resource types that don't have standard CloudWatch metrics.
    """
    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    rt = resource_type.lower()

    if rt == "instance":
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fEC2*2cInstanceId*7d*20{resource_id}"
    if rt == "alb":
        # Extract ALB name from ARN: arn:aws:elasticloadbalancing:region:account:loadbalancer/app/name/id
        lb_suffix = resource_id.split("loadbalancer/")[-1] if "loadbalancer/" in resource_id else resource_id
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fApplicationELB*2cLoadBalancer*7d*20{lb_suffix}"
    if rt == "target_group":
        # Extract TG name from ARN
        tg_suffix = resource_id.split("targetgroup/")[-1] if "targetgroup/" in resource_id else resource_id
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fApplicationELB*2cTargetGroup*7d*20{tg_suffix}"
    if rt == "rds":
        db_id = resource_id.split(":")[-1]
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fRDS*2cDBInstanceIdentifier*7d*20{db_id}"
    if rt == "lambda":
        fn_name = resource_id.split(":")[-1]
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fLambda*2cFunctionName*7d*20{fn_name}"
    if rt == "ecs_service":
        # Extract cluster and service from ARN
        svc_part = resource_id.split("service/")[-1] if "service/" in resource_id else resource_id
        parts = svc_part.split("/")
        cluster_name = parts[0] if len(parts) >= 2 else "unknown"
        service_name = parts[-1]
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fECS*2cClusterName*2cServiceName*7d*20{cluster_name}*20{service_name}"
    if rt == "ecs_cluster":
        cluster_name = resource_id.split("cluster/")[-1] if "cluster/" in resource_id else resource_id.split("/")[-1]
        return f"{base}#metricsV2:graph=~();query=~'*7bAWS*2fECS*2cClusterName*7d*20{cluster_name}"

    return None


def cloudwatch_logs_url(resource_type: str, resource_id: str, region: str) -> str | None:
    """Build a CloudWatch Logs URL for a resource.

    Returns None for resource types that don't have standard log groups.
    """
    base = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
    rt = resource_type.lower()

    if rt == "lambda":
        fn_name = resource_id.split(":")[-1]
        log_group = f"/aws/lambda/{fn_name}"
        # URL-encode the log group path
        encoded = log_group.replace("/", "*2f")
        return f"{base}#logsV2:log-groups/log-group/{encoded}"
    if rt == "ecs_service":
        # ECS services typically log to /ecs/<service-name> or custom log groups
        svc_part = resource_id.split("service/")[-1] if "service/" in resource_id else resource_id
        parts = svc_part.split("/")
        service_name = parts[-1]
        log_group = f"/ecs/{service_name}"
        encoded = log_group.replace("/", "*2f")
        return f"{base}#logsV2:log-groups/log-group/{encoded}"
    if rt == "rds":
        db_id = resource_id.split(":")[-1]
        # RDS logs are under /aws/rds/instance/<db-id>/<log-type>
        log_group = f"/aws/rds/instance/{db_id}"
        encoded = log_group.replace("/", "*2f")
        return f"{base}#logsV2:log-groups$3FlogGroupNameFilter$3D{encoded}"
    if rt == "eks_cluster":
        cluster_name = resource_id.split("/")[-1]
        log_group = f"/aws/eks/{cluster_name}"
        encoded = log_group.replace("/", "*2f")
        return f"{base}#logsV2:log-groups$3FlogGroupNameFilter$3D{encoded}"

    return None
