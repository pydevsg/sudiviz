"""Remediation module — generates and optionally applies fixes for diagnosed issues."""
from sudiviz.remediation.engine import generate_fixes, apply_fix, RemediationAction

__all__ = ["generate_fixes", "apply_fix", "RemediationAction"]