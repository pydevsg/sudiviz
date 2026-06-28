"""sudiviz MCP Server — expose infrastructure discovery as MCP tools.

Runs as a standalone MCP server (stdio transport) so any MCP-compatible
client (Claude Desktop, Claude Code, Cursor, etc.) can discover, diagnose,
and remediate AWS infrastructure via natural language.

Start:
    python -m sudiviz.mcp_server
    # or via the installed entry point:
    sudiviz-mcp
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from sudiviz.discovery.aws import discover_all
from sudiviz.discovery.costs import calculate_total_costs
from sudiviz.graph.analyzer import diagnose as run_diagnosis, mark_orphaned_edges
from sudiviz.graph.builder import build_graph
from sudiviz.graph.visualizer import export_cytoscape_json, serialize_graph
from sudiviz.utils.branding import VERSION

logger = logging.getLogger("sudiviz.mcp")

app = Server("sudiviz")


TOOLS = [
    Tool(
        name="sudiviz_discover",
        description=(
            "Discover live AWS infrastructure resources. Returns ALBs, target groups, "
            "EC2 instances, security groups, ECS, EKS, RDS, Aurora, Lambda, and S3 buckets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region (e.g. us-east-1). Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag filter, e.g. 'Service=checkout' or 'k=v,k2=v2'.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name from ~/.aws/credentials.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_diagnose",
        description=(
            "Discover AWS infrastructure and analyze for issues: orphan resources, "
            "unhealthy targets, security group misconfigurations, insecure S3 buckets, "
            "and more. Returns a structured diagnosis with prioritized fixes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region (e.g. us-east-1). Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag filter, e.g. 'Service=checkout'.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_graph",
        description=(
            "Generate the infrastructure topology graph. Returns Cytoscape-compatible "
            "JSON with nodes (resources) and edges (relationships) for visualization."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag filter.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_fix",
        description=(
            "Generate remediation commands for diagnosed infrastructure issues. "
            "Returns AWS CLI commands that would fix each issue. "
            "Set dry_run=false to apply fixes (requires write permissions)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true (default), only show commands without applying.",
                    "default": True,
                },
                "issue_filter": {
                    "type": "string",
                    "description": "Only show fixes matching this substring.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_drift",
        description=(
            "Compare Terraform state against live AWS infrastructure to detect drift. "
            "Requires a path to a terraform show -json output file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tfstate_path": {
                    "type": "string",
                    "description": "Path to the terraform state JSON file.",
                },
                "region": {
                    "type": "string",
                    "description": "AWS region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
            },
            "required": ["tfstate_path"],
        },
    ),
    Tool(
        name="sudiviz_costs",
        description=(
            "Estimate monthly costs for discovered AWS resources. "
            "Returns cost breakdown by service and by individual resource."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "AWS region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_list_resources",
        description=(
            "List discovered resources of a specific type. "
            "Valid kinds: alb, target_group, instance, security_group, "
            "ecs_cluster, eks_cluster, rds, aurora, lambda, s3."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Resource type to list.",
                    "enum": [
                        "alb", "target_group", "instance", "security_group",
                        "ecs_cluster", "eks_cluster", "rds", "aurora", "lambda", "s3",
                    ],
                },
                "region": {
                    "type": "string",
                    "description": "AWS region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name.",
                },
            },
            "required": ["kind"],
        },
    ),
]


async def _run_discovery(arguments: dict[str, Any]):
    return await discover_all(
        region=arguments.get("region"),
        vpc_id=arguments.get("vpc_id"),
        service_tag=arguments.get("service_tag"),
        profile=arguments.get("profile"),
    )


def _resource_list_for_kind(discovery, kind: str) -> list[dict]:
    mapping = {
        "alb": discovery.load_balancers,
        "target_group": discovery.target_groups,
        "instance": discovery.instances,
        "security_group": discovery.security_groups,
        "ecs_cluster": discovery.ecs_clusters,
        "eks_cluster": discovery.eks_clusters,
        "rds": discovery.rds_instances,
        "aurora": discovery.aurora_clusters,
        "lambda": discovery.lambda_functions,
        "s3": discovery.s3_buckets,
    }
    resources = mapping.get(kind, [])
    return [r.model_dump(mode="json") for r in resources]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    logger.info("MCP tool called: %s with args: %s", name, arguments)

    if name == "sudiviz_discover":
        result = await _run_discovery(arguments)
        summary = {
            "provider": result.provider.value,
            "account_id": result.account_id,
            "region": result.region,
            "vpc_id": result.vpc_id,
            "resource_counts": {
                "load_balancers": len(result.load_balancers),
                "target_groups": len(result.target_groups),
                "instances": len(result.instances),
                "security_groups": len(result.security_groups),
                "ecs_clusters": len(result.ecs_clusters),
                "eks_clusters": len(result.eks_clusters),
                "rds_instances": len(result.rds_instances),
                "aurora_clusters": len(result.aurora_clusters),
                "lambda_functions": len(result.lambda_functions),
                "s3_buckets": len(result.s3_buckets),
            },
            "resources": json.loads(result.model_dump_json()),
        }
        return [TextContent(type="text", text=json.dumps(summary, indent=2, default=str))]

    elif name == "sudiviz_diagnose":
        result = await _run_discovery(arguments)
        graph = mark_orphaned_edges(build_graph(result))
        diag = run_diagnosis(graph)
        payload = {
            "provider": result.provider.value,
            "account_id": result.account_id,
            "region": result.region,
            "resource_counts": {
                "load_balancers": len(result.load_balancers),
                "target_groups": len(result.target_groups),
                "instances": len(result.instances),
                "security_groups": len(result.security_groups),
            },
            "diagnosis": diag.to_dict(),
            "fix_count": len(diag.fixes),
            "critical_count": sum(1 for f in diag.fixes if f.severity == "critical"),
            "warning_count": sum(1 for f in diag.fixes if f.severity == "warning"),
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    elif name == "sudiviz_graph":
        result = await _run_discovery(arguments)
        graph = mark_orphaned_edges(build_graph(result))
        cytoscape_data = export_cytoscape_json(graph, region=result.region)
        return [TextContent(type="text", text=json.dumps(cytoscape_data, indent=2, default=str))]

    elif name == "sudiviz_fix":
        from sudiviz.remediation import generate_fixes, apply_fix

        result = await _run_discovery(arguments)
        graph = mark_orphaned_edges(build_graph(result))
        diag = run_diagnosis(graph)

        if not diag.fixes:
            return [TextContent(type="text", text=json.dumps({"message": "No issues found — nothing to fix!", "fixes": []}))]

        actions = generate_fixes(diag, graph, region=result.region)

        issue_filter = arguments.get("issue_filter")
        if issue_filter:
            actions = [a for a in actions if issue_filter.lower() in a.fix.title.lower()]

        dry_run = arguments.get("dry_run", True)

        if not dry_run:
            from sudiviz.utils.auth import get_session
            session = get_session(profile=arguments.get("profile"), region=arguments.get("region"))
            for action in actions:
                if action._service and not action.is_destructive:
                    apply_fix(action, session=session, dry_run=False)

        payload = [
            {
                "title": a.fix.title,
                "severity": a.fix.severity,
                "description": a.description,
                "aws_cli_command": a.aws_cli_command,
                "is_destructive": a.is_destructive,
                "applied": a.applied,
                "error": a.error,
            }
            for a in actions
        ]
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    elif name == "sudiviz_drift":
        from pathlib import Path
        from sudiviz.discovery.terraform import detect_drift, load_state, parse_intended_resources

        tfstate_path = arguments["tfstate_path"]
        path = Path(tfstate_path)
        if not path.exists():
            return [TextContent(type="text", text=json.dumps({"error": f"File not found: {tfstate_path}"}))]

        intended = parse_intended_resources(load_state(path))
        live = await _run_discovery(arguments)
        findings = detect_drift(intended, live)
        payload = {
            "drift_detected": len(findings) > 0,
            "finding_count": len(findings),
            "findings": [f.__dict__ for f in findings],
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    elif name == "sudiviz_costs":
        result = await _run_discovery(arguments)
        costs = calculate_total_costs(result)
        return [TextContent(type="text", text=json.dumps(costs, indent=2, default=str))]

    elif name == "sudiviz_list_resources":
        kind = arguments["kind"]
        result = await _run_discovery(arguments)
        resources = _resource_list_for_kind(result, kind)
        payload = {
            "kind": kind,
            "count": len(resources),
            "region": result.region,
            "resources": resources,
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def main() -> None:
    logger.info("Starting sudiviz MCP server v%s", VERSION)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def cli_main() -> None:
    """Sync entry point for the ``sudiviz-mcp`` console script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
