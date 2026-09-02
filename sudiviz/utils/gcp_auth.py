"""GCP authentication helpers.

We never accept credentials as CLI arguments. Authentication relies on
Google Cloud's Application Default Credentials (ADC) chain: service account
key file, ``gcloud auth application-default login``, GCE metadata server,
Workload Identity, etc.  This module wraps credential and project resolution
and exposes identity metadata that the visualizer surfaces in the status bar.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-import guard — give a clear message when google-cloud SDKs are absent.
# ---------------------------------------------------------------------------

_GCP_IMPORT_ERROR: Optional[str] = None

try:
    import google.auth  # type: ignore[import-untyped]
    import google.auth.transport.requests  # type: ignore[import-untyped]
    from google.auth import exceptions as gauth_exceptions  # type: ignore[import-untyped]
except ImportError:
    _GCP_IMPORT_ERROR = (
        "google-cloud SDK is not installed. Install the GCP extras:\n"
        "  pip install sudiviz[gcp]"
    )


def _require_gcp_sdk() -> None:
    """Raise ``RuntimeError`` with an install hint if the SDK is missing."""
    if _GCP_IMPORT_ERROR:
        raise RuntimeError(_GCP_IMPORT_ERROR)


# ---------------------------------------------------------------------------
# Project ID validation
# ---------------------------------------------------------------------------

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def validate_project_id(project_id: str) -> str:
    """Validate and return a GCP project ID.

    Project IDs must be 6-30 characters, lowercase letters, digits, and
    hyphens only, starting with a letter and not ending with a hyphen.
    """
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid GCP project ID: {project_id!r}. Must be 6-30 chars, "
            "lowercase alphanumeric + hyphens, starting with a letter."
        )
    return project_id


# ---------------------------------------------------------------------------
# Credentials & identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GcpIdentity:
    """Resolved GCP identity — shown in the TUI/web status bar."""

    project_id: str
    account_email: Optional[str]
    region: str


def get_gcp_credentials():
    """Return Application Default Credentials.

    Raises ``RuntimeError`` with a helpful message if ADC resolution fails.
    """
    _require_gcp_sdk()
    try:
        credentials, _ = google.auth.default()
        return credentials
    except gauth_exceptions.DefaultCredentialsError as exc:
        raise RuntimeError(
            "No GCP credentials found. Configure via:\n"
            "  gcloud auth application-default login\n"
            "or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key.\n"
            "sudiviz never accepts credentials as CLI flags."
        ) from exc


def get_gcp_project_id(project: Optional[str] = None) -> str:
    """Resolve the GCP project ID.

    Resolution order:
      1. Explicit ``project`` parameter (validated)
      2. ``GOOGLE_CLOUD_PROJECT`` / ``GCLOUD_PROJECT`` / ``GCP_PROJECT`` env
      3. ADC metadata (the project associated with the credentials)

    Raises ``RuntimeError`` if no project can be determined.
    """
    _require_gcp_sdk()
    # 1. Explicit parameter
    if project and isinstance(project, str):
        return validate_project_id(project)

    # 2. Environment variables (in precedence order)
    for env_var in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT"):
        env_val = os.environ.get(env_var)
        if env_val:
            return validate_project_id(env_val)

    # 3. ADC metadata
    try:
        _, adc_project = google.auth.default()
        if adc_project:
            return adc_project
    except gauth_exceptions.DefaultCredentialsError:
        pass

    raise RuntimeError(
        "Could not determine GCP project ID. Set --project, the "
        "GOOGLE_CLOUD_PROJECT env var, or configure ADC with a project."
    )


def gcp_whoami(
    project: Optional[str] = None,
    region: Optional[str] = None,
) -> GcpIdentity:
    """Resolve GCP caller identity — analogous to ``aws whoami``.

    Returns a ``GcpIdentity`` with project, account email, and region.
    """
    _require_gcp_sdk()
    credentials = get_gcp_credentials()
    project_id = get_gcp_project_id(project)
    resolved_region = region or os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")

    # Try to get the service account email from credentials
    account_email: Optional[str] = None
    if hasattr(credentials, "service_account_email"):
        account_email = credentials.service_account_email
    elif hasattr(credentials, "_service_account_email"):
        account_email = credentials._service_account_email

    return GcpIdentity(
        project_id=project_id,
        account_email=account_email,
        region=resolved_region,
    )


# ---------------------------------------------------------------------------
# Console deep links
# ---------------------------------------------------------------------------


def gcp_console_url(
    resource_type: str,
    resource_id: str,
    project: str,
    region: str,
) -> str:
    """Build a GCP Cloud Console deep link for a resource.

    ``resource_type`` is the sudiviz-internal kind: 'instance', 'alb',
    'rds', 'eks_cluster', 'lambda', 's3', 'security_group', etc.
    """
    base = "https://console.cloud.google.com"
    rt = resource_type.lower()

    if rt == "instance":
        # Compute Engine VM — resource_id is the self-link or instance name
        name = resource_id.rsplit("/", 1)[-1]
        zone = _zone_from_self_link(resource_id) or f"{region}-a"
        return f"{base}/compute/instancesDetail/zones/{zone}/instances/{name}?project={project}"

    if rt == "alb":
        # Cloud Load Balancer (forwarding rule / URL map)
        name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/net-services/loadbalancing/details/http/{name}?project={project}"

    if rt == "target_group":
        # Instance group
        name = resource_id.rsplit("/", 1)[-1]
        zone_or_region = _zone_from_self_link(resource_id) or region
        return f"{base}/compute/instanceGroups/details/{zone_or_region}/{name}?project={project}"

    if rt == "security_group":
        # Firewall rule
        name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/networking/firewalls/details/{name}?project={project}"

    if rt in ("eks_cluster", "gke_cluster"):
        # GKE cluster
        name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/kubernetes/clusters/details/{region}/{name}/details?project={project}"

    if rt == "eks_nodegroup":
        # GKE node pool — resource_id format: clusters/<cluster>/nodePools/<pool>
        parts = resource_id.split("/")
        cluster_name = parts[-3] if len(parts) >= 3 else "unknown"
        pool_name = parts[-1]
        return f"{base}/kubernetes/nodepool/{region}/{cluster_name}/{pool_name}?project={project}"

    if rt == "rds":
        # Cloud SQL
        name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/sql/instances/{name}/overview?project={project}"

    if rt == "lambda":
        # Cloud Functions
        name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/functions/details/{region}/{name}?project={project}"

    if rt == "s3":
        # Cloud Storage
        bucket_name = resource_id.rsplit("/", 1)[-1]
        return f"{base}/storage/browser/{bucket_name}?project={project}"

    # Fallback: project dashboard
    return f"{base}/home/dashboard?project={project}"


def _zone_from_self_link(self_link: str) -> Optional[str]:
    """Extract zone from a GCP self-link URL, e.g. .../zones/us-central1-a/..."""
    parts = self_link.split("/")
    for i, part in enumerate(parts):
        if part == "zones" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def gcp_pricing_url(resource_type: str) -> str:
    """Return the GCP public pricing page URL for a resource type."""
    rt = resource_type.lower()
    urls = {
        "instance": "https://cloud.google.com/compute/vm-instance-pricing",
        "alb": "https://cloud.google.com/vpc/network-pricing#lb",
        "target_group": "https://cloud.google.com/compute/vm-instance-pricing#instancegroups",
        "security_group": "https://cloud.google.com/vpc/network-pricing",
        "eks_cluster": "https://cloud.google.com/kubernetes-engine/pricing",
        "eks_nodegroup": "https://cloud.google.com/kubernetes-engine/pricing",
        "rds": "https://cloud.google.com/sql/pricing",
        "lambda": "https://cloud.google.com/functions/pricing",
        "s3": "https://cloud.google.com/storage/pricing",
    }
    return urls.get(rt, "https://cloud.google.com/pricing")
