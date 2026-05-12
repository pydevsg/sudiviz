"""sudiviz CLI — Typer-based entry points.

Subcommands:
    diagnose   live discovery + analysis (default)
    fix        generate or apply remediation for diagnosed issues
    drift      compare Terraform state against live AWS
    graph      export to PNG or launch web server
    tui        launch the Textual TUI
    watch      continuous monitoring loop
    compare    diff two saved snapshots (BONUS)
    share      upload graph to transfer.sh for a public link (BONUS)
    version    print version + banner
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from sudiviz.discovery.aws import discover_all
from sudiviz.discovery.terraform import detect_drift, load_state, parse_intended_resources
from sudiviz.graph.analyzer import diagnose as run_diagnosis, mark_orphaned_edges
from sudiviz.graph.builder import build_graph
from sudiviz.graph.visualizer import (
    export_cytoscape_json,
    export_png,
    render_diagnosis,
    render_terminal,
    serialize_graph,
)
from sudiviz.utils.branding import VERSION, banner

logger = logging.getLogger("sudiviz")

app = typer.Typer(
    name="sudiviz",
    help="X-ray vision for your cloud infrastructure.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


@app.command()
def diagnose(
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id", help="Filter discovery to one VPC"),
    service_tag: Optional[str] = typer.Option(
        None, "--service-tag", help="Tag filter, e.g. 'Service=checkout' or 'k=v,k2=v2'"
    ),
    profile: Optional[str] = typer.Option(None, "--profile", help="AWS profile to use"),
    region: Optional[str] = typer.Option(None, "--region", help="AWS region override"),
    show_unattached: bool = typer.Option(
        False, "--show-unattached", help="Include orphan resources in output"
    ),
    highlight_orphans: bool = typer.Option(
        False, "--highlight-orphans", help="Render orphans with red dashed lines"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON for CI/CD"),
    speak: bool = typer.Option(False, "--speak", help="(macOS) speak the diagnosis aloud via `say`"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run live discovery + analysis and print a colored topology + fixes."""
    _setup_logging(verbose)
    discovery = asyncio.run(
        discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile, region=region)
    )
    graph = build_graph(discovery)
    if highlight_orphans or show_unattached:
        graph = mark_orphaned_edges(graph)
    diag = run_diagnosis(graph)

    if json_output:
        payload = {
            "graph": json.loads(serialize_graph(graph)),
            "diagnosis": diag.to_dict(),
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        # Also exit non-zero if any critical fix exists, so CI fails loudly.
        if any(f.severity == "critical" for f in diag.fixes):
            raise typer.Exit(2)
        return

    console.print(banner())
    render_terminal(graph, console)
    render_diagnosis(diag, console)

    if speak:
        _speak_diagnosis(diag)

    if any(f.severity == "critical" for f in diag.fixes):
        raise typer.Exit(2)


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


@app.command()
def fix(
    fix_numbers: Optional[str] = typer.Argument(
        None,
        help="Fix number(s) to apply, e.g. '1' or '1,3' or '1-3'. If omitted, shows all fixes.",
    ),
    apply: bool = typer.Option(False, "--apply", help="Actually apply the fixes (default: dry-run)"),
    force: bool = typer.Option(False, "--force", help="Apply destructive fixes (delete operations)"),
    issue: Optional[str] = typer.Option(None, "--issue", help="Only fix issues matching this string"),
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id", help="Filter discovery to one VPC"),
    service_tag: Optional[str] = typer.Option(None, "--service-tag", help="Tag filter"),
    profile: Optional[str] = typer.Option(None, "--profile", help="AWS profile to use"),
    region: Optional[str] = typer.Option(None, "--region", help="AWS region override"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Generate or apply remediation commands for diagnosed issues.

    By default, shows the AWS CLI commands that would fix each issue (dry-run).

    Examples:
        sudiviz fix                  # List all fixes (dry-run)
        sudiviz fix 1 --apply        # Apply fix #1 only
        sudiviz fix 1,3 --apply      # Apply fixes #1 and #3
        sudiviz fix 1-3 --apply      # Apply fixes #1, #2, and #3
        sudiviz fix --apply          # Apply all fixes
        sudiviz fix --apply --force  # Apply all fixes including destructive ones
    """
    _setup_logging(verbose)
    from sudiviz.remediation import generate_fixes, apply_fix

    # Run discovery and diagnosis
    discovery = asyncio.run(
        discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile, region=region)
    )
    graph = mark_orphaned_edges(build_graph(discovery))
    diag = run_diagnosis(graph)

    if not diag.fixes:
        console.print("[green]No issues found — nothing to fix![/green]")
        return

    # Generate remediation actions
    actions = generate_fixes(diag, graph, region=discovery.region)

    # Filter by issue string if specified
    if issue:
        actions = [a for a in actions if issue.lower() in a.fix.title.lower()]
        if not actions:
            console.print(f"[yellow]No issues matching '{issue}'[/yellow]")
            return

    # Parse fix numbers if specified (e.g., "1", "1,3", "1-3")
    selected_indices: set[int] = set()
    if fix_numbers:
        try:
            for part in fix_numbers.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = part.split("-", 1)
                    selected_indices.update(range(int(start), int(end) + 1))
                else:
                    selected_indices.add(int(part))
        except ValueError:
            console.print(f"[red]Invalid fix number format: '{fix_numbers}'[/red]")
            console.print("[dim]Use: 1, 1,3, or 1-3[/dim]")
            raise typer.Exit(1)

        # Validate indices are in range
        max_idx = len(actions)
        invalid = [i for i in selected_indices if i < 1 or i > max_idx]
        if invalid:
            console.print(f"[red]Invalid fix number(s): {invalid}. Valid range: 1-{max_idx}[/red]")
            raise typer.Exit(1)

    if json_output:
        payload = [
            {
                "title": a.fix.title,
                "severity": a.fix.severity,
                "description": a.description,
                "aws_cli_command": a.aws_cli_command,
                "is_destructive": a.is_destructive,
                "applied": a.applied,
                "error": a.error,
            }
            for a in actions
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(banner())

    if not apply:
        # Dry-run mode — show commands
        if selected_indices:
            console.print(f"[bold cyan]Selected fix(es): {sorted(selected_indices)}[/bold cyan]\n")
        else:
            console.print("[bold cyan]Proposed fixes (dry-run):[/bold cyan]\n")

        for i, action in enumerate(actions, 1):
            # If specific fixes selected, only show those
            if selected_indices and i not in selected_indices:
                continue

            severity_style = {
                "critical": "red",
                "warning": "yellow",
                "info": "blue",
            }.get(action.fix.severity, "white")

            destructive_tag = " [red][DESTRUCTIVE][/red]" if action.is_destructive else ""
            console.print(
                f"[bold]{i}. [{severity_style}]{action.fix.severity.upper()}[/] "
                f"{action.fix.title}{destructive_tag}[/bold]"
            )
            console.print(f"   [dim]{action.description}[/dim]\n")
            console.print(f"   [green]{action.aws_cli_command}[/green]\n")

        console.print("[dim]Run with --apply to execute these fixes.[/dim]")
        console.print("[dim]Example: sudiviz fix 1 --apply[/dim]")
        if any(a.is_destructive for a in actions):
            console.print("[dim]Destructive fixes require --apply --force.[/dim]")
        return

    # Apply mode
    if selected_indices:
        console.print(f"[bold cyan]Applying fix(es): {sorted(selected_indices)}[/bold cyan]\n")
    else:
        console.print("[bold cyan]Applying all fixes...[/bold cyan]\n")

    from sudiviz.utils.auth import get_session

    session = get_session(profile=profile, region=region)
    applied_count = 0
    skipped_count = 0
    failed_count = 0

    for i, action in enumerate(actions, 1):
        # If specific fixes selected, skip others
        if selected_indices and i not in selected_indices:
            continue

        severity_style = {
            "critical": "red",
            "warning": "yellow",
            "info": "blue",
        }.get(action.fix.severity, "white")

        # Skip destructive actions unless --force
        if action.is_destructive and not force:
            console.print(
                f"[yellow]⏭ Skipping[/yellow] {i}. {action.fix.title} "
                f"[dim](destructive — use --force)[/dim]"
            )
            skipped_count += 1
            continue

        # Skip if no automated fix available
        if not action._service:
            console.print(
                f"[yellow]⏭ Skipping[/yellow] {i}. {action.fix.title} "
                f"[dim](no automated fix)[/dim]"
            )
            skipped_count += 1
            continue

        console.print(f"[cyan]Applying[/cyan] {i}. [{severity_style}]{action.fix.title}[/]...")

        action = apply_fix(action, session=session, dry_run=False)

        if action.applied:
            console.print(f"  [green]✓ Done:[/green] {action.description}")
            applied_count += 1
        elif action.error:
            console.print(f"  [red]✗ Failed:[/red] {action.error}")
            failed_count += 1

    console.print()
    console.print(
        f"[bold]Summary:[/bold] {applied_count} applied, {skipped_count} skipped, {failed_count} failed"
    )

    if failed_count > 0:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


@app.command()
def drift(
    tfstate: Path = typer.Option(..., "--tfstate", help="Path to `terraform show -json` output"),
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    region: Optional[str] = typer.Option(None, "--region"),
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Compare Terraform state against live AWS and report drift."""
    _setup_logging(verbose)
    if not tfstate.exists():
        typer.echo(f"tfstate file not found: {tfstate}", err=True)
        raise typer.Exit(1)

    intended = parse_intended_resources(load_state(tfstate))
    live = asyncio.run(discover_all(vpc_id=vpc_id, profile=profile, region=region))
    findings = detect_drift(intended, live)

    if json_output:
        typer.echo(json.dumps([f.__dict__ for f in findings], indent=2, default=str))
        if findings:
            raise typer.Exit(1)
        return

    if not findings:
        console.print("[green]No drift detected.[/green]")
        return
    console.print(f"[yellow]{len(findings)} drift item(s):[/yellow]")
    for f in findings:
        kind_style = {
            "missing": "red",
            "orphan_in_aws": "yellow",
            "orphan_listener": "red",
            "attribute_mismatch": "yellow",
        }.get(f.kind, "white")
        console.print(f"  [{kind_style}]{f.kind}[/]: {f.message}")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


@app.command()
def graph(
    output: str = typer.Option("png", "--output", help="png | web | json"),
    file: str = typer.Option("sudiviz.png", "--file", help="Output filename for png/json"),
    open_after: bool = typer.Option(False, "--open", help="Open output after generation"),
    port: int = typer.Option(8000, "--port", help="Port for web mode"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host for web mode"),
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id"),
    service_tag: Optional[str] = typer.Option(None, "--service-tag"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    region: Optional[str] = typer.Option(None, "--region"),
    refresh_interval: float = typer.Option(30.0, "--refresh-interval"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Export the topology as PNG / JSON, or launch the interactive web view."""
    _setup_logging(verbose)
    output_mode = output.lower()

    if output_mode == "web":
        # Lazy import — don't drag FastAPI into every CLI invocation.
        from sudiviz.web import ServerConfig, serve

        cfg = ServerConfig(
            vpc_id=vpc_id,
            service_tag=service_tag,
            profile=profile,
            region=region,
            refresh_interval=refresh_interval,
        )
        url = f"http://{host}:{port}"
        console.print(f"[cyan]sudiviz web[/cyan] running at {url}")
        if open_after:
            webbrowser.open(url)
        serve(cfg, host=host, port=port)
        return

    discovery = asyncio.run(
        discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile, region=region)
    )
    g = mark_orphaned_edges(build_graph(discovery))

    if output_mode == "json":
        payload = export_cytoscape_json(g, region=discovery.region)
        Path(file).write_text(json.dumps(payload, indent=2, default=str))
        console.print(f"Wrote {file}")
        return

    if output_mode == "png":
        out = export_png(g, file)
        console.print(f"Wrote {out}")
        if open_after:
            _open_file(out)
        return

    typer.echo(f"Unknown --output value: {output}", err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# tui
# ---------------------------------------------------------------------------


@app.command()
def tui(
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id"),
    service_tag: Optional[str] = typer.Option(None, "--service-tag"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    region: Optional[str] = typer.Option(None, "--region"),
    refresh_interval: float = typer.Option(30.0, "--refresh-interval"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Launch the Textual TUI with live refresh + mouse support."""
    _setup_logging(verbose)
    from sudiviz.tui import run_tui

    run_tui(
        vpc_id=vpc_id,
        service_tag=service_tag,
        profile=profile,
        region=region,
        refresh_interval=refresh_interval,
    )


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@app.command()
def watch(
    interval: int = typer.Option(30, "--interval", help="Seconds between refreshes"),
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id"),
    service_tag: Optional[str] = typer.Option(None, "--service-tag"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    region: Optional[str] = typer.Option(None, "--region"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Continuous monitoring mode — re-runs discovery every --interval seconds."""
    _setup_logging(verbose)
    console.print(banner())
    while True:
        os.system("clear" if os.name != "nt" else "cls")  # noqa: S605 — single literal
        try:
            discovery = asyncio.run(
                discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile, region=region)
            )
            graph = mark_orphaned_edges(build_graph(discovery))
            render_terminal(graph, console)
            render_diagnosis(run_diagnosis(graph), console)
            console.print(f"[dim]next refresh in {interval}s — Ctrl+C to stop[/dim]")
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]error:[/red] {exc}")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# compare (BONUS)
# ---------------------------------------------------------------------------


@app.command()
def compare(
    baseline: Path = typer.Option(..., "--baseline", help="Path to a saved graph JSON"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Diff a saved graph snapshot against the live topology."""
    if not baseline.exists():
        typer.echo(f"baseline not found: {baseline}", err=True)
        raise typer.Exit(1)

    base = json.loads(baseline.read_text())
    base_nodes = {n["data"]["id"] for n in base.get("nodes", [])}

    discovery = asyncio.run(discover_all())
    g = mark_orphaned_edges(build_graph(discovery))
    current = export_cytoscape_json(g, region=discovery.region)
    current_nodes = {n["data"]["id"] for n in current["nodes"]}

    added = sorted(current_nodes - base_nodes)
    removed = sorted(base_nodes - current_nodes)

    if json_output:
        typer.echo(json.dumps({"added": added, "removed": removed}, indent=2))
        return
    console.print(f"[green]+ {len(added)}[/] new, [red]- {len(removed)}[/] removed")
    for n in added:
        console.print(f"  [green]+[/] {n}")
    for n in removed:
        console.print(f"  [red]-[/] {n}")


# ---------------------------------------------------------------------------
# share (BONUS — uses transfer.sh, no auth required)
# ---------------------------------------------------------------------------


@app.command()
def share(
    upload: bool = typer.Option(True, "--upload/--no-upload"),
    vpc_id: Optional[str] = typer.Option(None, "--vpc-id"),
    service_tag: Optional[str] = typer.Option(None, "--service-tag"),
    profile: Optional[str] = typer.Option(None, "--profile"),
) -> None:
    """Generate a JSON snapshot and (optionally) upload to transfer.sh for a public link."""
    import tempfile

    discovery = asyncio.run(discover_all(vpc_id=vpc_id, service_tag=service_tag, profile=profile))
    g = mark_orphaned_edges(build_graph(discovery))
    payload = export_cytoscape_json(g, region=discovery.region)
    tmp = Path(tempfile.gettempdir()) / "sudiviz-share.json"
    tmp.write_text(json.dumps(payload, indent=2, default=str))

    if not upload:
        console.print(f"Snapshot saved to {tmp}")
        return

    try:
        import urllib.request

        req = urllib.request.Request(  # noqa: S310 — controlled URL
            f"https://transfer.sh/{tmp.name}",
            data=tmp.read_bytes(),
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            url = resp.read().decode().strip()
        console.print(f"[green]Shared at:[/green] {url}")
        console.print("[dim]Link is publicly accessible; treat as ephemeral.[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Upload failed:[/red] {exc}")
        console.print(f"Local snapshot still available at {tmp}")


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print version and banner."""
    console.print(banner())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_file(path: Path) -> None:
    """Cross-platform `open` helper."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)  # noqa: S603, S607
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined] # noqa: S606
        else:
            subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603, S607
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open %s: %s", path, exc)


def _speak_diagnosis(diag) -> None:
    """macOS-only voice mode: read top fixes via the `say` command."""
    if sys.platform != "darwin":
        console.print("[yellow]--speak only works on macOS.[/yellow]")
        return
    summary = (
        f"Sudiviz found {len(diag.fixes)} issues. "
        + " ".join(f.title for f in diag.fixes[:3])
    )
    try:
        subprocess.run(["say", summary], check=False, timeout=30)  # noqa: S603, S607
    except Exception as exc:  # noqa: BLE001
        logger.warning("`say` failed: %s", exc)


if __name__ == "__main__":
    app()
