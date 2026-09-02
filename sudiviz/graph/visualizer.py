"""Render the topology graph in three modes: terminal, web (Cytoscape), and PNG.

All three modes share a single rule for orphans: edges touching orphan nodes
are rendered as RED DASHED lines. The rule is applied here uniformly so the
analyzer module stays output-agnostic.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import networkx as nx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from sudiviz.discovery.costs import format_cost
from sudiviz.discovery.models import HealthStatus
from sudiviz.utils.auth import cloudwatch_logs_url, cloudwatch_metrics_url, console_url, pricing_url
from sudiviz.utils.branding import Colors

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cytoscape.js export
# ---------------------------------------------------------------------------


def export_cytoscape_json(
    graph: nx.DiGraph,
    region: str | None = None,
) -> dict[str, list[dict]]:
    """Convert the NetworkX graph to Cytoscape.js elements format.

    Output schema:
        {
          "nodes": [{"data": {...}, "classes": "..."}],
          "edges": [{"data": {...}, "classes": "...", "style": {...}}]
        }

    Cytoscape's stylesheet consumes `classes` (CSS-like selectors) — the front
    end maps `orphan healthy unhealthy ...` classes to colors. Per-element
    `style` overrides handle the edge-specific dashed/red rendering.
    """
    region = region or graph.graph.get("region", "us-east-1")
    provider = graph.graph.get("provider", "aws")
    project_id = graph.graph.get("project_id")
    nodes: list[dict] = []
    edges: list[dict] = []

    for node_id, attrs in graph.nodes(data=True):
        kind = attrs.get("kind", "unknown")
        health = attrs.get("health", HealthStatus.UNKNOWN.value)
        is_orphan = bool(attrs.get("orphan"))
        classes = " ".join(filter(None, [kind, health, "orphan" if is_orphan else ""]))

        # Get cost from metadata if available
        monthly_cost = attrs.get("monthly_cost", 0)
        cost_display = format_cost(monthly_cost) if monthly_cost else None

        # Build console/pricing URLs — dispatch by provider.
        node_console_url = console_url(
            kind, attrs.get("id", node_id), region,
            provider=provider, project=project_id,
        )
        node_pricing_url = pricing_url(kind, attrs.get("metadata", {}), provider=provider)

        # CloudWatch URLs only apply to AWS.
        node_metrics_url = (
            cloudwatch_metrics_url(kind, attrs.get("id", node_id), region)
            if provider == "aws" else None
        )
        node_logs_url = (
            cloudwatch_logs_url(kind, attrs.get("id", node_id), region)
            if provider == "aws" else None
        )

        nodes.append(
            {
                "data": {
                    "id": node_id,
                    "label": attrs.get("label", node_id),
                    "kind": kind,
                    "health": health,
                    "orphan": is_orphan,
                    "monthly_cost": monthly_cost,
                    "cost_display": cost_display,
                    "console_url": node_console_url,
                    "pricing_url": node_pricing_url,
                    "metrics_url": node_metrics_url,
                    "logs_url": node_logs_url,
                    "metadata": attrs.get("metadata", {}),
                },
                "classes": classes,
            }
        )

    for u, v, data in graph.edges(data=True):
        is_orphan = bool(data.get("orphan"))
        relation = data.get("relation", "")

        # Determine edge class based on relation type
        classes_list = []
        if is_orphan:
            classes_list.append("orphan")
        if relation in ("allows_ingress", "allows_egress"):
            classes_list.append("sg-flow")
            classes_list.append(f"sg-{data.get('direction', 'ingress')}")
        classes = " ".join(classes_list)

        # Color coding for security group flows
        if relation == "allows_ingress":
            line_color = "#3b82f6"  # Blue for ingress
        elif relation == "allows_egress":
            line_color = "#8b5cf6"  # Purple for egress
        elif is_orphan:
            line_color = Colors.ORPHAN
        else:
            line_color = data.get("color", Colors.HEALTHY)

        style = {
            "line-style": data.get("style", "solid"),
            "line-color": line_color,
            "target-arrow-color": line_color,
        }

        # Build edge data with security group rule details
        edge_data = {
            "id": f"{u}__{v}",
            "source": u,
            "target": v,
            "relation": relation,
            "orphan": is_orphan,
        }

        # Add SG rule details if present
        if relation in ("allows_ingress", "allows_egress"):
            edge_data["protocol"] = data.get("protocol", "-1")
            edge_data["from_port"] = data.get("from_port")
            edge_data["to_port"] = data.get("to_port")
            edge_data["direction"] = data.get("direction")

        edges.append(
            {
                "data": edge_data,
                "classes": classes,
                "style": style,
            }
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "provider": provider,
            "account_id": graph.graph.get("account_id"),
            "project_id": graph.graph.get("project_id"),
            "region": region,
            "vpc_id": graph.graph.get("vpc_id"),
            "discovered_at": graph.graph.get("discovered_at"),
        },
    }


# ---------------------------------------------------------------------------
# Terminal rendering (Rich)
# ---------------------------------------------------------------------------


_HEALTH_STYLE = {
    HealthStatus.HEALTHY.value: Colors.RICH_HEALTHY,
    HealthStatus.UNHEALTHY.value: Colors.RICH_UNHEALTHY,
    HealthStatus.INITIAL.value: Colors.RICH_WARNING,
    HealthStatus.DRAINING.value: Colors.RICH_WARNING,
    HealthStatus.UNUSED.value: Colors.RICH_UNREACHABLE,
    HealthStatus.UNKNOWN.value: Colors.RICH_UNREACHABLE,
}


def render_terminal(graph: nx.DiGraph, console: Console | None = None) -> None:
    """Render the graph as a Rich tree, with red dashed branches for orphans."""
    console = console or Console()

    tree = Tree("[bold cyan]Topology[/bold cyan]")

    # ALBs → listeners → target groups → instances/lambdas.
    albs = [n for n, a in graph.nodes(data=True) if a.get("kind") == "alb"]
    for alb in albs:
        attrs = graph.nodes[alb]
        branch = tree.add(_node_line(alb, attrs))
        for _u, tg, edge in graph.out_edges(alb, data=True):
            if edge.get("relation") != "forwards_to":
                continue
            tg_attrs = graph.nodes[tg]
            tg_branch = branch.add(_edge_line(edge, _node_line(tg, tg_attrs)))
            for inst, _v, e2 in graph.in_edges(tg, data=True):
                if e2.get("relation") != "registered_in":
                    continue
                tg_branch.add(_edge_line(e2, _node_line(inst, graph.nodes[inst])))

    # ECS clusters → services.
    ecs_clusters = [n for n, a in graph.nodes(data=True) if a.get("kind") == "ecs_cluster"]
    if ecs_clusters:
        ecs_branch = tree.add("[bold magenta]ECS[/bold magenta]")
        for cluster in ecs_clusters:
            cb = ecs_branch.add(_node_line(cluster, graph.nodes[cluster]))
            for _c, svc, edge in graph.out_edges(cluster, data=True):
                if edge.get("relation") != "contains":
                    continue
                cb.add(_edge_line(edge, _node_line(svc, graph.nodes[svc])))

    # EKS clusters → node groups.
    eks_clusters = [n for n, a in graph.nodes(data=True) if a.get("kind") == "eks_cluster"]
    if eks_clusters:
        eks_branch = tree.add("[bold blue]EKS[/bold blue]")
        for cluster in eks_clusters:
            cb = eks_branch.add(_node_line(cluster, graph.nodes[cluster]))
            for _c, ng, edge in graph.out_edges(cluster, data=True):
                if edge.get("relation") != "contains":
                    continue
                cb.add(_edge_line(edge, _node_line(ng, graph.nodes[ng])))

    # RDS instances.
    rds_nodes = [(n, a) for n, a in graph.nodes(data=True) if a.get("kind") == "rds"]
    if rds_nodes:
        rds_branch = tree.add("[bold yellow]RDS[/bold yellow]")
        for node, attrs in rds_nodes:
            rds_branch.add(_node_line(node, attrs))

    # Lambda functions.
    lambda_nodes = [(n, a) for n, a in graph.nodes(data=True) if a.get("kind") == "lambda"]
    if lambda_nodes:
        lmb_branch = tree.add("[bold green]Lambda[/bold green]")
        for node, attrs in lambda_nodes:
            lmb_branch.add(_node_line(node, attrs))

    # S3 buckets.
    s3_nodes = [(n, a) for n, a in graph.nodes(data=True) if a.get("kind") == "s3"]
    if s3_nodes:
        s3_branch = tree.add("[bold cyan]S3[/bold cyan]")
        for node, attrs in s3_nodes:
            s3_branch.add(_node_line(node, attrs))

    # Orphans: separate section so they're impossible to miss.
    orphan_nodes = [(n, a) for n, a in graph.nodes(data=True) if a.get("orphan")]
    if orphan_nodes:
        orphan_branch = tree.add(f"[{Colors.RICH_ORPHAN}]ORPHANS[/]")
        for node, attrs in orphan_nodes:
            orphan_branch.add(f"[{Colors.RICH_ORPHAN}]╌╌ {attrs.get('kind')}: {attrs.get('label')} ({node})[/]")

    session_region = graph.graph.get("region", "unknown")
    console.print(Panel(tree, title=f"sudiviz topology — region: {session_region}", expand=False))


def _node_line(node_id: str, attrs: dict) -> str:
    health = attrs.get("health", HealthStatus.UNKNOWN.value)
    style = _HEALTH_STYLE.get(health, "white")
    label = attrs.get("label", node_id)
    kind = attrs.get("kind", "")
    is_orphan = attrs.get("orphan")
    if is_orphan:
        style = Colors.RICH_ORPHAN
    suffix = ""
    if attrs.get("kind") == "target_group":
        suffix = f" [{attrs.get('healthy_count', 0)}/{attrs.get('total_count', 0)}]"
    region = attrs.get("region")
    region_suffix = f" [dim]({region})[/dim]" if region else ""
    return f"[{style}]{kind}: {label}{suffix}[/]{region_suffix}"


def _edge_line(edge: dict, child_repr: str) -> str:
    """Inline-render an edge marker. Dashed/red for orphan edges."""
    if edge.get("orphan") or edge.get("style") == "dashed":
        return f"[{Colors.RICH_ORPHAN}]╌╌▶[/] {child_repr}"
    return f"[dim]──▶[/] {child_repr}"


# ---------------------------------------------------------------------------
# Diagnosis rendering (used by CLI + TUI)
# ---------------------------------------------------------------------------


def render_diagnosis(diagnosis: Any, console: Console | None = None) -> None:
    """Print a Rich table of fix suggestions, sorted by severity."""
    console = console or Console()
    if not diagnosis.fixes:
        console.print(Panel("[green]No issues detected.[/green]", title="sudiviz diagnose"))
        return
    table = Table(title="Diagnosis", show_lines=False)
    table.add_column("Severity", style="bold")
    table.add_column("Title")
    table.add_column("Detail", overflow="fold")
    for fix in diagnosis.fixes:
        sev_color = {"critical": "red", "warning": "yellow", "info": "cyan"}.get(fix.severity, "white")
        table.add_row(f"[{sev_color}]{fix.severity}[/]", fix.title, fix.detail)
    console.print(table)


# ---------------------------------------------------------------------------
# PNG export
# ---------------------------------------------------------------------------


def export_png(graph: nx.DiGraph, filename: str) -> Path:
    """Export the graph as a PNG.

    Strategy: prefer the `diagrams` library for nice cloud iconography. If
    it's missing or graphviz isn't installed, fall back to graphviz directly.
    Either way, orphan edges are dashed red.
    """
    try:
        return _export_png_via_graphviz(graph, filename)
    except Exception as exc:
        logger.warning("Graphviz export failed: %s", exc)
        raise


def _export_png_via_graphviz(graph: nx.DiGraph, filename: str) -> Path:
    """Use graphviz directly — works without the diagrams library."""
    try:
        import graphviz  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "graphviz package not installed. `pip install graphviz` and ensure the "
            "graphviz binary is on PATH."
        ) from exc

    dot = graphviz.Digraph(comment="sudiviz topology", format="png")
    dot.attr("graph", rankdir="LR", bgcolor="white", fontname="Helvetica")
    dot.attr("node", fontname="Helvetica", style="filled", color="#1f2937", fontcolor="#1f2937")

    for node_id, attrs in graph.nodes(data=True):
        kind = attrs.get("kind", "unknown")
        health = attrs.get("health", HealthStatus.UNKNOWN.value)
        is_orphan = bool(attrs.get("orphan"))
        fillcolor = _node_fill(health, is_orphan)
        shape = {
            "alb": "box3d",
            "target_group": "component",
            "instance": "box",
            "security_group": "diamond",
            "vpc": "rectangle",
            "ecs_cluster": "tab",
            "ecs_service": "note",
            "eks_cluster": "hexagon",
            "eks_nodegroup": "parallelogram",
            "rds": "cylinder",
            "lambda": "invtriangle",
            "s3": "folder",
        }.get(kind, "ellipse")
        label = f"{attrs.get('label', node_id)}\\n[{kind}]"
        if kind == "target_group":
            label += f"\\n{attrs.get('healthy_count', 0)}/{attrs.get('total_count', 0)} healthy"
        elif kind == "ecs_service":
            label += f"\\n{attrs.get('running_count', 0)}/{attrs.get('desired_count', 0)} running"
        elif kind == "eks_nodegroup":
            label += f"\\n{attrs.get('desired_size', 0)} nodes"
        elif kind == "rds":
            label += f"\\n{attrs.get('engine', '')} [{attrs.get('status', '')}]"
        elif kind == "lambda":
            label += f"\\n{attrs.get('runtime', '')}"
        dot.node(_safe_id(node_id), label=label, shape=shape, fillcolor=fillcolor)

    for u, v, data in graph.edges(data=True):
        is_orphan = bool(data.get("orphan"))
        attrs: dict[str, Any] = {
            "label": data.get("relation", ""),
            "fontsize": "10",
            "fontname": "Helvetica",
        }
        if is_orphan:
            attrs.update({"color": Colors.ORPHAN, "style": "dashed", "penwidth": "2"})
        else:
            attrs.update({"color": "#374151", "style": "solid"})
        dot.edge(_safe_id(u), _safe_id(v), **attrs)

    out_path = Path(filename)
    if out_path.suffix.lower() == ".png":
        out_path = out_path.with_suffix("")
    rendered = dot.render(filename=str(out_path), cleanup=True)
    return Path(rendered)


def _node_fill(health: str, is_orphan: bool) -> str:
    if is_orphan:
        return "#fee2e2"  # red-100
    return {
        HealthStatus.HEALTHY.value: "#dcfce7",     # green-100
        HealthStatus.UNHEALTHY.value: "#fecaca",   # red-200
        HealthStatus.INITIAL.value: "#fef9c3",     # yellow-100
        HealthStatus.DRAINING.value: "#fed7aa",    # orange-200
        HealthStatus.UNUSED.value: "#e5e7eb",      # gray-200
        HealthStatus.UNKNOWN.value: "#e5e7eb",
    }.get(health, "#ffffff")


def _safe_id(node_id: str) -> str:
    """Graphviz node IDs can't contain ARN colons/slashes without quoting."""
    return '"' + node_id.replace('"', "'") + '"'


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def serialize_graph(graph: nx.DiGraph) -> str:
    """Serialize the graph as JSON (for `--json` output)."""
    payload = {
        "meta": dict(graph.graph),
        "nodes": [{"id": n, **a} for n, a in graph.nodes(data=True)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in graph.edges(data=True)],
    }
    return json.dumps(payload, indent=2, default=str)
