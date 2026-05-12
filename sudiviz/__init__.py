"""sudiviz — sufficient visibility into cloud infrastructure failures.

A Python + Terraform CLI for diagnosing connectivity failures (503s, unhealthy
targets, drift) across multi-cloud environments. AWS first; Azure/GCP planned.
"""
from sudiviz.utils.branding import VERSION

__version__ = VERSION
__all__ = ["__version__"]
