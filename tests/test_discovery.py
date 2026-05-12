"""Tests for discovery layer — Pydantic models + Terraform parser.

The AWS discovery functions are exercised lightly via mocked boto3 sessions;
the heavy logic (orphan detection, graph wiring) lives in graph/* and is
tested separately. We don't try to integration-test against real AWS here.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sudiviz.discovery.aws import _parse_service_tag, discover_all
from sudiviz.discovery.models import (
    DiscoveryResult,
    HealthStatus,
    Instance,
    Listener,
    LoadBalancer,
    SecurityGroup,
    Target,
    TargetGroup,
)
from sudiviz.discovery.terraform import (
    detect_drift,
    load_state,
    parse_intended_resources,
)


FIXTURE = Path(__file__).parent / "fixtures" / "terraform_state.json"


# ---------------------------------------------------------------------------
# Pydantic model invariants
# ---------------------------------------------------------------------------


def test_target_group_orphan_property() -> None:
    tg = TargetGroup(arn="arn:tg/empty", name="empty", protocol="HTTP", port=80)
    assert tg.is_orphan is True
    tg2 = TargetGroup(
        arn="arn:tg/used",
        name="used",
        protocol="HTTP",
        port=80,
        associated_lb_arns=["arn:lb/main"],
    )
    assert tg2.is_orphan is False


def test_target_group_health_aggregation() -> None:
    tg = TargetGroup(
        arn="arn:tg/x",
        name="x",
        protocol="HTTP",
        port=80,
        targets=[
            Target(target_id="i-1", target_type="instance", health=HealthStatus.HEALTHY),
            Target(target_id="i-2", target_type="instance", health=HealthStatus.UNHEALTHY),
        ],
    )
    assert tg.healthy_count == 1
    assert tg.total_count == 2


def test_instance_orphan_property() -> None:
    inst = Instance(instance_id="i-orphan")
    assert inst.is_orphan is True
    inst2 = Instance(instance_id="i-attached", target_group_arns=["arn:tg/web"])
    assert inst2.is_orphan is False


def test_security_group_orphan_property() -> None:
    sg = SecurityGroup(sg_id="sg-x", name="x")
    assert sg.is_orphan is True
    sg.attached_to = ["eni-1"]
    assert sg.is_orphan is False


# ---------------------------------------------------------------------------
# Service tag parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ("", {}),
        ("Service=checkout", {"Service": "checkout"}),
        ("k1=v1,k2=v2", {"k1": "v1", "k2": "v2"}),
        ("noequalsign", {}),
    ],
)
def test_parse_service_tag(raw, expected) -> None:
    assert _parse_service_tag(raw) == expected


# ---------------------------------------------------------------------------
# Terraform parser
# ---------------------------------------------------------------------------


def test_parse_intended_resources_walks_root_module() -> None:
    state = load_state(FIXTURE)
    intended = parse_intended_resources(state)
    types = {r.type for r in intended}
    assert "aws_lb" in types
    assert "aws_lb_target_group" in types
    assert "aws_lb_listener" in types
    assert "aws_security_group" in types
    assert "aws_instance" in types


def test_detect_drift_flags_missing_and_orphan_listener() -> None:
    state = load_state(FIXTURE)
    intended = parse_intended_resources(state)

    # Live state: only the ALB exists; no TG, no listener, no SG, no instance.
    live = DiscoveryResult(
        load_balancers=[
            LoadBalancer(
                arn="arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/public/abc",
                name="public",
                listeners=[],
            )
        ]
    )

    findings = detect_drift(intended, live)
    kinds = {f.kind for f in findings}
    # TG is in TF but not live → 'missing'.
    assert "missing" in kinds
    # Listener references a TG that no live listener forwards to → 'orphan_listener'.
    assert "orphan_listener" in kinds


def test_detect_drift_clean_when_state_matches() -> None:
    intended = parse_intended_resources(load_state(FIXTURE))
    live = DiscoveryResult(
        load_balancers=[
            LoadBalancer(
                arn="arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/public/abc",
                name="public",
                listeners=[
                    Listener(
                        arn="arn:aws:elasticloadbalancing:us-east-1:111111111111:listener/app/public/abc/list1",
                        lb_arn="arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/public/abc",
                        protocol="HTTP",
                        port=80,
                        default_target_group_arns=[
                            "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/web/123"
                        ],
                    )
                ],
            )
        ],
        target_groups=[
            TargetGroup(
                arn="arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/web/123",
                name="web",
                protocol="HTTP",
                port=80,
            )
        ],
        security_groups=[SecurityGroup(sg_id="sg-alb", name="alb-sg")],
        instances=[Instance(instance_id="i-aaaaaaaaaaaaaaaaa")],
    )

    findings = detect_drift(intended, live)
    # No 'missing' or 'orphan_listener' findings — the state matches.
    kinds = {f.kind for f in findings}
    assert "missing" not in kinds
    assert "orphan_listener" not in kinds


# ---------------------------------------------------------------------------
# discover_all happy path with mocked boto3
# ---------------------------------------------------------------------------


@patch("sudiviz.discovery.aws.whoami")
@patch("sudiviz.discovery.aws._discover_instances_sync")
@patch("sudiviz.discovery.aws._discover_security_groups_sync")
@patch("sudiviz.discovery.aws._discover_target_groups_sync")
@patch("sudiviz.discovery.aws._discover_listeners_sync")
@patch("sudiviz.discovery.aws._discover_albs_sync")
def test_discover_all_orchestrates(
    mock_albs,
    mock_listeners,
    mock_tgs,
    mock_sgs,
    mock_instances,
    mock_whoami,
) -> None:
    import asyncio

    mock_whoami.return_value = MagicMock(account_id="111", region="us-east-1")
    lb = LoadBalancer(arn="arn:lb/1", name="lb1")
    tg = TargetGroup(arn="arn:tg/1", name="tg1", protocol="HTTP", port=80, vpc_id="vpc-1")

    mock_albs.return_value = [lb]
    mock_listeners.return_value = [Listener(arn="lst-1", lb_arn=lb.arn, protocol="HTTP", port=80, default_target_group_arns=[tg.arn])]
    mock_tgs.return_value = [tg]
    mock_sgs.return_value = []
    mock_instances.return_value = []

    result = asyncio.run(discover_all(vpc_id="vpc-1"))

    assert result.account_id == "111"
    assert result.load_balancers[0].arn == "arn:lb/1"
    # Listener wiring back-fills tg.associated_lb_arns.
    assert result.target_groups[0].associated_lb_arns == ["arn:lb/1"]
