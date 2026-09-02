"""Pydantic v2 models — the cloud-agnostic data contract.

These types form the boundary between provider-specific discovery code
(`discovery/aws.py`, future `discovery/azure.py`) and downstream graph/visual
modules. AWS-specific bits live in `provider_metadata` so we can add Azure/GCP
without changing the graph builder.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class CloudProvider(str, Enum):
    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class HealthStatus(str, Enum):
    """Normalized health states. Provider-specific states map onto these."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    INITIAL = "initial"
    DRAINING = "draining"
    UNUSED = "unused"
    UNKNOWN = "unknown"


class _Base(BaseModel):
    """Project-wide model defaults."""

    model_config = ConfigDict(extra="allow", frozen=False, populate_by_name=True)


class SecurityGroupRule(_Base):
    """A single ingress/egress rule on a security group."""

    direction: str  # 'ingress' | 'egress'
    protocol: str   # 'tcp', 'udp', 'icmp', '-1'
    from_port: Optional[int] = None
    to_port: Optional[int] = None
    cidr_ranges: list[str] = Field(default_factory=list)
    referenced_sg_ids: list[str] = Field(default_factory=list)
    description: Optional[str] = None


class SecurityGroup(_Base):
    """Cloud-agnostic security group representation."""

    provider: CloudProvider = CloudProvider.AWS
    sg_id: str
    name: str
    vpc_id: Optional[str] = None
    rules: list[SecurityGroupRule] = Field(default_factory=list)
    attached_to: list[str] = Field(default_factory=list)  # ENI/instance IDs
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_orphan(self) -> bool:
        """A security group is an orphan if nothing references it."""
        return len(self.attached_to) == 0


class Target(_Base):
    """A single backend target inside a target group."""

    target_id: str  # instance-id, IP, or Lambda ARN
    target_type: str  # 'instance' | 'ip' | 'lambda'
    port: Optional[int] = None
    health: HealthStatus = HealthStatus.UNKNOWN
    health_reason: Optional[str] = None
    health_description: Optional[str] = None


class TargetGroup(_Base):
    """Cloud-agnostic target group / backend pool."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    protocol: str
    port: int
    vpc_id: Optional[str] = None
    targets: list[Target] = Field(default_factory=list)
    associated_lb_arns: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def healthy_count(self) -> int:
        return sum(1 for t in self.targets if t.health == HealthStatus.HEALTHY)

    @property
    def total_count(self) -> int:
        return len(self.targets)

    @property
    def is_orphan(self) -> bool:
        """No load balancer routes to this target group → orphan."""
        return len(self.associated_lb_arns) == 0


class Listener(_Base):
    """Listener rule on a load balancer.

    Critical for orphan detection: a target group with no listener pointing at
    it is unreachable even if its backends are healthy.
    """

    arn: str
    lb_arn: str
    protocol: str
    port: int
    default_target_group_arns: list[str] = Field(default_factory=list)
    rule_target_group_arns: list[str] = Field(default_factory=list)


class LoadBalancer(_Base):
    """Cloud-agnostic load balancer (ALB/NLB/Azure LB/GCP LB)."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    dns_name: Optional[str] = None
    scheme: str = "internet-facing"  # or 'internal'
    type: str = "application"        # 'application' | 'network' | 'gateway'
    vpc_id: Optional[str] = None
    state: str = "unknown"
    security_group_ids: list[str] = Field(default_factory=list)
    subnet_ids: list[str] = Field(default_factory=list)
    listeners: list[Listener] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)


class Instance(_Base):
    """EC2 instance / VM."""

    provider: CloudProvider = CloudProvider.AWS
    instance_id: str
    instance_type: Optional[str] = None
    state: str = "unknown"
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    security_group_ids: list[str] = Field(default_factory=list)
    target_group_arns: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_orphan(self) -> bool:
        """Instance not registered with any target group is unreachable via LB."""
        return len(self.target_group_arns) == 0


class ECSService(_Base):
    """An ECS service within a cluster."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    cluster_arn: str
    status: str = "UNKNOWN"          # ACTIVE | DRAINING | INACTIVE
    desired_count: int = 0
    running_count: int = 0
    pending_count: int = 0
    launch_type: str = "EC2"         # EC2 | FARGATE | EXTERNAL
    task_definition: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_ids: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    load_balancer_arns: list[str] = Field(default_factory=list)
    target_group_arns: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.running_count >= self.desired_count and self.desired_count > 0


class ECSCluster(_Base):
    """An ECS cluster (container for services)."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    status: str = "ACTIVE"
    active_services_count: int = 0
    running_tasks_count: int = 0
    pending_tasks_count: int = 0
    container_instances_count: int = 0
    capacity_providers: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    services: list[ECSService] = Field(default_factory=list)


