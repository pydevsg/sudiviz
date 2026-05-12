"""Diagnose connectivity failures and detect orphans.

Orphan detection algorithm
--------------------------
A resource is "orphan" if no path through the topology reaches it from a
client-facing entry point. We approximate this with cheap, local rules:

  * Target group is orphan if NO ALB has a listener that forwards to it
    (i.e. no incoming edge of relation 'forwards_to').
  * Instance is orphan if it appears in NO target group
    (i.e. no outgoing 'registered_in' edge).
  * Security group is orphan if nothing is attached to it
    (i.e. no incoming 'guarded_by' edge).

These rules catch the >90% of real-world misconfigurations (forgotten test
fixtures, half-deleted stacks, drifted Terraform). The Reachability Analyzer
catches the rest, but is paid + slow, so it's opt-in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx

from sudiviz.discovery.models import HealthStatus
from sudiviz.utils.branding import Colors

logger = logging.getLogger(__name__)


@dataclass
class Fix:
    """One actionable fix suggestion shown in the sidebar."""

    severity: str  # 'critical' | 'warning' | 'info'
    title: str
    detail: str
    resource_id: Optional[str] = None
    aws_console_hint: Optional[str] = None


@dataclass
class Diagnosis:
    """Full diagnostic output: orphans + targeted fixes."""

    orphan_target_groups: list[dict] = field(default_factory=list)
    orphan_instances: list[dict] = field(default_factory=list)
    orphan_security_groups: list[dict] = field(default_factory=list)
    unhealthy_target_groups: list[dict] = field(default_factory=list)
    unhealthy_ecs_services: list[dict] = field(default_factory=list)
    unhealthy_eks_clusters: list[dict] = field(default_factory=list)
    unhealthy_rds_instances: list[dict] = field(default_factory=list)
    insecure_s3_buckets: list[dict] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "orphan_target_groups": self.orphan_target_groups,
            "orphan_instances": self.orphan_instances,
            "orphan_security_groups": self.orphan_security_groups,
            "unhealthy_target_groups": self.unhealthy_target_groups,
            "unhealthy_ecs_services": self.unhealthy_ecs_services,
            "unhealthy_eks_clusters": self.unhealthy_eks_clusters,
            "unhealthy_rds_instances": self.unhealthy_rds_instances,
            "insecure_s3_buckets": self.insecure_s3_buckets,
            "fixes": [f.__dict__ for f in self.fixes],
        }


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def find_unattached_target_groups(graph: nx.DiGraph) -> list[dict]:
    """Target groups with NO incoming 'forwards_to' edge from any listener."""
    out: list[dict] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "target_group":
            continue
        incoming = [
            (u, _v, e) for u, _v, e in graph.in_edges(node, data=True)
            if e.get("relation") == "forwards_to"
        ]
        if not incoming:
            out.append({"id": node, "label": attrs.get("label"), "kind": "target_group"})
    return out


def find_unattached_instances(graph: nx.DiGraph) -> list[dict]:
    """Instances NOT registered in any target group."""
    out: list[dict] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "instance":
            continue
        outgoing = [
            (_u, v, e) for _u, v, e in graph.out_edges(node, data=True)
            if e.get("relation") == "registered_in"
        ]
        if not outgoing:
            out.append({"id": node, "label": attrs.get("label"), "kind": "instance"})
    return out


def find_unused_security_groups(graph: nx.DiGraph) -> list[dict]:
    """Security groups with no 'guarded_by' edge pointing at them."""
    out: list[dict] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "security_group":
            continue
        attachments = [
            (u, _v, e) for u, _v, e in graph.in_edges(node, data=True)
            if e.get("relation") == "guarded_by"
        ]
        if not attachments:
            out.append({"id": node, "label": attrs.get("label"), "kind": "security_group"})
    return out


def mark_orphaned_edges(graph: nx.DiGraph) -> nx.DiGraph:
    """Annotate orphan nodes/edges in-place and return the graph.

    Adds:
        node['orphan']  = True for orphan nodes
        edge['style']   = 'dashed' on edges touching orphans
        edge['color']   = Colors.ORPHAN

    Mutates and returns the graph for chaining.
    """
    orphan_ids: set[str] = set()
    for orphan in find_unattached_target_groups(graph):
        orphan_ids.add(orphan["id"])
    for orphan in find_unattached_instances(graph):
        orphan_ids.add(orphan["id"])
    for orphan in find_unused_security_groups(graph):
        orphan_ids.add(orphan["id"])

    for node_id in orphan_ids:
        if node_id in graph:
            graph.nodes[node_id]["orphan"] = True
            graph.nodes[node_id]["health"] = HealthStatus.UNHEALTHY.value

    # Mark every incident edge as dashed/red so visualizers render correctly.
    for u, v, data in graph.edges(data=True):
        if u in orphan_ids or v in orphan_ids:
            data["style"] = "dashed"
            data["color"] = Colors.ORPHAN
            data["orphan"] = True
        else:
            data.setdefault("style", "solid")
            data.setdefault("color", Colors.HEALTHY)
            data.setdefault("orphan", False)

    return graph


# ---------------------------------------------------------------------------
# Fix-suggestion engine
# ---------------------------------------------------------------------------


def diagnose(graph: nx.DiGraph) -> Diagnosis:
    """Run every rule and return a Diagnosis with prioritized fixes."""
    orphan_tgs = find_unattached_target_groups(graph)
    orphan_inst = find_unattached_instances(graph)
    orphan_sgs = find_unused_security_groups(graph)

    diag = Diagnosis(
        orphan_target_groups=orphan_tgs,
        orphan_instances=orphan_inst,
        orphan_security_groups=orphan_sgs,
    )

    diag.fixes.extend(_diagnose_target_health(graph, diag))
    diag.fixes.extend(_diagnose_security_groups(graph))
    diag.fixes.extend(_diagnose_orphans(orphan_tgs, orphan_inst, orphan_sgs))
    diag.fixes.extend(_diagnose_ecs(graph, diag))
    diag.fixes.extend(_diagnose_eks(graph, diag))
    diag.fixes.extend(_diagnose_rds(graph, diag))
    diag.fixes.extend(_diagnose_lambda(graph))
    diag.fixes.extend(_diagnose_s3(graph, diag))

    # Stable sort: critical > warning > info, then alphabetical.
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    diag.fixes.sort(key=lambda f: (severity_rank.get(f.severity, 3), f.title))
    return diag


def _diagnose_ecs(graph: nx.DiGraph, diag: Diagnosis) -> list[Fix]:
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "ecs_service":
            continue
        running = attrs.get("running_count", 0)
        desired = attrs.get("desired_count", 0)
        label = attrs.get("label", node)
        if desired == 0:
            continue
        if running < desired:
            severity = "critical" if running == 0 else "warning"
            diag.unhealthy_ecs_services.append({"id": node, "label": label, "running": running, "desired": desired})
            fixes.append(
                Fix(
                    severity=severity,
                    resource_id=node,
                    title=f"ECS service '{label}': {running}/{desired} tasks running",
                    detail=(
                        f"{desired - running} task(s) not running. Check CloudWatch logs for the service, "
                        "verify the task definition is valid, and confirm the cluster has capacity "
                        "(CPU/memory). Also check security group rules if tasks fail health checks."
                    ),
                )
            )
    return fixes


def _diagnose_eks(graph: nx.DiGraph, diag: Diagnosis) -> list[Fix]:
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "eks_cluster":
            continue
        label = attrs.get("label", node)
        health = attrs.get("health")
        if health != "healthy":
            diag.unhealthy_eks_clusters.append({"id": node, "label": label})
            fixes.append(
                Fix(
                    severity="critical",
                    resource_id=node,
                    title=f"EKS cluster '{label}' is not ACTIVE",
                    detail=(
                        "Cluster status is not ACTIVE. Check the EKS console for creation/update errors, "
                        "verify IAM roles have the required permissions, and check subnet/VPC configuration."
                    ),
                )
            )
        # Check node groups attached to this cluster.
        for _c, ng_node, edge in graph.out_edges(node, data=True):
            if edge.get("relation") != "contains":
                continue
            ng_attrs = graph.nodes.get(ng_node, {})
            if ng_attrs.get("kind") != "eks_nodegroup":
                continue
            if ng_attrs.get("health") != "healthy":
                fixes.append(
                    Fix(
                        severity="warning",
                        resource_id=ng_node,
                        title=f"EKS node group '{ng_attrs.get('label', ng_node)}' is not ACTIVE",
                        detail=(
                            "Node group is not in ACTIVE state. Check IAM node role, launch template, "
                            "and whether the desired instance type is available in the selected AZs."
                        ),
                    )
                )
    return fixes


def _diagnose_rds(graph: nx.DiGraph, diag: Diagnosis) -> list[Fix]:
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "rds":
            continue
        label = attrs.get("label", node)
        status = attrs.get("status", "unknown")
        meta = attrs.get("metadata", {})
        if status != "available":
            diag.unhealthy_rds_instances.append({"id": node, "label": label, "status": status})
            fixes.append(
                Fix(
                    severity="critical" if status in ("failed", "incompatible-network") else "warning",
                    resource_id=node,
                    title=f"RDS '{label}' status: {status}",
                    detail=(
                        f"Instance is not 'available' (current: {status}). "
                        "Check RDS events in the console and verify parameter group and subnet group are valid."
                    ),
                )
            )
        if not meta.get("storage_encrypted"):
            fixes.append(
                Fix(
                    severity="warning",
                    resource_id=node,
                    title=f"RDS '{label}': storage not encrypted",
                    detail="Enable storage encryption at rest. Requires a snapshot restore to enable on existing instances.",
                )
            )
        if meta.get("publicly_accessible"):
            fixes.append(
                Fix(
                    severity="warning",
                    resource_id=node,
                    title=f"RDS '{label}': publicly accessible",
                    detail=(
                        "Instance is publicly accessible. Unless intentional, disable this and use "
                        "a bastion host or VPN for access."
                    ),
                )
            )
    return fixes


def _diagnose_lambda(graph: nx.DiGraph) -> list[Fix]:
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "lambda":
            continue
        label = attrs.get("label", node)
        health = attrs.get("health")
        if health != "healthy":
            fixes.append(
                Fix(
                    severity="warning",
                    resource_id=node,
                    title=f"Lambda '{label}' is not Active",
                    detail=(
                        "Function state is not Active. Check for failed deployments, "
                        "missing layers, or broken VPC config that prevents ENI creation."
                    ),
                )
            )
    return fixes


def _diagnose_s3(graph: nx.DiGraph, diag: Diagnosis) -> list[Fix]:
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "s3":
            continue
        label = attrs.get("label", node)
        meta = attrs.get("metadata", {})
        if not attrs.get("public_access_blocked", True):
            diag.insecure_s3_buckets.append({"id": node, "label": label})
            fixes.append(
                Fix(
                    severity="critical",
                    resource_id=node,
                    title=f"S3 bucket '{label}': public access not fully blocked",
                    detail=(
                        "Enable S3 Block Public Access on this bucket. "
                        "Publicly readable buckets can leak sensitive data."
                    ),
                )
            )
        if not meta.get("encryption_enabled"):
            fixes.append(
                Fix(
                    severity="warning",
                    resource_id=node,
                    title=f"S3 bucket '{label}': server-side encryption not enabled",
                    detail="Enable SSE-S3 or SSE-KMS default encryption on this bucket.",
                )
            )
    return fixes


def _diagnose_target_health(graph: nx.DiGraph, diag: Diagnosis) -> list[Fix]:
    """Produce one fix per partially or fully unhealthy target group."""
    fixes: list[Fix] = []
    for node, attrs in graph.nodes(data=True):
        if attrs.get("kind") != "target_group":
            continue
        healthy = attrs.get("healthy_count", 0)
        total = attrs.get("total_count", 0)
        if total == 0:
            continue  # surfaced as orphan instead
        if healthy < total:
            unhealthy = total - healthy
            severity = "critical" if healthy == 0 else "warning"
            diag.unhealthy_target_groups.append(
                {"id": node, "label": attrs.get("label"), "healthy": healthy, "total": total}
            )
            fixes.append(
                Fix(
                    severity=severity,
                    resource_id=node,
                    title=f"Target group '{attrs.get('label')}': {healthy}/{total} healthy",
                    detail=(
                        f"{unhealthy} target(s) failing health checks. "
                        "Check (1) the health check path returns 200, (2) the security "
                        "group on the target allows the configured port from the ALB SG, "
                        "(3) the target's process is actually listening on that port."
                    ),
                )
            )
    return fixes


def _diagnose_security_groups(graph: nx.DiGraph) -> list[Fix]:
    """Look for ALB→target SG mismatches that prevent ALB→target traffic.

    Heuristic: for every (alb, tg) pair where the TG has registered instances,
    verify the instance SGs allow ingress on the TG port from at least one of
    the ALB's SGs. If not, emit a high-confidence fix.
    """
    fixes: list[Fix] = []

    # Build helper indices.
    alb_sgs: dict[str, set[str]] = {}
    for n, a in graph.nodes(data=True):
        if a.get("kind") == "alb":
            alb_sgs[n] = set(a.get("metadata", {}).get("security_group_ids", []) or [])

    sg_metadata: dict[str, dict] = {
        n: a.get("metadata", {}) for n, a in graph.nodes(data=True) if a.get("kind") == "security_group"
    }

    # For each ALB → TG forwards_to edge, walk to target instances.
    for alb_node, tg_node, edge in graph.edges(data=True):
        if edge.get("relation") != "forwards_to":
            continue
        tg_attrs = graph.nodes[tg_node]
        port = tg_attrs.get("port")
        if not port:
            continue
        for inst_node, _t, e2 in graph.in_edges(tg_node, data=True):
            if e2.get("relation") != "registered_in":
                continue
            # Find the instance's SGs.
            inst_sg_ids = [
                v for _u, v, e3 in graph.out_edges(inst_node, data=True)
                if e3.get("relation") == "guarded_by" and graph.nodes[v].get("kind") == "security_group"
            ]
            if not _allows_ingress_from(inst_sg_ids, alb_sgs.get(alb_node, set()), port, sg_metadata):
                fixes.append(
                    Fix(
                        severity="critical",
                        resource_id=inst_node,
                        title=f"Security group missing port {port} from ALB SG",
                        detail=(
                            f"Instance {inst_node} security groups do not allow port {port} "
                            f"ingress from any ALB security group. Add a rule allowing "
                            f"tcp/{port} from sg of ALB {alb_node.split('/')[-1]}."
                        ),
                    )
                )
    return fixes


def _allows_ingress_from(
    target_sg_ids: list[str],
    source_sg_ids: set[str],
    port: int,
    sg_metadata: dict[str, dict],
) -> bool:
    """Return True if any target SG allows tcp/`port` ingress from `source_sg_ids`."""
    if not source_sg_ids or not target_sg_ids:
        return False
    for sg_id in target_sg_ids:
        meta = sg_metadata.get(sg_id, {})
        for rule in meta.get("rules", []):
            if rule.get("direction") != "ingress":
                continue
            proto = rule.get("protocol", "-1")
            if proto not in ("tcp", "-1"):
                continue
            from_p = rule.get("from_port") or 0
            to_p = rule.get("to_port") or 65535
            if not (from_p <= port <= to_p):
                continue
            referenced = set(rule.get("referenced_sg_ids", []) or [])
            if referenced & source_sg_ids:
                return True
            # 0.0.0.0/0 accepts everything (security note: still a misconfig but it does allow).
            if "0.0.0.0/0" in (rule.get("cidr_ranges") or []):
                return True
    return False


def _diagnose_orphans(
    orphan_tgs: list[dict],
    orphan_inst: list[dict],
    orphan_sgs: list[dict],
) -> list[Fix]:
    fixes: list[Fix] = []
    for o in orphan_tgs:
        fixes.append(
            Fix(
                severity="warning",
                resource_id=o["id"],
                title=f"Orphan target group: {o.get('label')}",
                detail=(
                    "No load balancer listener forwards to this target group. "
                    "Either delete it, or add a listener rule that routes traffic to it."
                ),
            )
        )
    for o in orphan_inst:
        fixes.append(
            Fix(
                severity="info",
                resource_id=o["id"],
                title=f"Instance not in any target group: {o.get('label')}",
                detail=(
                    "This instance receives no traffic from any ALB/NLB. "
                    "Register it with a target group, or remove it if unused."
                ),
            )
        )
    for o in orphan_sgs:
        fixes.append(
            Fix(
                severity="info",
                resource_id=o["id"],
                title=f"Unused security group: {o.get('label')}",
                detail="This security group is attached to nothing. Safe to delete.",
            )
        )
    return fixes
