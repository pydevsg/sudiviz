"""Tests for graph analyzer — orphan detection + diagnosis output.

These tests construct synthetic NetworkX graphs that mirror the schema produced
by `graph.builder`, so they exercise analyzer logic without touching boto3.
"""
from __future__ import annotations

import networkx as nx
import pytest

from sudiviz.discovery.models import HealthStatus
from sudiviz.graph.analyzer import (
    diagnose,
    find_unattached_instances,
    find_unattached_target_groups,
    find_unused_security_groups,
    mark_orphaned_edges,
)


def _add_alb(g: nx.DiGraph, arn: str, sg_ids: list[str] | None = None) -> None:
    g.add_node(
        arn,
        kind="alb",
        label=arn.split("/")[-1],
        health=HealthStatus.HEALTHY.value,
        id=arn,
        metadata={"security_group_ids": sg_ids or []},
        orphan=False,
    )


def _add_tg(g: nx.DiGraph, arn: str, port: int = 80, healthy: int = 1, total: int = 1) -> None:
    g.add_node(
        arn,
        kind="target_group",
        label=arn.split("/")[-1],
        health=HealthStatus.HEALTHY.value if healthy == total else HealthStatus.UNHEALTHY.value,
        id=arn,
        port=port,
        healthy_count=healthy,
        total_count=total,
        metadata={},
        orphan=False,
    )


def _add_instance(g: nx.DiGraph, instance_id: str) -> None:
    g.add_node(
        instance_id,
        kind="instance",
        label=instance_id,
        health=HealthStatus.HEALTHY.value,
        id=instance_id,
        metadata={},
        orphan=False,
    )


def _add_sg(g: nx.DiGraph, sg_id: str, rules: list[dict] | None = None) -> None:
    g.add_node(
        sg_id,
        kind="security_group",
        label=sg_id,
        health=HealthStatus.HEALTHY.value,
        id=sg_id,
        metadata={"rules": rules or []},
        orphan=False,
    )


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def test_target_group_with_no_listener_is_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_tg(g, "tg/orphan")
    orphans = find_unattached_target_groups(g)
    assert len(orphans) == 1
    assert orphans[0]["id"] == "tg/orphan"


def test_target_group_with_listener_is_not_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_alb(g, "alb/main")
    _add_tg(g, "tg/web")
    g.add_edge("alb/main", "tg/web", relation="forwards_to")
    assert find_unattached_target_groups(g) == []


def test_instance_with_no_target_group_is_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_instance(g, "i-orphan")
    orphans = find_unattached_instances(g)
    assert orphans[0]["id"] == "i-orphan"


def test_instance_registered_is_not_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_tg(g, "tg/web")
    _add_instance(g, "i-1")
    g.add_edge("i-1", "tg/web", relation="registered_in")
    assert find_unattached_instances(g) == []


def test_unused_security_group_is_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_sg(g, "sg-abandoned")
    orphans = find_unused_security_groups(g)
    assert orphans[0]["id"] == "sg-abandoned"


def test_attached_security_group_is_not_orphan() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_sg(g, "sg-web")
    _add_instance(g, "i-1")
    g.add_edge("i-1", "sg-web", relation="guarded_by")
    assert find_unused_security_groups(g) == []


def test_mark_orphaned_edges_dashes_red() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_tg(g, "tg/orphan")  # orphan
    _add_alb(g, "alb/main")
    _add_tg(g, "tg/web")
    _add_instance(g, "i-1")
    g.add_edge("alb/main", "tg/web", relation="forwards_to")
    g.add_edge("i-1", "tg/web", relation="registered_in")

    mark_orphaned_edges(g)

    # Orphan node flagged.
    assert g.nodes["tg/orphan"]["orphan"] is True
    # Healthy edges not flagged.
    assert g.edges["alb/main", "tg/web"].get("orphan") is False
    # No edges incident on the orphan TG (none were added) — but if we add one:
    g.add_edge("alb/main", "tg/orphan", relation="forwards_to")
    mark_orphaned_edges(g)
    edge = g.edges["alb/main", "tg/orphan"]
    assert edge["orphan"] is True
    assert edge["style"] == "dashed"


# ---------------------------------------------------------------------------
# Diagnosis (fix suggestions)
# ---------------------------------------------------------------------------


def test_diagnose_unhealthy_target_group_yields_critical_fix() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_alb(g, "alb/main")
    _add_tg(g, "tg/web", healthy=0, total=3)
    g.add_edge("alb/main", "tg/web", relation="forwards_to")

    diag = diagnose(g)
    titles = [f.title for f in diag.fixes if f.severity == "critical"]
    assert any("0/3 healthy" in t for t in titles)


def test_diagnose_security_group_missing_rule_emits_critical_fix() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_alb(g, "alb/main", sg_ids=["sg-alb"])
    _add_tg(g, "tg/web", port=80, healthy=1, total=1)
    _add_instance(g, "i-1")
    _add_sg(g, "sg-alb")
    _add_sg(g, "sg-web", rules=[])  # no ingress at all

    g.add_edge("alb/main", "tg/web", relation="forwards_to")
    g.add_edge("i-1", "tg/web", relation="registered_in")
    g.add_edge("i-1", "sg-web", relation="guarded_by")

    diag = diagnose(g)
    sg_fixes = [f for f in diag.fixes if "Security group missing port" in f.title]
    assert sg_fixes, "expected a security-group fix"
    assert sg_fixes[0].severity == "critical"


def test_diagnose_with_correct_sg_does_not_emit_fix() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_alb(g, "alb/main", sg_ids=["sg-alb"])
    _add_tg(g, "tg/web", port=80, healthy=1, total=1)
    _add_instance(g, "i-1")
    _add_sg(g, "sg-alb")
    _add_sg(
        g,
        "sg-web",
        rules=[
            {
                "direction": "ingress",
                "protocol": "tcp",
                "from_port": 80,
                "to_port": 80,
                "referenced_sg_ids": ["sg-alb"],
                "cidr_ranges": [],
            }
        ],
    )
    g.add_edge("alb/main", "tg/web", relation="forwards_to")
    g.add_edge("i-1", "tg/web", relation="registered_in")
    g.add_edge("i-1", "sg-web", relation="guarded_by")

    diag = diagnose(g)
    sg_fixes = [f for f in diag.fixes if "Security group missing port" in f.title]
    assert sg_fixes == []


def test_diagnose_orphan_tg_yields_warning_fix() -> None:
    g: nx.DiGraph = nx.DiGraph()
    _add_tg(g, "tg/lonely", healthy=0, total=0)
    diag = diagnose(g)
    assert any("Orphan target group" in f.title and f.severity == "warning" for f in diag.fixes)


@pytest.mark.parametrize(
    "scenario,expected_severity",
    [
        ("orphan_tg", "warning"),
        ("orphan_inst", "info"),
        ("orphan_sg", "info"),
    ],
)
def test_orphan_severities(scenario: str, expected_severity: str) -> None:
    g: nx.DiGraph = nx.DiGraph()
    if scenario == "orphan_tg":
        _add_tg(g, "tg/lonely", healthy=0, total=0)
    elif scenario == "orphan_inst":
        _add_instance(g, "i-x")
    elif scenario == "orphan_sg":
        _add_sg(g, "sg-x")

    diag = diagnose(g)
    assert any(f.severity == expected_severity for f in diag.fixes)
