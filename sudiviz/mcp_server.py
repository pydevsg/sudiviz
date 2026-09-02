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
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
)

from sudiviz.discovery.aws import discover_all
from sudiviz.discovery.costs import calculate_total_costs
from sudiviz.discovery.models import CloudProvider
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
            "Discover live cloud infrastructure resources (AWS or GCP). Returns load balancers, "
            "target groups, instances, security groups/firewall rules, container clusters, "
            "databases, serverless functions, and storage buckets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region (e.g. us-east-1 for AWS, us-central1 for GCP).",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC (AWS) or VPC network (GCP).",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag/label filter, e.g. 'Service=checkout' or 'k=v,k2=v2'.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name from ~/.aws/credentials (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only). Resolved from env/ADC if omitted.",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_diagnose",
        description=(
            "Discover cloud infrastructure (AWS or GCP) and analyze for issues: orphan resources, "
            "unhealthy targets, security group misconfigurations, insecure storage, "
            "and more. Returns a structured diagnosis with prioritized fixes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag filter, e.g. 'Service=checkout'.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_graph",
        description=(
            "Generate the infrastructure topology graph (AWS or GCP). Returns Cytoscape-compatible "
            "JSON with nodes (resources) and edges (relationships) for visualization."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "service_tag": {
                    "type": "string",
                    "description": "Tag filter.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_fix",
        description=(
            "Generate remediation commands for diagnosed infrastructure issues. "
            "Returns CLI commands that would fix each issue. "
            "Set dry_run=false to apply fixes (requires write permissions)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
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
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
                },
            },
            "required": ["tfstate_path"],
        },
    ),
    Tool(
        name="sudiviz_costs",
        description=(
            "Estimate monthly costs for discovered cloud resources (AWS or GCP). "
            "Returns cost breakdown by service and by individual resource."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
                },
            },
        },
    ),
    Tool(
        name="sudiviz_list_resources",
        description=(
            "List discovered resources of a specific type (AWS or GCP). "
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
                "provider": {
                    "type": "string",
                    "description": "Cloud provider: 'aws' (default) or 'gcp'.",
                    "enum": ["aws", "gcp"],
                    "default": "aws",
                },
                "region": {
                    "type": "string",
                    "description": "Cloud region. Uses default if omitted.",
                },
                "vpc_id": {
                    "type": "string",
                    "description": "Filter to a specific VPC/network.",
                },
                "profile": {
                    "type": "string",
                    "description": "AWS profile name (AWS only).",
                },
                "project": {
                    "type": "string",
                    "description": "GCP project ID (GCP only).",
                },
            },
            "required": ["kind"],
        },
    ),
]


async def _run_discovery(arguments: dict[str, Any]):
    provider = arguments.get("provider", "aws")
    if provider == "gcp":
        try:
            from sudiviz.discovery.gcp import discover_all_gcp
        except ImportError as exc:
            raise RuntimeError(
                "GCP SDK not installed. Install with: pip install sudiviz[gcp]"
            ) from exc
        return await discover_all_gcp(
            project=arguments.get("project"),
            region=arguments.get("region"),
            vpc_network=arguments.get("vpc_id"),
            service_tag=arguments.get("service_tag"),
        )
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


RESOURCE_TEMPLATES = [
    ResourceTemplate(
        name="aws-topology",
        uriTemplate="infra://aws/{region}/topology",
        description="Live AWS infrastructure topology for a region as JSON.",
        mimeType="application/json",
    ),
    ResourceTemplate(
        name="aws-health",
        uriTemplate="infra://aws/{region}/health",
        description="Health status summary for AWS resources in a region.",
        mimeType="application/json",
    ),
    ResourceTemplate(
        name="aws-costs",
        uriTemplate="infra://aws/{region}/costs",
        description="Estimated monthly cost breakdown for a region.",
        mimeType="application/json",
    ),
]

PROMPTS = [
    Prompt(
        name="diagnose-infrastructure",
        description="Analyze AWS infrastructure in a region for issues and recommend fixes.",
        arguments=[
            PromptArgument(name="region", description="AWS region (e.g. us-east-1)", required=False),
            PromptArgument(name="profile", description="AWS profile name", required=False),
        ],
    ),
    Prompt(
        name="cost-optimization",
        description="Find cost-saving opportunities in AWS infrastructure.",
        arguments=[
            PromptArgument(name="region", description="AWS region (e.g. us-east-1)", required=False),
            PromptArgument(name="profile", description="AWS profile name", required=False),
        ],
    ),
    Prompt(
        name="security-audit",
        description="Check for security misconfigurations: open security groups, public databases, unencrypted storage.",
        arguments=[
            PromptArgument(name="region", description="AWS region (e.g. us-east-1)", required=False),
            PromptArgument(name="profile", description="AWS profile name", required=False),
        ],
    ),
    Prompt(
        name="incident-triage",
        description="Trace unhealthy resources through the dependency chain to identify root cause.",
        arguments=[
            PromptArgument(name="region", description="AWS region (e.g. us-east-1)", required=False),
            PromptArgument(name="profile", description="AWS profile name", required=False),
        ],
    ),
]


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    return RESOURCE_TEMPLATES


