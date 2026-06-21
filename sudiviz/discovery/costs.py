"""FinOps cost estimation for AWS resources.

Provides estimated monthly costs based on resource configurations.
Uses approximate on-demand pricing for us-east-1 as baseline.
"""
from __future__ import annotations

from typing import Optional
from .models import (
    AuroraCluster,
    DiscoveryResult,
    LoadBalancer,
    Instance,
    RDSInstance,
    LambdaFunction,
    S3Bucket,
    ECSCluster,
    EKSCluster,
    TargetGroup,
    SecurityGroup,
)

# Approximate hourly on-demand prices (us-east-1, USD)
# These are estimates - actual prices vary by region
EC2_HOURLY_PRICES = {
    "t2.micro": 0.0116,
    "t2.small": 0.023,
    "t2.medium": 0.0464,
    "t2.large": 0.0928,
    "t2.xlarge": 0.1856,
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "m6i.large": 0.096,
    "m6i.xlarge": 0.192,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
}

RDS_HOURLY_PRICES = {
    "db.t3.micro": 0.017,
    "db.t3.small": 0.034,
    "db.t3.medium": 0.068,
    "db.t3.large": 0.136,
    "db.m5.large": 0.171,
    "db.m5.xlarge": 0.342,
    "db.m5.2xlarge": 0.684,
    "db.r5.large": 0.24,
    "db.r5.xlarge": 0.48,
}

# ALB: $0.0225/hour + $0.008/LCU-hour (estimate ~$20-30/month base)
ALB_MONTHLY_BASE = 22.0

# NLB: $0.0225/hour + $0.006/NLCU-hour
NLB_MONTHLY_BASE = 18.0

# EKS cluster: $0.10/hour
EKS_HOURLY = 0.10

# Lambda: $0.20 per 1M requests + compute time (hard to estimate without usage)
LAMBDA_MONTHLY_ESTIMATE = 5.0

# S3: Varies widely, estimate based on existence
S3_MONTHLY_ESTIMATE = 2.0

HOURS_PER_MONTH = 730


def estimate_instance_cost(instance: Instance) -> float:
    """Estimate monthly cost for an EC2 instance."""
    if instance.state != "running":
        return 0.0

    hourly = EC2_HOURLY_PRICES.get(instance.instance_type, 0.05)  # Default fallback
    return hourly * HOURS_PER_MONTH


def estimate_rds_cost(rds: RDSInstance) -> float:
    """Estimate monthly cost for an RDS instance."""
    if rds.status not in ("available", "backing-up", "modifying"):
        return 0.0

    hourly = RDS_HOURLY_PRICES.get(rds.db_instance_class, 0.10)
    base = hourly * HOURS_PER_MONTH

    # Multi-AZ doubles the cost
    if rds.multi_az:
        base *= 2

    return base


def estimate_lb_cost(lb: LoadBalancer) -> float:
    """Estimate monthly cost for a load balancer."""
    if lb.state != "active":
        return 0.0

    if lb.type == "network":
        return NLB_MONTHLY_BASE
    return ALB_MONTHLY_BASE


def estimate_eks_cost(cluster: EKSCluster) -> float:
    """Estimate monthly cost for an EKS cluster (control plane only)."""
    if cluster.status != "ACTIVE":
        return 0.0
    return EKS_HOURLY * HOURS_PER_MONTH


def estimate_lambda_cost(fn: LambdaFunction) -> float:
    """Estimate monthly cost for Lambda (rough estimate without usage data)."""
    if fn.state != "Active":
        return 0.0
    return LAMBDA_MONTHLY_ESTIMATE


def estimate_aurora_cost(cluster: AuroraCluster) -> float:
    """Estimate monthly cost for an Aurora cluster.

    Aurora Serverless v2 is billed per ACU-hour; provisioned is billed per
    instance-hour. We use the RDS price table as a proxy for provisioned Aurora
    (Aurora instances share the same instance classes). Serverless gets a flat
    estimate based on typical minimum ACU usage.
    """
    if cluster.status not in ("available", "backing-up", "modifying"):
        return 0.0

    if cluster.engine_mode == "serverless":
        # Aurora Serverless v2: ~0.12 USD/ACU-hour, assume 2 ACU minimum average
        return 0.12 * 2 * HOURS_PER_MONTH

    # Provisioned: use RDS price table, multiply by instance count (min 1)
    hourly = RDS_HOURLY_PRICES.get("db.r6g.large", 0.26)  # common Aurora default
    count = max(cluster.instance_count, 1)
    return hourly * HOURS_PER_MONTH * count


