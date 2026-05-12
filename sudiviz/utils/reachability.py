"""Thin wrapper around AWS VPC Reachability Analyzer.

Reachability Analyzer is a paid feature ($0.10/path), so we only call it when
the user explicitly opts in (`--analyze-paths`). Most failures are diagnosable
from security groups + target health alone.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from sudiviz.utils.auth import build_botocore_config

logger = logging.getLogger(__name__)


def analyze_path(
    source_arn: str,
    destination_arn: str,
    destination_port: int,
    protocol: str = "tcp",
    session: Optional[boto3.Session] = None,
    poll_interval: float = 3.0,
    max_wait: float = 90.0,
) -> dict:
    """Create a network insights path, run analysis, and return the result.

    Returns the raw NetworkInsightsAnalysis dict from boto3. Sudiviz callers
    typically only care about `NetworkPathFound` and `ForwardPathComponents`
    (the human-readable hop list).
    """
    sess = session or boto3.Session()
    ec2 = sess.client("ec2", config=build_botocore_config())

    path = ec2.create_network_insights_path(
        Source=source_arn,
        Destination=destination_arn,
        DestinationPort=destination_port,
        Protocol=protocol.upper(),
    )["NetworkInsightsPath"]
    path_id = path["NetworkInsightsPathId"]

    try:
        analysis = ec2.start_network_insights_analysis(NetworkInsightsPathId=path_id)
        analysis_id = analysis["NetworkInsightsAnalysis"]["NetworkInsightsAnalysisId"]

        deadline = time.time() + max_wait
        while time.time() < deadline:
            resp = ec2.describe_network_insights_analyses(
                NetworkInsightsAnalysisIds=[analysis_id]
            )
            current = resp["NetworkInsightsAnalyses"][0]
            if current["Status"] in {"succeeded", "failed"}:
                return current
            time.sleep(poll_interval)
        raise TimeoutError(f"Reachability analysis {analysis_id} did not finish in {max_wait}s")
    finally:
        # Always clean up the path resource so we don't leak billable artifacts.
        try:
            ec2.delete_network_insights_path(NetworkInsightsPathId=path_id)
        except ClientError as exc:
            logger.warning("Failed to delete network insights path %s: %s", path_id, exc)


def summarize_analysis(analysis: dict) -> str:
    """Render a one-line summary of a Reachability Analyzer result."""
    if analysis.get("NetworkPathFound"):
        return "Reachable: path found"
    explanations = analysis.get("Explanations") or []
    if explanations:
        codes = sorted({e.get("ExplanationCode", "?") for e in explanations})
        return f"Unreachable: {', '.join(codes)}"
    return "Unreachable: no path"