@app.read_resource()
async def read_resource(uri: str) -> list[TextResourceContents]:
    from urllib.parse import urlparse

    parsed = urlparse(str(uri))
    if parsed.scheme != "infra" or parsed.hostname != "aws":
        return [TextResourceContents(uri=str(uri), mimeType="application/json", text=json.dumps({"error": f"Unknown resource URI: {uri}"}))]

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        return [TextResourceContents(uri=str(uri), mimeType="application/json", text=json.dumps({"error": f"Invalid resource path: {parsed.path}"}))]

    region, kind = parts
    result = await discover_all(region=region)

    if kind == "topology":
        graph = mark_orphaned_edges(build_graph(result))
        data = export_cytoscape_json(graph, region=region)
    elif kind == "health":
        graph = mark_orphaned_edges(build_graph(result))
        diag = run_diagnosis(graph)
        data = {
            "region": region,
            "account_id": result.account_id,
            "resource_counts": {
                "load_balancers": len(result.load_balancers),
                "target_groups": len(result.target_groups),
                "instances": len(result.instances),
                "security_groups": len(result.security_groups),
            },
            "fix_count": len(diag.fixes),
            "critical_count": sum(1 for f in diag.fixes if f.severity == "critical"),
            "warning_count": sum(1 for f in diag.fixes if f.severity == "warning"),
            "fixes": [{"title": f.title, "severity": f.severity} for f in diag.fixes],
        }
    elif kind == "costs":
        data = calculate_total_costs(result)
    else:
        data = {"error": f"Unknown resource kind: {kind}"}

    return [TextResourceContents(uri=str(uri), mimeType="application/json", text=json.dumps(data, indent=2, default=str))]


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return PROMPTS


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
    args = arguments or {}
    region = args.get("region", "your default region")
    profile = args.get("profile", "")
    profile_note = f" using AWS profile '{profile}'" if profile else ""

    if name == "diagnose-infrastructure":
        return GetPromptResult(
            description=f"Diagnose AWS infrastructure in {region}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Analyze my AWS infrastructure in {region}{profile_note} for issues.\n\n"
                            "1. First, use sudiviz_diagnose to discover resources and run diagnosis.\n"
                            "2. Summarize the resource counts and any issues found.\n"
                            "3. For each issue, explain the risk and recommend a fix.\n"
                            "4. Prioritize critical issues first, then warnings.\n"
                            "5. If there are fixable issues, show the AWS CLI commands using sudiviz_fix (dry_run=true)."
                        ),
                    ),
                )
            ],
        )

    elif name == "cost-optimization":
        return GetPromptResult(
            description=f"Find cost-saving opportunities in {region}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Analyze my AWS infrastructure costs in {region}{profile_note}.\n\n"
                            "1. Use sudiviz_costs to get the cost breakdown by service and resource.\n"
                            "2. Identify the most expensive resources.\n"
                            "3. Use sudiviz_diagnose to check for orphan resources that are wasting money.\n"
                            "4. Suggest specific cost-saving actions (right-sizing, deleting orphans, reserved instances).\n"
                            "5. Estimate potential monthly savings for each suggestion."
                        ),
                    ),
                )
            ],
        )

    elif name == "security-audit":
        return GetPromptResult(
            description=f"Security audit for {region}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Perform a security audit of my AWS infrastructure in {region}{profile_note}.\n\n"
                            "1. Use sudiviz_diagnose to discover resources and find security issues.\n"
                            "2. Check for: open security groups (0.0.0.0/0), publicly accessible RDS, "
                            "unencrypted S3 buckets, missing encryption at rest.\n"
                            "3. Rate each finding by severity (critical / warning / info).\n"
                            "4. For each finding, explain the risk and the remediation command.\n"
                            "5. Use sudiviz_list_resources with kind='security_group' to review all SG rules."
                        ),
                    ),
                )
            ],
        )

    elif name == "incident-triage":
        return GetPromptResult(
            description=f"Incident triage for {region}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Something may be wrong with my AWS infrastructure in {region}{profile_note}. Help me triage.\n\n"
                            "1. Use sudiviz_diagnose to find all current issues.\n"
                            "2. Use sudiviz_graph to get the topology and trace dependencies.\n"
                            "3. Focus on critical issues first — unhealthy targets, failing health checks.\n"
                            "4. Trace the dependency chain: ALB → Target Group → Instance → Security Group.\n"
                            "5. Identify the root cause and suggest immediate remediation steps.\n"
                            "6. If a fix is available, show the dry-run command with sudiviz_fix."
                        ),
                    ),
                )
            ],
        )

    return GetPromptResult(
        description=f"Unknown prompt: {name}",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=f"Unknown prompt: {name}. Available prompts: diagnose-infrastructure, cost-optimization, security-audit, incident-triage."),
            )
        ],
    )


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
        provider = arguments.get("provider", "aws")
        if provider == "gcp":
            from sudiviz.discovery.gcp_costs import calculate_gcp_total_costs
            costs = calculate_gcp_total_costs(result)
        else:
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