def estimate_s3_cost(bucket: S3Bucket) -> float:
    """Estimate monthly cost for S3 bucket (rough estimate without size data)."""
    return S3_MONTHLY_ESTIMATE


def get_resource_cost(resource_type: str, resource: any) -> Optional[float]:
    """Get estimated monthly cost for a resource."""
    if resource_type == "instance":
        return estimate_instance_cost(resource)
    elif resource_type == "rds":
        return estimate_rds_cost(resource)
    elif resource_type == "aurora":
        return estimate_aurora_cost(resource)
    elif resource_type == "alb":
        return estimate_lb_cost(resource)
    elif resource_type == "eks_cluster":
        return estimate_eks_cost(resource)
    elif resource_type == "lambda":
        return estimate_lambda_cost(resource)
    elif resource_type == "s3":
        return estimate_s3_cost(resource)
    # Free resources
    elif resource_type in ("target_group", "security_group", "ecs_service"):
        return 0.0
    return None


def calculate_total_costs(discovery: DiscoveryResult) -> dict:
    """Calculate cost breakdown for all discovered resources."""
    costs = {
        "total_monthly": 0.0,
        "by_service": {},
        "by_resource": {},
    }

    # EC2 Instances
    ec2_total = 0.0
    for inst in discovery.instances:
        cost = estimate_instance_cost(inst)
        ec2_total += cost
        costs["by_resource"][inst.instance_id] = {
            "type": "EC2",
            "name": inst.tags.get("Name", inst.instance_id),
            "monthly_cost": cost,
            "instance_type": inst.instance_type,
        }
    costs["by_service"]["EC2"] = ec2_total

    # Load Balancers
    lb_total = 0.0
    for lb in discovery.load_balancers:
        cost = estimate_lb_cost(lb)
        lb_total += cost
        costs["by_resource"][lb.arn] = {
            "type": "ELB",
            "name": lb.name,
            "monthly_cost": cost,
            "lb_type": lb.type,
        }
    costs["by_service"]["ELB"] = lb_total

    # RDS
    rds_total = 0.0
    for rds in discovery.rds_instances:
        cost = estimate_rds_cost(rds)
        rds_total += cost
        costs["by_resource"][rds.arn] = {
            "type": "RDS",
            "name": rds.db_instance_id,
            "monthly_cost": cost,
            "instance_class": rds.db_instance_class,
        }
    costs["by_service"]["RDS"] = rds_total

    # Aurora
    aurora_total = 0.0
    for cluster in discovery.aurora_clusters:
        cost = estimate_aurora_cost(cluster)
        aurora_total += cost
        costs["by_resource"][cluster.arn] = {
            "type": "Aurora",
            "name": cluster.cluster_id,
            "monthly_cost": cost,
            "engine": cluster.engine,
            "engine_mode": cluster.engine_mode,
        }
    costs["by_service"]["Aurora"] = aurora_total

    # EKS
    eks_total = 0.0
    for cluster in discovery.eks_clusters:
        cost = estimate_eks_cost(cluster)
        eks_total += cost
        costs["by_resource"][cluster.arn] = {
            "type": "EKS",
            "name": cluster.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["EKS"] = eks_total

    # Lambda
    lambda_total = 0.0
    for fn in discovery.lambda_functions:
        cost = estimate_lambda_cost(fn)
        lambda_total += cost
        costs["by_resource"][fn.arn] = {
            "type": "Lambda",
            "name": fn.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["Lambda"] = lambda_total

    # S3
    s3_total = 0.0
    for bucket in discovery.s3_buckets:
        cost = estimate_s3_cost(bucket)
        s3_total += cost
        costs["by_resource"][bucket.arn] = {
            "type": "S3",
            "name": bucket.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["S3"] = s3_total

    # Calculate total
    costs["total_monthly"] = sum(costs["by_service"].values())

    return costs


def format_cost(cost: float) -> str:
    """Format cost as currency string."""
    if cost == 0:
        return "Free"
    elif cost < 1:
        return f"~${cost:.2f}/mo"
    elif cost < 100:
        return f"~${cost:.0f}/mo"
    else:
        return f"~${cost:,.0f}/mo"