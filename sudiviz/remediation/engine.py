"""Remediation engine — translates diagnoses into executable AWS actions.

This module takes a Diagnosis object and generates RemediationAction objects,
each containing:
  - The AWS CLI command to fix the issue
  - The boto3 code to execute it programmatically
  - A human-readable description of what will change

Safety:
  - By default, only generates commands (dry-run)
  - --apply flag required to actually execute
  - Destructive actions (delete) require explicit --force flag
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import boto3
import networkx as nx

from sudiviz.graph.analyzer import Diagnosis, Fix
from sudiviz.utils.auth import build_botocore_config

logger = logging.getLogger(__name__)


@dataclass
class RemediationAction:
    """A single remediation action that can be previewed or applied."""

    fix: Fix
    aws_cli_command: str
    description: str
    is_destructive: bool = False
    applied: bool = False
    error: Optional[str] = None

    # Internal: the actual boto3 call parameters
    _service: str = ""
    _method: str = ""
    _params: dict = field(default_factory=dict)


def generate_fixes(
    diagnosis: Diagnosis,
    graph: nx.DiGraph,
    region: str,
) -> list[RemediationAction]:
    """Generate remediation actions for all diagnosed issues.

    Args:
        diagnosis: The Diagnosis object from the analyzer
        graph: The topology graph (needed for context like SG IDs)
        region: AWS region for the commands

    Returns:
        List of RemediationAction objects ready for preview or execution
    """
    actions: list[RemediationAction] = []

    for fix in diagnosis.fixes:
        action = _generate_action_for_fix(fix, graph, region)
        if action:
            actions.append(action)

    return actions


def _generate_action_for_fix(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> Optional[RemediationAction]:
    """Generate a RemediationAction for a single Fix."""

    # Security group missing port from ALB SG
    if "Security group missing port" in fix.title:
        return _fix_sg_missing_port(fix, graph, region)

    # Orphan target group
    if "Orphan target group" in fix.title:
        return _fix_orphan_target_group(fix, graph, region)

    # Unused security group
    if "Unused security group" in fix.title:
        return _fix_unused_security_group(fix, graph, region)

    # S3 public access not blocked
    if "public access not fully blocked" in fix.title:
        return _fix_s3_public_access(fix, graph, region)

    # S3 encryption not enabled
    if "server-side encryption not enabled" in fix.title:
        return _fix_s3_encryption(fix, graph, region)

    # RDS publicly accessible
    if "publicly accessible" in fix.title and "RDS" in fix.title:
        return _fix_rds_public_access(fix, graph, region)

    # No automated fix available
    return RemediationAction(
        fix=fix,
        aws_cli_command=f"# No automated fix available for: {fix.title}",
        description=f"Manual intervention required: {fix.detail}",
        _service="",
        _method="",
        _params={},
    )


def _fix_sg_missing_port(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for: Security group missing port X from ALB SG."""

    # Parse port from title: "Security group missing port 80 from ALB SG"
    port_match = re.search(r"port (\d+)", fix.title)
    port = int(port_match.group(1)) if port_match else 80

    # Get instance node and its security groups
    instance_id = fix.resource_id
    instance_sg_ids = []
    alb_sg_ids = []

    if instance_id and instance_id in graph:
        # Find instance's security groups (instance -> guarded_by -> SG)
        for _u, sg_node, edge in graph.out_edges(instance_id, data=True):
            if edge.get("relation") == "guarded_by":
                sg_attrs = graph.nodes.get(sg_node, {})
                if sg_attrs.get("kind") == "security_group":
                    instance_sg_ids.append(sg_node)

        # Find TG that this instance is registered in (instance -> registered_in -> TG)
        for _inst, tg_node, edge in graph.out_edges(instance_id, data=True):
            if edge.get("relation") == "registered_in":
                # Find ALB that forwards to this TG (ALB -> forwards_to -> TG)
                for alb_node, _tg, alb_edge in graph.in_edges(tg_node, data=True):
                    if alb_edge.get("relation") == "forwards_to":
                        alb_attrs = graph.nodes.get(alb_node, {})
                        alb_sg_ids = alb_attrs.get("metadata", {}).get("security_group_ids", [])
                        if alb_sg_ids:
                            break
                if alb_sg_ids:
                    break

    # If we still don't have ALB SGs, search all ALB nodes directly
    if not alb_sg_ids:
        for node, attrs in graph.nodes(data=True):
            if attrs.get("kind") == "alb":
                alb_sg_ids = attrs.get("metadata", {}).get("security_group_ids", [])
                if alb_sg_ids:
                    break

    # Use first SG from each (simplification)
    target_sg = instance_sg_ids[0] if instance_sg_ids else "sg-INSTANCE_SG_ID"
    source_sg = alb_sg_ids[0] if alb_sg_ids else "sg-ALB_SG_ID"

    aws_cli = (
        f"aws ec2 authorize-security-group-ingress \\\n"
        f"  --region {region} \\\n"
        f"  --group-id {target_sg} \\\n"
        f"  --protocol tcp \\\n"
        f"  --port {port} \\\n"
        f"  --source-group {source_sg}"
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Add inbound rule to {target_sg}: allow TCP/{port} from {source_sg}",
        is_destructive=False,
        _service="ec2",
        _method="authorize_security_group_ingress",
        _params={
            "GroupId": target_sg,
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [{"GroupId": source_sg}],
                }
            ],
        },
    )


