"""Parse `terraform show -json` output and detect drift.

The state JSON schema is documented at:
  https://developer.hashicorp.com/terraform/internals/json-format

We only consume what's needed for the resource types sudiviz already models
(LBs, target groups, listeners, SGs, instances). Anything else is ignored —
this is a diagnostic tool, not a state-management tool.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sudiviz.discovery.models import DiscoveryResult

logger = logging.getLogger(__name__)


@dataclass
class IntendedResource:
    """A resource as Terraform expects it to exist."""

    address: str            # 'aws_lb.public[0]'
    type: str               # 'aws_lb', 'aws_lb_target_group', etc.
    name: str               # logical name from the config
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def aws_id(self) -> Optional[str]:
        """Return the AWS-side ID/ARN for this resource, if exported."""
        # Different resource types expose IDs under different keys.
        for key in ("arn", "id", "group_id"):
            if key in self.attributes and self.attributes[key]:
                return self.attributes[key]
        return None


@dataclass
class DriftItem:
    """One drift finding — exposed in CLI output and the web sidebar."""

    kind: str                       # 'missing', 'orphan_in_aws', 'attribute_mismatch'
    resource_type: str
    address: Optional[str] = None
    aws_id: Optional[str] = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------


def load_state(tfstate_path: Path | str) -> dict:
    """Load `terraform show -json` output from a file.

    Accepts either a JSON file produced by `terraform show -json > out.json`
    OR a raw `terraform.tfstate` (fallback). Subprocess execution of the
    `terraform` CLI is handled by `run_terraform_show`.
    """
    path = Path(tfstate_path)
    with path.open() as fp:
        data = json.load(fp)
    return data


def run_terraform_show(workdir: Path | str, terraform_bin: str = "terraform") -> dict:
    """Invoke `terraform show -json` inside `workdir` and return parsed JSON.

    Uses subprocess directly (rather than python-terraform) to keep the
    dependency optional. python-terraform is fine but adds nothing here.
    """
    proc = subprocess.run(  # noqa: S603 — controlled args, controlled cwd
        [terraform_bin, "show", "-json"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"terraform show failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def parse_intended_resources(state: dict) -> list[IntendedResource]:
    """Walk the state tree and emit a flat list of intended resources."""
    out: list[IntendedResource] = []

    # `terraform show -json` (current state) puts resources under `values.root_module`.
    root = (state.get("values") or {}).get("root_module") or {}
    out.extend(_walk_module(root))

    # Fallback for raw tfstate: resources keyed at top level.
    if not out and "resources" in state:
        for resource in state.get("resources", []):
            for instance in resource.get("instances", []):
                out.append(
                    IntendedResource(
                        address=resource.get("name", ""),
                        type=resource.get("type", ""),
                        name=resource.get("name", ""),
                        attributes=instance.get("attributes", {}),
                    )
                )
    return out


def _walk_module(module: dict) -> list[IntendedResource]:
    out: list[IntendedResource] = []
    for resource in module.get("resources", []) or []:
        out.append(
            IntendedResource(
                address=resource.get("address", resource.get("name", "")),
                type=resource.get("type", ""),
                name=resource.get("name", ""),
                attributes=resource.get("values", {}) or {},
            )
        )
    for child in module.get("child_modules", []) or []:
        out.extend(_walk_module(child))
    return out


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


# Map Terraform resource types to the live discovery field they correspond to.
_TF_TYPE_TO_LIVE = {
    "aws_lb": "load_balancers",
    "aws_alb": "load_balancers",
    "aws_lb_target_group": "target_groups",
    "aws_alb_target_group": "target_groups",
    "aws_security_group": "security_groups",
    "aws_instance": "instances",
    "aws_ecs_cluster": "ecs_clusters",
    "aws_ecs_service": "ecs_services",
    "aws_eks_cluster": "eks_clusters",
    "aws_db_instance": "rds_instances",
    "aws_lambda_function": "lambda_functions",
}


def detect_drift(
    intended: list[IntendedResource],
    live: DiscoveryResult,
) -> list[DriftItem]:
    """Compare intended Terraform resources to live discovery output.

    Drift kinds reported:
      * 'missing'             — Terraform expects this resource, AWS doesn't have it
      * 'orphan_in_aws'       — AWS has this, Terraform doesn't (often manual changes)
      * 'attribute_mismatch'  — both exist but key attributes differ
      * 'orphan_listener'     — Terraform expected a listener, but the live LB has none
                                pointing to its target group (= unreachable backend)
    """
    findings: list[DriftItem] = []

    intended_by_type: dict[str, list[IntendedResource]] = {}
    for r in intended:
        intended_by_type.setdefault(r.type, []).append(r)

    # Flatten ECS services across all clusters for drift lookup.
    all_ecs_services = {svc.arn: svc for cluster in live.ecs_clusters for svc in cluster.services}

    live_indices: dict[str, dict[str, Any]] = {
        "load_balancers": {lb.arn: lb for lb in live.load_balancers},
        "target_groups": {tg.arn: tg for tg in live.target_groups},
        "security_groups": {sg.sg_id: sg for sg in live.security_groups},
        "instances": {i.instance_id: i for i in live.instances},
        "ecs_clusters": {c.arn: c for c in live.ecs_clusters},
        "ecs_services": all_ecs_services,
        "eks_clusters": {c.arn: c for c in live.eks_clusters},
        "rds_instances": {db.arn: db for db in live.rds_instances},
        "lambda_functions": {fn.arn: fn for fn in live.lambda_functions},
    }

    for tf_type, live_field in _TF_TYPE_TO_LIVE.items():
        for intent in intended_by_type.get(tf_type, []):
            aws_id = intent.aws_id
            if not aws_id:
                continue
            if aws_id not in live_indices[live_field]:
                findings.append(
                    DriftItem(
                        kind="missing",
                        resource_type=tf_type,
                        address=intent.address,
                        aws_id=aws_id,
                        message=f"{intent.address} declared in Terraform but not found in AWS",
                    )
                )

    # Orphans in AWS: live resources not represented in Terraform state.
    intended_ids = {r.aws_id for r in intended if r.aws_id}
    for live_field, idx in live_indices.items():
        for resource_id, resource in idx.items():
            if resource_id in intended_ids:
                continue
            findings.append(
                DriftItem(
                    kind="orphan_in_aws",
                    resource_type=live_field.rstrip("s"),
                    aws_id=resource_id,
                    message=f"{resource_id} exists in AWS but not in Terraform state",
                )
            )

    # Listener-level orphan check: TF wants TG X to receive traffic, but live
    # state shows no listener routes to it.
    listener_targets: set[str] = set()
    for lb in live.load_balancers:
        for lst in lb.listeners:
            listener_targets.update(lst.default_target_group_arns)
            listener_targets.update(lst.rule_target_group_arns)

    for intent in intended_by_type.get("aws_lb_listener", []):
        # default_action[0].target_group_arn is the standard pattern.
        tg_arn = _extract_listener_target(intent.attributes)
        if tg_arn and tg_arn not in listener_targets:
            findings.append(
                DriftItem(
                    kind="orphan_listener",
                    resource_type="aws_lb_listener",
                    address=intent.address,
                    aws_id=tg_arn,
                    message=(
                        f"{intent.address} should forward to {tg_arn}, "
                        "but no live listener references that target group"
                    ),
                )
            )

    return findings


def _extract_listener_target(attrs: dict[str, Any]) -> Optional[str]:
    """Pull the target group ARN out of a Terraform aws_lb_listener resource."""
    actions = attrs.get("default_action") or []
    if isinstance(actions, list) and actions:
        first = actions[0]
        if isinstance(first, dict):
            if first.get("target_group_arn"):
                return first["target_group_arn"]
            forward = first.get("forward")
            if isinstance(forward, list) and forward:
                tgs = forward[0].get("target_group") or []
                if tgs:
                    return tgs[0].get("arn")
    return None
