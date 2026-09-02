"""Resource discovery — live cloud APIs and Terraform state parsing."""
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

__all__ = [
    "CloudProvider",
    "DiscoveryResult",
    "HealthStatus",
    "Instance",
    "Listener",
    "LoadBalancer",
    "SecurityGroup",
    "SecurityGroupRule",
    "Target",
    "TargetGroup",
]

# GCP discovery is optional — only importable when google-cloud SDKs are installed.
try:
    from sudiviz.discovery.gcp import discover_all_gcp  # noqa: F401

    __all__.append("discover_all_gcp")
except ImportError:
    pass
