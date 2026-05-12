"""Textual TUI for live in-terminal visualization.

Falls back to a Rich-only renderer if Textual isn't installed — the user
shouldn't have to install Textual just to use sudiviz CLI.

Keyboard shortcuts (per spec):
    r → refresh discovery
    d → toggle drift overlay
    o → toggle orphan-only filter
    q → quit

Mouse: click any node row in the table to populate the details panel.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import networkx as nx
from rich.console import Console
from rich.text import Text

from sudiviz.discovery.aws import discover_all
from sudiviz.discovery.models import HealthStatus
from sudiviz.graph.analyzer import diagnose, mark_orphaned_edges
from sudiviz.graph.builder import build_graph
from sudiviz.graph.visualizer import render_diagnosis, render_terminal
from sudiviz.utils.branding import VERSION, Colors

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional Textual app
# ---------------------------------------------------------------------------

try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widgets import DataTable, Footer, Header, Static

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover — only triggered when extra is missing
    _HAS_TEXTUAL = False


if _HAS_TEXTUAL:

    class SudivizTUI(App):
        """Live, mouse-driven Textual app rendering the topology table."""

        CSS = """
        Screen { background: $surface; }
        #status { dock: top; height: 1; padding: 0 1; background: $accent-darken-2; }
        #legend { dock: bottom; height: 1; padding: 0 1; background: $primary-darken-2; }
        DataTable { height: 1fr; }
        #details { width: 50; border: solid $primary; padding: 1; }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("o", "toggle_orphans", "Toggle orphans"),
            Binding("d", "toggle_drift", "Drift view"),
        ]

        orphan_only: reactive[bool] = reactive(False)

        def __init__(
            self,
            vpc_id: Optional[str] = None,
            service_tag: Optional[str] = None,
            profile: Optional[str] = None,
            region: Optional[str] = None,
            refresh_interval: float = 30.0,
        ) -> None:
            super().__init__()
            self.vpc_id = vpc_id
            self.service_tag = service_tag
            self.profile = profile
            self.region = region
            self.refresh_interval = refresh_interval
            self.graph: Optional[nx.DiGraph] = None
            self.last_refresh: Optional[datetime] = None

        # -- Layout -----------------------------------------------------

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True, name=f"sudiviz v{VERSION}")
            yield Static("loading…", id="status")
            with Horizontal():
                yield DataTable(id="topology", zebra_stripes=True, cursor_type="row")
                with Vertical(id="details"):
                    yield Static("Select a node to inspect.", id="detail-content")
            yield Static(self._legend(), id="legend")
            yield Footer()

        async def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.add_columns("Kind", "Label", "Health", "Orphan", "ID")
            await self.action_refresh()
            self.set_interval(self.refresh_interval, self.action_refresh)

        # -- Actions ----------------------------------------------------

        async def action_refresh(self) -> None:
            self._set_status("Discovering…")
            try:
                discovery = await discover_all(
                    vpc_id=self.vpc_id,
                    service_tag=self.service_tag,
                    profile=self.profile,
                    region=self.region,
                )
                self.graph = mark_orphaned_edges(build_graph(discovery))
                self.last_refresh = datetime.utcnow()
                self._render_table()
                self._set_status(self._status_line(discovery))
            except Exception as exc:  # noqa: BLE001
                logger.exception("refresh failed")
                self._set_status(f"[red]error:[/red] {exc}")

        def action_toggle_orphans(self) -> None:
            self.orphan_only = not self.orphan_only
            self._render_table()

        def action_toggle_drift(self) -> None:
            # Lazy import — drift view doesn't need to load on every TUI start.
            self._set_status("[yellow]Drift view requires `--tfstate` arg; rerun via `sudiviz drift`.[/yellow]")

        # -- Rendering --------------------------------------------------

        def _render_table(self) -> None:
            table = self.query_one(DataTable)
            table.clear()
            if not self.graph:
                return
            rows = [
                (n, a) for n, a in self.graph.nodes(data=True)
                if not self.orphan_only or a.get("orphan")
            ]
            for node_id, attrs in rows:
                health = attrs.get("health", HealthStatus.UNKNOWN.value)
                style = _health_style(health, attrs.get("orphan", False))
                table.add_row(
                    Text(attrs.get("kind", ""), style=style),
                    Text(attrs.get("label", ""), style=style),
                    Text(health, style=style),
                    Text("yes" if attrs.get("orphan") else "no", style=style),
                    Text(node_id, style="dim"),
                    key=node_id,
                )

        @on(DataTable.RowSelected)
        def _row_selected(self, event: "DataTable.RowSelected") -> None:  # type: ignore[name-defined]
            if not self.graph or event.row_key.value is None:
                return
            node_id = str(event.row_key.value)
            if node_id not in self.graph:
                return
            attrs = self.graph.nodes[node_id]
            content = self.query_one("#detail-content", Static)
            content.update(self._format_details(node_id, attrs))

        # -- Helpers ----------------------------------------------------

        def _set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        def _status_line(self, discovery) -> str:
            ts = self.last_refresh.strftime("%H:%M:%S") if self.last_refresh else "—"
            parts = [
                f"[green]●[/green] [b]{discovery.account_id}[/b] {discovery.region} vpc={discovery.vpc_id or 'all'}",
                f"{len(discovery.load_balancers)} ALBs",
                f"{len(discovery.target_groups)} TGs",
                f"{len(discovery.instances)} EC2",
            ]
            if discovery.ecs_clusters:
                svc_total = sum(len(c.services) for c in discovery.ecs_clusters)
                parts.append(f"{len(discovery.ecs_clusters)} ECS clusters ({svc_total} svcs)")
            if discovery.eks_clusters:
                parts.append(f"{len(discovery.eks_clusters)} EKS clusters")
            if discovery.rds_instances:
                parts.append(f"{len(discovery.rds_instances)} RDS")
            if discovery.lambda_functions:
                parts.append(f"{len(discovery.lambda_functions)} Lambda")
            if discovery.s3_buckets:
                parts.append(f"{len(discovery.s3_buckets)} S3")
            parts.append(f"refreshed {ts}")
            return " · ".join(parts)

        def _legend(self) -> str:
            return (
                "[green]●[/green] healthy  "
                "[yellow]●[/yellow] warning  "
                "[red]●[/red] unhealthy  "
                "[grey50]●[/grey50] unknown  "
                "[bold red]╌╌▶[/bold red] orphan  "
                "│ keys: r=refresh  o=orphans  d=drift  q=quit"
            )

        def _format_details(self, node_id: str, attrs: dict) -> str:
            kind = attrs.get("kind", "")
            md = attrs.get("metadata") or {}
            lines = [
                f"[b]{attrs.get('label', node_id)}[/b]",
                f"kind:    {kind}",
                f"health:  {attrs.get('health')}",
                f"orphan:  {'[red]YES[/red]' if attrs.get('orphan') else 'no'}",
                f"id:      [dim]{node_id[:60]}[/dim]",
            ]
            if kind == "alb":
                lines += [f"dns:     {md.get('dns_name','—')}", f"scheme:  {md.get('scheme','—')}", f"state:   {md.get('state','—')}"]
            elif kind == "target_group":
                lines += [f"proto:   {md.get('protocol','—')}:{md.get('port','—')}", f"targets: {attrs.get('healthy_count',0)}/{attrs.get('total_count',0)} healthy"]
            elif kind == "instance":
                lines += [f"type:    {md.get('instance_type','—')}", f"state:   {md.get('state','—')}", f"priv IP: {md.get('private_ip','—')}"]
            elif kind == "ecs_cluster":
                lines += [f"status:  {md.get('status','—')}", f"running: {md.get('running_tasks_count',0)} tasks", f"active:  {md.get('active_services_count',0)} services"]
            elif kind == "ecs_service":
                lines += [f"status:  {md.get('status','—')}", f"running: {md.get('running_count',0)}/{md.get('desired_count',0)}", f"launch:  {md.get('launch_type','—')}"]
            elif kind == "eks_cluster":
                lines += [f"status:  {md.get('status','—')}", f"version: {md.get('version','—')}", f"vpc:     {md.get('vpc_id','—')}"]
            elif kind == "eks_nodegroup":
                lines += [f"status:  {md.get('status','—')}", f"nodes:   {md.get('desired_size',0)} desired", f"type:    {','.join(md.get('instance_types') or []) or '—'}"]
            elif kind == "rds":
                lines += [f"engine:  {md.get('engine','—')} {md.get('engine_version','')}",
                          f"status:  {md.get('status','—')}",
                          f"endpoint:{md.get('endpoint_address','—')}",
                          f"encrypted: {'yes' if md.get('storage_encrypted') else '[red]NO[/red]'}",
                          f"public:  {'[yellow]yes[/yellow]' if md.get('publicly_accessible') else 'no'}"]
            elif kind == "lambda":
                lines += [f"runtime: {md.get('runtime','—')}", f"state:   {md.get('state','—')}", f"memory:  {md.get('memory_size',0)} MB", f"vpc:     {md.get('vpc_id') or 'none (public)'}"]
            elif kind == "s3":
                lines += [f"region:  {md.get('region','—')}",
                          f"versioning: {'yes' if md.get('versioning_enabled') else 'no'}",
                          f"public blocked: {'yes' if md.get('public_access_blocked') else '[red]NO[/red]'}",
                          f"encrypted: {'yes' if md.get('encryption_enabled') else '[red]NO[/red]'}"]
            return "\n".join(lines)


