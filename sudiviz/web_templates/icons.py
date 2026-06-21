"""Resolve real AWS architecture icons to data URIs.

Two URI formats are produced per icon:
- cytoscape URI  → data:image/svg+xml;charset=utf-8,<percent-encoded-svg>
  Cytoscape.js background-image only accepts percent-encoded SVG or PNG base64.
- img URI        → data:image/svg+xml;base64,... (or PNG base64)
  Used in <img> tags (legend). Base64 is fine there.

Adding a new service: add one entry to KIND_ICON_PATHS below.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_ICON_BASE = _REPO_ROOT / "docs" / "Icons"

_ARCH_PREFIX = "Architecture-Service-Icons_04302026"
_GROUP_PREFIX = "Architecture-Group-Icons_04302026"
_ARCH = _ARCH_PREFIX

KIND_ICON_PATHS: dict[str, tuple[str, str]] = {
    "alb":           (_ARCH + "/Arch_Networking-Content-Delivery/32", "Arch_Elastic-Load-Balancing_32"),
    "target_group":  (_ARCH + "/Arch_Networking-Content-Delivery/32", "Arch_Elastic-Load-Balancing_32"),
    "instance":      (_ARCH + "/Arch_Compute/32",                     "Arch_Amazon-EC2_32"),
    "security_group":(_ARCH + "/Arch_Security-Identity/32",           "Arch_AWS-Shield_32"),
    "vpc":           (_GROUP_PREFIX,                                   "Virtual-private-cloud-VPC_32"),
    "ecs_cluster":   (_ARCH + "/Arch_Containers/32",                  "Arch_Amazon-Elastic-Container-Service_32"),
    "ecs_service":   (_ARCH + "/Arch_Containers/32",                  "Arch_Amazon-Elastic-Container-Service_32"),
    "eks_cluster":   (_ARCH + "/Arch_Containers/32",                  "Arch_Amazon-Elastic-Kubernetes-Service_32"),
    "eks_nodegroup": (_ARCH + "/Arch_Containers/32",                  "Arch_Amazon-Elastic-Kubernetes-Service_32"),
    "rds":           (_ARCH + "/Arch_Databases/32",                   "Arch_Amazon-RDS_32"),
    "aurora":        (_ARCH + "/Arch_Databases/32",                   "Arch_Amazon-Aurora_32"),
    "lambda":        (_ARCH + "/Arch_Compute/32",                     "Arch_AWS-Lambda_32"),
    "s3":            (_ARCH + "/Arch_Storage/32",                     "Arch_Amazon-Simple-Storage-Service_32"),
}


class _IconURIs:
    __slots__ = ("cytoscape", "img")

    def __init__(self, cytoscape: str, img: str) -> None:
        self.cytoscape = cytoscape  # percent-encoded SVG or base64 PNG — for Cytoscape background-image
        self.img = img              # base64 — for <img> src


def _read_icon(subdir: str, stem: str) -> Optional[_IconURIs]:
    base = _ICON_BASE / subdir
    svg_path = base / (stem + ".svg")
    png_path = base / (stem + ".png")

    if svg_path.exists():
        svg_text = svg_path.read_text(encoding="utf-8")
        # Cytoscape: percent-encode the raw SVG text
        cytoscape_uri = "data:image/svg+xml;charset=utf-8," + quote(svg_text, safe="")
        # Legend <img>: base64
        img_uri = "data:image/svg+xml;base64," + base64.b64encode(svg_text.encode()).decode()
        return _IconURIs(cytoscape=cytoscape_uri, img=img_uri)

    if png_path.exists():
        b64 = base64.b64encode(png_path.read_bytes()).decode()
        uri = f"data:image/png;base64,{b64}"
        # Cytoscape accepts base64 PNG fine
        return _IconURIs(cytoscape=uri, img=uri)

    return None


_cache: dict[str, _IconURIs] = {}


def _build_cache() -> None:
    if not _ICON_BASE.exists():
        logger.debug("AWS icon directory not found at %s; using placeholder SVGs", _ICON_BASE)
        return
    for kind, (subdir, stem) in KIND_ICON_PATHS.items():
        result = _read_icon(subdir, stem)
        if result:
            _cache[kind] = result
        else:
            logger.debug("Icon not found for kind=%s (%s/%s)", kind, subdir, stem)


_build_cache()


def get_cytoscape_uri(kind: str) -> Optional[str]:
    """Percent-encoded SVG URI for Cytoscape.js background-image."""
    entry = _cache.get(kind)
    return entry.cytoscape if entry else None


def get_img_uri(kind: str) -> Optional[str]:
    """Base64 URI for <img> src (legend)."""
    entry = _cache.get(kind)
    return entry.img if entry else None


def all_cytoscape_uris() -> dict[str, str]:
    return {k: v.cytoscape for k, v in _cache.items()}


def all_img_uris() -> dict[str, str]:
    return {k: v.img for k, v in _cache.items()}