def _fix_orphan_target_group(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for orphan target group (delete it)."""
    tg_arn = fix.resource_id or "arn:aws:elasticloadbalancing:REGION:ACCOUNT:targetgroup/NAME/ID"

    aws_cli = (
        f"aws elbv2 delete-target-group \\\n"
        f"  --region {region} \\\n"
        f"  --target-group-arn {tg_arn}"
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Delete orphan target group: {fix.resource_id}",
        is_destructive=True,
        _service="elbv2",
        _method="delete_target_group",
        _params={"TargetGroupArn": tg_arn},
    )


def _fix_unused_security_group(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for unused security group (delete it)."""
    sg_id = fix.resource_id or "sg-SECURITY_GROUP_ID"

    aws_cli = (
        f"aws ec2 delete-security-group \\\n"
        f"  --region {region} \\\n"
        f"  --group-id {sg_id}"
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Delete unused security group: {sg_id}",
        is_destructive=True,
        _service="ec2",
        _method="delete_security_group",
        _params={"GroupId": sg_id},
    )


def _fix_s3_public_access(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for S3 bucket without public access block."""
    # Extract bucket name from resource_id (arn:aws:s3:::bucket-name)
    bucket_arn = fix.resource_id or ""
    bucket_name = bucket_arn.replace("arn:aws:s3:::", "") if bucket_arn else "BUCKET_NAME"

    aws_cli = (
        f"aws s3api put-public-access-block \\\n"
        f"  --bucket {bucket_name} \\\n"
        f"  --public-access-block-configuration \\\n"
        f'    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Enable public access block on S3 bucket: {bucket_name}",
        is_destructive=False,
        _service="s3",
        _method="put_public_access_block",
        _params={
            "Bucket": bucket_name,
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def _fix_s3_encryption(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for S3 bucket without encryption."""
    bucket_arn = fix.resource_id or ""
    bucket_name = bucket_arn.replace("arn:aws:s3:::", "") if bucket_arn else "BUCKET_NAME"

    aws_cli = (
        f"aws s3api put-bucket-encryption \\\n"
        f"  --bucket {bucket_name} \\\n"
        f"  --server-side-encryption-configuration \\\n"
        f'    \'{{"Rules":[{{"ApplyServerSideEncryptionByDefault":{{"SSEAlgorithm":"AES256"}}}}]}}\''
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Enable SSE-S3 encryption on bucket: {bucket_name}",
        is_destructive=False,
        _service="s3",
        _method="put_bucket_encryption",
        _params={
            "Bucket": bucket_name,
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256",
                        },
                    },
                ],
            },
        },
    )


def _fix_rds_public_access(
    fix: Fix,
    graph: nx.DiGraph,
    region: str,
) -> RemediationAction:
    """Generate fix for publicly accessible RDS instance."""
    # Extract DB identifier from title or resource_id
    db_arn = fix.resource_id or ""
    db_id = db_arn.split(":")[-1] if db_arn else "DB_INSTANCE_ID"

    aws_cli = (
        f"aws rds modify-db-instance \\\n"
        f"  --region {region} \\\n"
        f"  --db-instance-identifier {db_id} \\\n"
        f"  --no-publicly-accessible \\\n"
        f"  --apply-immediately"
    )

    return RemediationAction(
        fix=fix,
        aws_cli_command=aws_cli,
        description=f"Disable public accessibility on RDS: {db_id}",
        is_destructive=False,
        _service="rds",
        _method="modify_db_instance",
        _params={
            "DBInstanceIdentifier": db_id,
            "PubliclyAccessible": False,
            "ApplyImmediately": True,
        },
    )


def apply_fix(
    action: RemediationAction,
    session: Optional[boto3.Session] = None,
    dry_run: bool = True,
) -> RemediationAction:
    """Apply a single remediation action.

    Args:
        action: The RemediationAction to apply
        session: Optional boto3 session (uses default if not provided)
        dry_run: If True, only validate but don't execute

    Returns:
        The same action with `applied` and `error` fields updated
    """
    if not action._service or not action._method:
        action.error = "No automated fix available"
        return action

    if dry_run:
        action.applied = False
        return action

    sess = session or boto3.Session()
    client = sess.client(action._service, config=build_botocore_config())

    try:
        method = getattr(client, action._method)
        method(**action._params)
        action.applied = True
        logger.info("Applied fix: %s", action.description)
    except Exception as e:
        action.error = str(e)
        logger.error("Failed to apply fix: %s — %s", action.description, e)

    return action