def _health_style(health: str, is_orphan: bool) -> str:
    if is_orphan:
        return Colors.RICH_ORPHAN
    return {
        HealthStatus.HEALTHY.value: Colors.RICH_HEALTHY,
        HealthStatus.UNHEALTHY.value: Colors.RICH_UNHEALTHY,
        HealthStatus.INITIAL.value: Colors.RICH_WARNING,
        HealthStatus.DRAINING.value: Colors.RICH_WARNING,
        HealthStatus.UNUSED.value: Colors.RICH_UNREACHABLE,
        HealthStatus.UNKNOWN.value: Colors.RICH_UNREACHABLE,
    }.get(health, "white")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_tui(
    vpc_id: Optional[str] = None,
    service_tag: Optional[str] = None,
    profile: Optional[str] = None,
    region: Optional[str] = None,
    refresh_interval: float = 30.0,
) -> None:
    """Launch the Textual TUI, or fall back to a one-shot Rich render."""
    if _HAS_TEXTUAL:
        SudivizTUI(
            vpc_id=vpc_id,
            service_tag=service_tag,
            profile=profile,
            region=region,
            refresh_interval=refresh_interval,
        ).run()
        return

    # Graceful fallback — a single non-interactive render using Rich.
    console = Console()
    console.print("[yellow]Textual not installed; falling back to single-shot Rich render.[/yellow]")
    discovery = asyncio.run(discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile, region=region))
    graph = mark_orphaned_edges(build_graph(discovery))
    render_terminal(graph, console)
    render_diagnosis(diagnose(graph), console)
