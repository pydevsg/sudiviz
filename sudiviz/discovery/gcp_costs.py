"""FinOps cost estimation for GCP resources.

Provides estimated monthly costs based on resource configurations.
Uses approximate on-demand pricing for us-central1 as baseline.
"""
from __future__ import annotations

from .models import (
    DiscoveryResult,
    EKSCluster,
    Instance,
    LambdaFunction,
    LoadBalancer,
    RDSInstance,
    S3Bucket,
)

# Approximate hourly on-demand prices (us-central1, USD)
# These are estimates — actual prices vary by region.
GCE_HOURLY_PRICES = {
    "e2-micro": 0.00838,
    "e2-small": 0.01675,
    "e2-medium": 0.03351,
    "e2-standard-2": 0.06701,
    "e2-standard-4": 0.13402,
    "e2-standard-8": 0.26805,
    "e2-standard-16": 0.53609,
    "n1-standard-1": 0.0475,
    "n1-standard-2": 0.0950,
    "n1-standard-4": 0.1900,
    "n1-standard-8": 0.3800,
    "n1-standard-16": 0.7600,
    "n2-standard-2": 0.0971,
    "n2-standard-4": 0.1942,
    "n2-standard-8": 0.3885,
    "n2-standard-16": 0.7769,
    "n2d-standard-2": 0.0845,
    "n2d-standard-4": 0.1690,
    "n2d-standard-8": 0.3380,
    "c2-standard-4": 0.2088,
    "c2-standard-8": 0.4176,
    "c2-standard-16": 0.8352,
    "t2d-standard-1": 0.0422,
    "t2d-standard-2": 0.0845,
    "t2d-standard-4": 0.1690,
}

# Cloud SQL hourly prices (us-central1)
CLOUD_SQL_HOURLY_PRICES = {
    "db-f1-micro": 0.0150,
    "db-g1-small": 0.0500,
    "db-n1-standard-1": 0.0965,
    "db-n1-standard-2": 0.1930,
    "db-n1-standard-4": 0.3860,
    "db-n1-standard-8": 0.7720,
    "db-n1-standard-16": 1.5441,
    "db-custom-1-3840": 0.0965,
    "db-custom-2-7680": 0.1930,
    "db-custom-4-15360": 0.3860,
}

# Cloud Load Balancing: 5 forwarding rules included, then $0.025/hr/rule
# + $0.008/GB processed. We estimate a base of ~$18/month.
CLB_MONTHLY_BASE = 18.0

# GKE cluster: $0.10/hr for standard cluster, free for Autopilot.
GKE_HOURLY_STANDARD = 0.10
GKE_HOURLY_AUTOPILOT = 0.0

# Cloud Functions: $0.40 per million invocations + compute time
CLOUD_FUNCTIONS_MONTHLY_ESTIMATE = 5.0

# Cloud Storage: varies widely, estimate per bucket existence
GCS_MONTHLY_ESTIMATE = 2.0

HOURS_PER_MONTH = 730


def estimate_gce_cost(instance: Instance) -> float:
    """Estimate monthly cost for a GCE instance."""
    if instance.state != "running":
        return 0.0
    hourly = GCE_HOURLY_PRICES.get(instance.instance_type, 0.05)
    return hourly * HOURS_PER_MONTH


def estimate_cloud_sql_cost(rds: RDSInstance) -> float:
    """Estimate monthly cost for a Cloud SQL instance."""
    active_states = ("runnable", "available", "running")
    if rds.status.lower() not in active_states:
        return 0.0
    hourly = CLOUD_SQL_HOURLY_PRICES.get(rds.db_instance_class, 0.10)
    base = hourly * HOURS_PER_MONTH
    # Regional (HA) doubles the cost.
    if rds.multi_az:
        base *= 2
    return base


def estimate_clb_cost(lb: LoadBalancer) -> float:
    """Estimate monthly cost for a Cloud Load Balancer."""
    if lb.state != "active":
        return 0.0
    return CLB_MONTHLY_BASE


def estimate_gke_cost(cluster: EKSCluster) -> float:
    """Estimate monthly cost for a GKE cluster (control plane only)."""
    if cluster.status != "ACTIVE":
        return 0.0
    # Autopilot clusters have a different pricing model; we detect by
    # checking if the cluster name hints at autopilot. Without the raw
    # cluster object, we conservatively assume standard.
    return GKE_HOURLY_STANDARD * HOURS_PER_MONTH


def estimate_cloud_function_cost(fn: LambdaFunction) -> float:
    """Estimate monthly cost for a Cloud Function."""
    if fn.state != "Active":
        return 0.0
    return CLOUD_FUNCTIONS_MONTHLY_ESTIMATE


def estimate_gcs_cost(bucket: S3Bucket) -> float:
    """Estimate monthly cost for a Cloud Storage bucket."""
    return GCS_MONTHLY_ESTIMATE


def calculate_gcp_total_costs(discovery: DiscoveryResult) -> dict:
    """Calculate cost breakdown for all discovered GCP resources."""
    costs: dict = {
        "total_monthly": 0.0,
        "by_service": {},
        "by_resource": {},
    }

    # Compute Engine
    gce_total = 0.0
    for inst in discovery.instances:
        cost = estimate_gce_cost(inst)
        gce_total += cost
        costs["by_resource"][inst.instance_id] = {
            "type": "Compute Engine",
            "name": inst.tags.get("Name", inst.instance_id),
            "monthly_cost": cost,
            "instance_type": inst.instance_type,
        }
    costs["by_service"]["Compute Engine"] = gce_total

    # Cloud Load Balancing
    clb_total = 0.0
    for lb in discovery.load_balancers:
        cost = estimate_clb_cost(lb)
        clb_total += cost
        costs["by_resource"][lb.arn] = {
            "type": "Cloud Load Balancing",
            "name": lb.name,
            "monthly_cost": cost,
            "lb_type": lb.type,
        }
    costs["by_service"]["Cloud Load Balancing"] = clb_total

    # Cloud SQL
    sql_total = 0.0
    for rds in discovery.rds_instances:
        cost = estimate_cloud_sql_cost(rds)
        sql_total += cost
        costs["by_resource"][rds.arn] = {
            "type": "Cloud SQL",
            "name": rds.db_instance_id,
            "monthly_cost": cost,
            "instance_class": rds.db_instance_class,
        }
    costs["by_service"]["Cloud SQL"] = sql_total

    # GKE
    gke_total = 0.0
    for cluster in discovery.eks_clusters:
        cost = estimate_gke_cost(cluster)
        gke_total += cost
        costs["by_resource"][cluster.arn] = {
            "type": "GKE",
            "name": cluster.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["GKE"] = gke_total

    # Cloud Functions
    functions_total = 0.0
    for fn in discovery.lambda_functions:
        cost = estimate_cloud_function_cost(fn)
        functions_total += cost
        costs["by_resource"][fn.arn] = {
            "type": "Cloud Functions",
            "name": fn.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["Cloud Functions"] = functions_total

    # Cloud Storage
    gcs_total = 0.0
    for bucket in discovery.s3_buckets:
        cost = estimate_gcs_cost(bucket)
        gcs_total += cost
        costs["by_resource"][bucket.arn] = {
            "type": "Cloud Storage",
            "name": bucket.name,
            "monthly_cost": cost,
        }
    costs["by_service"]["Cloud Storage"] = gcs_total

    costs["total_monthly"] = sum(costs["by_service"].values())
    return costs