class EKSNodeGroup(_Base):
    """A managed node group within an EKS cluster."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    cluster_name: str
    status: str = "UNKNOWN"
    capacity_type: str = "ON_DEMAND"
    instance_types: list[str] = Field(default_factory=list)
    desired_size: int = 0
    min_size: int = 0
    max_size: int = 0
    tags: dict[str, str] = Field(default_factory=dict)


class EKSCluster(_Base):
    """An EKS cluster."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    status: str = "UNKNOWN"          # ACTIVE | CREATING | DELETING | FAILED | UPDATING
    version: Optional[str] = None
    endpoint: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_ids: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    node_groups: list[EKSNodeGroup] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)


class RDSInstance(_Base):
    """An RDS database instance."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    db_instance_id: str
    db_instance_class: str
    engine: str
    engine_version: Optional[str] = None
    status: str = "unknown"          # available | stopped | starting | stopping | ...
    endpoint_address: Optional[str] = None
    endpoint_port: Optional[int] = None
    vpc_id: Optional[str] = None
    subnet_group: Optional[str] = None
    security_group_ids: list[str] = Field(default_factory=list)
    multi_az: bool = False
    publicly_accessible: bool = False
    storage_encrypted: bool = False
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.status == "available"


class AuroraCluster(_Base):
    """An Aurora DB cluster (serverless or provisioned)."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    cluster_id: str
    engine: str                       # aurora-mysql | aurora-postgresql
    engine_version: Optional[str] = None
    engine_mode: str = "provisioned"  # provisioned | serverless
    status: str = "unknown"           # available | stopped | creating | ...
    endpoint: Optional[str] = None    # writer endpoint
    reader_endpoint: Optional[str] = None
    port: Optional[int] = None
    vpc_id: Optional[str] = None
    subnet_group: Optional[str] = None
    security_group_ids: list[str] = Field(default_factory=list)
    multi_az: bool = False
    storage_encrypted: bool = False
    deletion_protection: bool = False
    instance_count: int = 0
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.status == "available"


class LambdaFunction(_Base):
    """An AWS Lambda function."""

    provider: CloudProvider = CloudProvider.AWS
    arn: str
    name: str
    runtime: Optional[str] = None
    state: str = "Unknown"           # Active | Inactive | Pending | Failed
    last_modified: Optional[str] = None
    memory_size: int = 128
    timeout: int = 3
    vpc_id: Optional[str] = None
    subnet_ids: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    # Which other resources invoke this function (e.g. ALB target group ARN)
    event_source_arns: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.state == "Active"


class S3Bucket(_Base):
    """An S3 bucket (region-level, not VPC-bound)."""

    provider: CloudProvider = CloudProvider.AWS
    name: str
    arn: str
    region: Optional[str] = None
    creation_date: Optional[str] = None
    versioning_enabled: bool = False
    public_access_blocked: bool = True
    encryption_enabled: bool = False
    tags: dict[str, str] = Field(default_factory=dict)


class DiscoveryResult(_Base):
    """Aggregate output of one discovery pass — everything needed to build a graph."""

    provider: CloudProvider = CloudProvider.AWS
    account_id: Optional[str] = None
    project_id: Optional[str] = None  # GCP project ID (AWS uses account_id)
    region: Optional[str] = None
    vpc_id: Optional[str] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    load_balancers: list[LoadBalancer] = Field(default_factory=list)
    target_groups: list[TargetGroup] = Field(default_factory=list)
    instances: list[Instance] = Field(default_factory=list)
    security_groups: list[SecurityGroup] = Field(default_factory=list)
    ecs_clusters: list[ECSCluster] = Field(default_factory=list)
    eks_clusters: list[EKSCluster] = Field(default_factory=list)
    rds_instances: list[RDSInstance] = Field(default_factory=list)
    aurora_clusters: list[AuroraCluster] = Field(default_factory=list)
    lambda_functions: list[LambdaFunction] = Field(default_factory=list)
    s3_buckets: list[S3Bucket] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def index_by_arn(self) -> dict[str, Any]:
        """Flat ARN/ID lookup table — used by the graph builder."""
        idx: dict[str, Any] = {}
        for lb in self.load_balancers:
            idx[lb.arn] = lb
        for tg in self.target_groups:
            idx[tg.arn] = tg
        for sg in self.security_groups:
            idx[sg.sg_id] = sg
        for inst in self.instances:
            idx[inst.instance_id] = inst
        for cluster in self.ecs_clusters:
            idx[cluster.arn] = cluster
            for svc in cluster.services:
                idx[svc.arn] = svc
        for cluster in self.eks_clusters:
            idx[cluster.arn] = cluster
        for db in self.rds_instances:
            idx[db.arn] = db
        for cluster in self.aurora_clusters:
            idx[cluster.arn] = cluster
        for fn in self.lambda_functions:
            idx[fn.arn] = fn
        for bucket in self.s3_buckets:
            idx[bucket.arn] = bucket
        return idx
