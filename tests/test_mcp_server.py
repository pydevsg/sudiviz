"""Tests for the MCP server — tool listing, dispatch, and response shapes.

All AWS discovery is mocked; these tests validate that the MCP tool layer
correctly wires through to the existing discovery/graph/analyzer pipeline
and returns well-formed JSON responses.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sudiviz.discovery.models import (
    CloudProvider,
    DiscoveryResult,
    HealthStatus,
    Instance,
    Listener,
    LoadBalancer,
    SecurityGroup,
    SecurityGroupRule,
    Target,
    TargetGroup,
)
from sudiviz.mcp_server import (
    PROMPTS,
    RESOURCE_TEMPLATES,
    TOOLS,
    call_tool,
    get_prompt,
    list_prompts,
    list_resource_templates,
    list_tools,
    read_resource,
)


def _fake_discovery(**overrides) -> DiscoveryResult:
    defaults = dict(
        provider=CloudProvider.AWS,
        account_id="123456789012",
        region="us-east-1",
        vpc_id="vpc-abc",
        load_balancers=[
            LoadBalancer(
                arn="arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/test/abc",
                name="test-alb",
                state="active",
                listeners=[
                    Listener(
                        arn="arn:listener/1",
                        lb_arn="arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/test/abc",
                        protocol="HTTP",
                        port=80,
                        default_target_group_arns=[
                            "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/web/123"
                        ],
                    )
                ],
            )
        ],
        target_groups=[
            TargetGroup(
                arn="arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/web/123",
                name="web-tg",
                protocol="HTTP",
                port=80,
                associated_lb_arns=[
                    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/test/abc"
                ],
                targets=[
                    Target(target_id="i-111", target_type="instance", health=HealthStatus.HEALTHY),
                ],
            )
        ],
        instances=[
            Instance(
                instance_id="i-111",
                instance_type="t3.micro",
                state="running",
                target_group_arns=[
                    "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/web/123"
                ],
                security_group_ids=["sg-web"],
            )
        ],
        security_groups=[
            SecurityGroup(
                sg_id="sg-web",
                name="web-sg",
                attached_to=["eni-1"],
                rules=[
                    SecurityGroupRule(
                        direction="ingress",
                        protocol="tcp",
                        from_port=80,
                        to_port=80,
                        cidr_ranges=["0.0.0.0/0"],
                    )
                ],
            )
        ],
    )
    defaults.update(overrides)
    return DiscoveryResult(**defaults)


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_returns_all_tools():
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "sudiviz_discover" in names
    assert "sudiviz_diagnose" in names
    assert "sudiviz_graph" in names
    assert "sudiviz_fix" in names
    assert "sudiviz_drift" in names
    assert "sudiviz_costs" in names
    assert "sudiviz_list_resources" in names
    assert len(tools) == 7


def test_tools_constant_matches_handler():
    names = {t.name for t in TOOLS}
    assert len(names) == 7
    for t in TOOLS:
        assert t.inputSchema is not None


# ---------------------------------------------------------------------------
# sudiviz_discover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_discover_returns_resource_counts(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_discover", {"region": "us-east-1"})
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert payload["account_id"] == "123456789012"
    assert payload["region"] == "us-east-1"
    assert payload["resource_counts"]["load_balancers"] == 1
    assert payload["resource_counts"]["instances"] == 1
    assert payload["resource_counts"]["target_groups"] == 1


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_discover_passes_filters(mock_discover):
    mock_discover.return_value = _fake_discovery()
    await call_tool("sudiviz_discover", {
        "region": "eu-west-1",
        "vpc_id": "vpc-xyz",
        "service_tag": "Env=prod",
        "profile": "staging",
    })
    mock_discover.assert_called_once_with(
        region="eu-west-1",
        vpc_id="vpc-xyz",
        service_tag="Env=prod",
        profile="staging",
    )


# ---------------------------------------------------------------------------
# sudiviz_diagnose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_diagnose_returns_diagnosis(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_diagnose", {"region": "us-east-1"})
    payload = json.loads(result[0].text)
    assert "diagnosis" in payload
    assert "fix_count" in payload
    assert "critical_count" in payload
    assert "warning_count" in payload
    assert isinstance(payload["diagnosis"]["fixes"], list)


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_diagnose_detects_orphan_tg(mock_discover):
    discovery = _fake_discovery(
        target_groups=[
            TargetGroup(
                arn="arn:tg/orphan",
                name="orphan-tg",
                protocol="HTTP",
                port=80,
                associated_lb_arns=[],
            ),
        ],
        load_balancers=[
            LoadBalancer(arn="arn:lb/1", name="lb1", listeners=[]),
        ],
        instances=[],
        security_groups=[],
    )
    mock_discover.return_value = discovery
    result = await call_tool("sudiviz_diagnose", {})
    payload = json.loads(result[0].text)
    assert payload["fix_count"] > 0
    fix_titles = [f["title"] for f in payload["diagnosis"]["fixes"]]
    assert any("Orphan target group" in t for t in fix_titles)


# ---------------------------------------------------------------------------
# sudiviz_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_graph_returns_cytoscape_json(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_graph", {"region": "us-east-1"})
    payload = json.loads(result[0].text)
    assert "nodes" in payload
    assert "edges" in payload
    assert len(payload["nodes"]) > 0


# ---------------------------------------------------------------------------
# sudiviz_fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_fix_dry_run_returns_commands(mock_discover):
    discovery = _fake_discovery(
        target_groups=[
            TargetGroup(
                arn="arn:tg/orphan",
                name="orphan-tg",
                protocol="HTTP",
                port=80,
                associated_lb_arns=[],
            ),
        ],
        load_balancers=[
            LoadBalancer(arn="arn:lb/1", name="lb1", listeners=[]),
        ],
        instances=[],
        security_groups=[],
    )
    mock_discover.return_value = discovery
    result = await call_tool("sudiviz_fix", {"dry_run": True})
    payload = json.loads(result[0].text)
    assert isinstance(payload, list)
    assert len(payload) > 0
    assert "aws_cli_command" in payload[0]
    assert payload[0]["applied"] is False


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_fix_no_issues_returns_empty(mock_discover):
    mock_discover.return_value = _fake_discovery(
        load_balancers=[],
        target_groups=[],
        instances=[],
        security_groups=[],
    )
    result = await call_tool("sudiviz_fix", {})
    payload = json.loads(result[0].text)
    assert payload["message"] == "No issues found — nothing to fix!"


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_fix_issue_filter(mock_discover):
    discovery = _fake_discovery(
        target_groups=[
            TargetGroup(arn="arn:tg/orphan", name="orphan-tg", protocol="HTTP", port=80, associated_lb_arns=[]),
        ],
        load_balancers=[LoadBalancer(arn="arn:lb/1", name="lb1", listeners=[])],
        instances=[],
        security_groups=[
            SecurityGroup(sg_id="sg-unused", name="unused-sg", attached_to=[]),
        ],
    )
    mock_discover.return_value = discovery
    result = await call_tool("sudiviz_fix", {"issue_filter": "Orphan"})
    payload = json.loads(result[0].text)
    assert all("Orphan" in fix["title"] or "orphan" in fix["title"].lower() for fix in payload)


# ---------------------------------------------------------------------------
# sudiviz_drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_missing_file_returns_error():
    result = await call_tool("sudiviz_drift", {"tfstate_path": "/nonexistent/file.json"})
    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "not found" in payload["error"].lower()


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_drift_with_valid_state(mock_discover):
    fixture = Path(__file__).parent / "fixtures" / "terraform_state.json"
    if not fixture.exists():
        pytest.skip("terraform fixture not available")

    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_drift", {"tfstate_path": str(fixture)})
    payload = json.loads(result[0].text)
    assert "drift_detected" in payload
    assert "finding_count" in payload
    assert isinstance(payload["findings"], list)


# ---------------------------------------------------------------------------
# sudiviz_costs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_costs_returns_breakdown(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_costs", {"region": "us-east-1"})
    payload = json.loads(result[0].text)
    assert "total_monthly" in payload
    assert "by_service" in payload
    assert "by_resource" in payload
    assert payload["total_monthly"] >= 0


# ---------------------------------------------------------------------------
# sudiviz_list_resources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_list_resources_instances(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_list_resources", {"kind": "instance", "region": "us-east-1"})
    payload = json.loads(result[0].text)
    assert payload["kind"] == "instance"
    assert payload["count"] == 1
    assert payload["resources"][0]["instance_id"] == "i-111"


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_list_resources_alb(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_list_resources", {"kind": "alb"})
    payload = json.loads(result[0].text)
    assert payload["kind"] == "alb"
    assert payload["count"] == 1


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_list_resources_empty_kind(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await call_tool("sudiviz_list_resources", {"kind": "eks_cluster"})
    payload = json.loads(result[0].text)
    assert payload["count"] == 0
    assert payload["resources"] == []


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    result = await call_tool("sudiviz_nonexistent", {})
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# MCP Resource Templates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_resource_templates():
    templates = await list_resource_templates()
    names = {t.name for t in templates}
    assert "aws-topology" in names
    assert "aws-health" in names
    assert "aws-costs" in names
    assert len(templates) == 3


def test_resource_templates_constant():
    assert len(RESOURCE_TEMPLATES) == 3
    for t in RESOURCE_TEMPLATES:
        assert t.uriTemplate.startswith("infra://aws/")
        assert t.mimeType == "application/json"


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_read_resource_topology(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await read_resource("infra://aws/us-east-1/topology")
    assert len(result) == 1
    payload = json.loads(result[0].text)
    assert "nodes" in payload
    assert "edges" in payload


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_read_resource_health(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await read_resource("infra://aws/us-east-1/health")
    payload = json.loads(result[0].text)
    assert "region" in payload
    assert "fix_count" in payload
    assert "critical_count" in payload
    assert "warning_count" in payload
    assert isinstance(payload["fixes"], list)


@pytest.mark.asyncio
@patch("sudiviz.mcp_server.discover_all", new_callable=AsyncMock)
async def test_read_resource_costs(mock_discover):
    mock_discover.return_value = _fake_discovery()
    result = await read_resource("infra://aws/us-east-1/costs")
    payload = json.loads(result[0].text)
    assert "total_monthly" in payload
    assert "by_service" in payload


@pytest.mark.asyncio
async def test_read_resource_unknown_scheme():
    result = await read_resource("ftp://aws/us-east-1/topology")
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_read_resource_unknown_kind():
    result = await read_resource("infra://aws/us-east-1/unknown_kind")
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# MCP Prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_prompts_returns_all():
    prompts = await list_prompts()
    names = {p.name for p in prompts}
    assert "diagnose-infrastructure" in names
    assert "cost-optimization" in names
    assert "security-audit" in names
    assert "incident-triage" in names
    assert len(prompts) == 4


def test_prompts_constant():
    assert len(PROMPTS) == 4
    for p in PROMPTS:
        assert p.description
        assert p.arguments is not None


@pytest.mark.asyncio
async def test_get_prompt_diagnose():
    result = await get_prompt("diagnose-infrastructure", {"region": "eu-west-1", "profile": "prod"})
    assert result.description == "Diagnose AWS infrastructure in eu-west-1"
    assert len(result.messages) == 1
    text = result.messages[0].content.text
    assert "eu-west-1" in text
    assert "prod" in text
    assert "sudiviz_diagnose" in text


@pytest.mark.asyncio
async def test_get_prompt_cost_optimization():
    result = await get_prompt("cost-optimization", {"region": "us-east-1"})
    assert "cost" in result.description.lower()
    text = result.messages[0].content.text
    assert "sudiviz_costs" in text


@pytest.mark.asyncio
async def test_get_prompt_security_audit():
    result = await get_prompt("security-audit", {})
    text = result.messages[0].content.text
    assert "security" in text.lower()
    assert "sudiviz_diagnose" in text


@pytest.mark.asyncio
async def test_get_prompt_incident_triage():
    result = await get_prompt("incident-triage", {"region": "ap-southeast-1"})
    text = result.messages[0].content.text
    assert "ap-southeast-1" in text
    assert "sudiviz_graph" in text


@pytest.mark.asyncio
async def test_get_prompt_unknown():
    result = await get_prompt("nonexistent-prompt", {})
    assert "Unknown prompt" in result.messages[0].content.text


@pytest.mark.asyncio
async def test_get_prompt_no_arguments():
    result = await get_prompt("diagnose-infrastructure", None)
    text = result.messages[0].content.text
    assert "your default region" in text
