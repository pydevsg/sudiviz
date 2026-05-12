"""Build a NetworkX DiGraph from a `DiscoveryResult`.

Node attributes:
    kind        : 'alb' | 'target_group' | 'instance' | 'security_group' | 'vpc'
                  'ecs_cluster' | 'ecs_service' | 'eks_cluster' | 'eks_nodegroup'
                  'rds' | 'lambda' | 's3'
    label       : human-readable display name
    health      : HealthStatus value (string)
    arn / id    : provider-side identifier
    metadata    : full Pydantic dump for downstream consumers
    orphan      : bool — annotated by analyzer.mark_orphaned_edges

Edge attributes:
    relation    : 'forwards_to' | 'registered_in' | 'guarded_by' | 'in_vpc'
                  'contains' | 'backed_by' | 'invokes' | 'event_source'
    style       : 'solid' (default) or 'dashed' (orphan, set by analyzer)
    color       : hex color (set by visualizer)
"""
from __future__ import annotations

from typing import Any

import networkx as nx

from sudiviz.discovery.models import DiscoveryResult, HealthStatus


def build_graph(discovery: DiscoveryResult) -> nx.DiGraph:
    """Build a directed graph encoding the discovered topology."""
    g: nx.DiGraph = nx.DiGraph()
    g.graph["account_id"] = discovery.account_id
    g.graph["region"] = discovery.region
    g.graph["vpc_id"] = discovery.vpc_id
    g.graph["discovered_at"] = discovery.discovered_at.isoformat()

    if discovery.vpc_id:
        g.add_node(
            discovery.vpc_id,
            kind="vpc",
            label=f"VPC {discovery.vpc_id}",
            health=HealthStatus.HEALTHY.value,
            id=discovery.vpc_id,
            metadata={},
            orphan=False,
        )

    # Load balancers.
    for lb in discovery.load_balancers:
        g.add_node(
            lb.arn,
            kind="alb",
            label=lb.name,
            health=HealthStatus.HEALTHY.value if lb.state == "active" else HealthStatus.UNKNOWN.value,
            arn=lb.arn,
            id=lb.arn,
            dns_name=lb.dns_name,
            metadata=lb.model_dump(mode="json"),
            orphan=False,
        )
        if lb.vpc_id and lb.vpc_id in g:
            g.add_edge(lb.arn, lb.vpc_id, relation="in_vpc", style="solid")

        # SG attachments.
        for sg_id in lb.security_group_ids:
            g.add_edge(lb.arn, sg_id, relation="guarded_by", style="solid")

        # Listener → target_group edges (the only edges that prove reachability).
        for lst in lb.listeners:
            for tg_arn in (*lst.default_target_group_arns, *lst.rule_target_group_arns):
                g.add_edge(
                    lb.arn,
                    tg_arn,
                    relation="forwards_to",
                    style="solid",
                    listener_arn=lst.arn,
                    listener_port=lst.port,
                    listener_protocol=lst.protocol,
                )

    # Target groups.
    for tg in discovery.target_groups:
        health = _aggregate_tg_health(tg)
        g.add_node(
            tg.arn,
            kind="target_group",
            label=tg.name,
            health=health.value,
            arn=tg.arn,
            id=tg.arn,
            healthy_count=tg.healthy_count,
            total_count=tg.total_count,
            protocol=tg.protocol,
            port=tg.port,
            metadata=tg.model_dump(mode="json"),
            orphan=False,
        )

        # Target → target_group edges (instance registration).
        for target in tg.targets:
            target_node = target.target_id
            if target_node not in g:
                g.add_node(
                    target_node,
                    kind="instance" if target.target_type == "instance" else target.target_type,
                    label=target_node,
                    health=target.health.value,
                    id=target_node,
                    metadata={"target_type": target.target_type},
                    orphan=False,
                )
            g.add_edge(
                target_node,
                tg.arn,
                relation="registered_in",
                style="solid",
                health=target.health.value,
                health_reason=target.health_reason,
            )

    # Instances (may already exist as nodes from target group registration).
    for inst in discovery.instances:
        if inst.instance_id in g:
            existing = g.nodes[inst.instance_id]
            existing["metadata"] = inst.model_dump(mode="json")
            existing["label"] = inst.tags.get("Name") or inst.instance_id
        else:
            g.add_node(
                inst.instance_id,
                kind="instance",
                label=inst.tags.get("Name") or inst.instance_id,
                health=HealthStatus.HEALTHY.value if inst.state == "running" else HealthStatus.UNHEALTHY.value,
                id=inst.instance_id,
                metadata=inst.model_dump(mode="json"),
                orphan=False,
            )
        for sg_id in inst.security_group_ids:
            g.add_edge(inst.instance_id, sg_id, relation="guarded_by", style="solid")

    # Security groups.
    for sg in discovery.security_groups:
        if sg.sg_id in g:
            g.nodes[sg.sg_id].update(
                kind="security_group",
                label=sg.name or sg.sg_id,
                health=HealthStatus.HEALTHY.value,
                id=sg.sg_id,
                metadata=sg.model_dump(mode="json"),
                orphan=False,
            )
        else:
            g.add_node(
                sg.sg_id,
                kind="security_group",
                label=sg.name or sg.sg_id,
                health=HealthStatus.HEALTHY.value,
                id=sg.sg_id,
                metadata=sg.model_dump(mode="json"),
                orphan=False,
            )

    # ECS clusters + services.
    for cluster in discovery.ecs_clusters:
        cluster_health = HealthStatus.HEALTHY.value if cluster.status == "ACTIVE" else HealthStatus.UNKNOWN.value
        g.add_node(
            cluster.arn,
            kind="ecs_cluster",
            label=cluster.name,
            health=cluster_health,
            arn=cluster.arn,
            id=cluster.arn,
            metadata=cluster.model_dump(mode="json", exclude={"services"}),
            orphan=False,
        )
        for svc in cluster.services:
            svc_health = HealthStatus.HEALTHY.value if svc.is_healthy else HealthStatus.UNHEALTHY.value
            g.add_node(
                svc.arn,
                kind="ecs_service",
                label=svc.name,
                health=svc_health,
                arn=svc.arn,
                id=svc.arn,
                running_count=svc.running_count,
                desired_count=svc.desired_count,
                launch_type=svc.launch_type,
                metadata=svc.model_dump(mode="json"),
                orphan=False,
            )
            g.add_edge(cluster.arn, svc.arn, relation="contains", style="solid")
            # Link service → target groups (service is backed by TG).
            for tg_arn in svc.target_group_arns:
                if tg_arn in g:
                    g.add_edge(svc.arn, tg_arn, relation="backed_by", style="solid")
            # Link service → security groups.
            for sg_id in svc.security_group_ids:
                g.add_edge(svc.arn, sg_id, relation="guarded_by", style="solid")

    # EKS clusters + node groups.
    for cluster in discovery.eks_clusters:
        cluster_health = HealthStatus.HEALTHY.value if cluster.status == "ACTIVE" else HealthStatus.UNKNOWN.value
        g.add_node(
            cluster.arn,
            kind="eks_cluster",
            label=cluster.name,
            health=cluster_health,
            arn=cluster.arn,
            id=cluster.arn,
            version=cluster.version,
            metadata=cluster.model_dump(mode="json", exclude={"node_groups"}),
            orphan=False,
        )
        if cluster.vpc_id and cluster.vpc_id in g:
            g.add_edge(cluster.arn, cluster.vpc_id, relation="in_vpc", style="solid")
        for sg_id in cluster.security_group_ids:
            g.add_edge(cluster.arn, sg_id, relation="guarded_by", style="solid")
        for ng in cluster.node_groups:
            ng_health = HealthStatus.HEALTHY.value if ng.status == "ACTIVE" else HealthStatus.UNKNOWN.value
            g.add_node(
                ng.arn,
                kind="eks_nodegroup",
                label=ng.name,
                health=ng_health,
                arn=ng.arn,
                id=ng.arn,
                desired_size=ng.desired_size,
                capacity_type=ng.capacity_type,
                metadata=ng.model_dump(mode="json"),
                orphan=False,
            )
            g.add_edge(cluster.arn, ng.arn, relation="contains", style="solid")

    # RDS instances.
    for db in discovery.rds_instances:
        db_health = HealthStatus.HEALTHY.value if db.is_healthy else HealthStatus.UNHEALTHY.value
        g.add_node(
            db.arn,
            kind="rds",
            label=db.db_instance_id,
            health=db_health,
            arn=db.arn,
            id=db.arn,
            engine=db.engine,
            status=db.status,
            metadata=db.model_dump(mode="json"),
            orphan=False,
        )
        if db.vpc_id and db.vpc_id in g:
            g.add_edge(db.arn, db.vpc_id, relation="in_vpc", style="solid")
        for sg_id in db.security_group_ids:
            g.add_edge(db.arn, sg_id, relation="guarded_by", style="solid")

    # Lambda functions.
    for fn in discovery.lambda_functions:
        fn_health = HealthStatus.HEALTHY.value if fn.is_healthy else HealthStatus.UNHEALTHY.value
        g.add_node(
            fn.arn,
            kind="lambda",
            label=fn.name,
            health=fn_health,
            arn=fn.arn,
            id=fn.arn,
            runtime=fn.runtime,
            metadata=fn.model_dump(mode="json"),
            orphan=False,
        )
        if fn.vpc_id and fn.vpc_id in g:
            g.add_edge(fn.arn, fn.vpc_id, relation="in_vpc", style="solid")
        for sg_id in fn.security_group_ids:
            g.add_edge(fn.arn, sg_id, relation="guarded_by", style="solid")
        # Lambda as ALB target: if any TG has this function's ARN as a target_id,
        # the registered_in edge was already added via the target-group loop above.
        for src_arn in fn.event_source_arns:
            if src_arn in g:
                g.add_edge(src_arn, fn.arn, relation="invokes", style="solid")

    # S3 buckets (account-level, no VPC edges).
    for bucket in discovery.s3_buckets:
        security_health = HealthStatus.HEALTHY.value if bucket.public_access_blocked else HealthStatus.UNHEALTHY.value
        g.add_node(
            bucket.arn,
            kind="s3",
            label=bucket.name,
            health=security_health,
            arn=bucket.arn,
            id=bucket.arn,
            region=bucket.region,
            public_access_blocked=bucket.public_access_blocked,
            encryption_enabled=bucket.encryption_enabled,
            metadata=bucket.model_dump(mode="json"),
            orphan=False,
        )

    return g


def _aggregate_tg_health(tg: Any) -> HealthStatus:
    """Reduce per-target health into a single TG-level status.

    Rules:
        all healthy    → HEALTHY
        none           → UNKNOWN (no targets registered yet — also flagged as orphan)
        any unhealthy  → UNHEALTHY
        only initial   → INITIAL
        otherwise      → UNHEALTHY (mixed states get the worst value visualized)
    """
    if not tg.targets:
        return HealthStatus.UNKNOWN
    states = {t.health for t in tg.targets}
    if states == {HealthStatus.HEALTHY}:
        return HealthStatus.HEALTHY
    if HealthStatus.UNHEALTHY in states:
        return HealthStatus.UNHEALTHY
    if states == {HealthStatus.INITIAL}:
        return HealthStatus.INITIAL
    return HealthStatus.UNHEALTHY
