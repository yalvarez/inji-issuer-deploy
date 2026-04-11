"""
inji-issuer-deploy — CLI entrypoint.

Usage:
  inji-issuer-deploy run                  # interactive full deployment
  inji-issuer-deploy run --from phase     # resume from a specific phase
  inji-issuer-deploy run --dry-run        # show what would happen without doing it
  inji-issuer-deploy status               # show current deployment state
  inji-issuer-deploy web                  # launch the lightweight web dashboard
  inji-issuer-deploy bootstrap ubuntu-onprem  # prepare an Ubuntu VPS for on-prem installs
  inji-issuer-deploy reset                # clear saved state and start over
  inji-issuer-deploy phase collect        # run only Phase 0
  inji-issuer-deploy phase infra          # run only Phase 1
  inji-issuer-deploy phase aws-infra      # legacy alias for Phase 1
  inji-issuer-deploy phase config         # run only Phase 2
  inji-issuer-deploy phase deploy         # run only Phase 3
  inji-issuer-deploy phase register       # run only Phase 4
"""
from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from inji_issuer_deploy import state as st
from inji_issuer_deploy.bootstrap import bootstrap_ubuntu_onprem
from inji_issuer_deploy.orchestrator import (
    PHASE_LABELS,
    PHASE_ORDER,
    normalize_phase_choice,
    run_phase as _run_phase,
)

console = Console()


def _normalize_phase_choice(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return normalize_phase_choice(name)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


# ── commands ──────────────────────────────────────────────────

@click.group()
def main():
    """Deploy a new Inji VC issuer replicating the RENIEC stack."""
    pass


@main.command()
@click.option("--from", "from_phase", default=None,
              metavar="PHASE",
              help="Resume from a phase: collect, infra, config, deploy, register.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would happen without making any changes.")
@click.option("--state-file", default=None,
              help="Path to the state file (default: inji-deploy-state.json).")
def run(from_phase: str | None, dry_run: bool, state_file: str | None):
    """Run the full deployment pipeline (or resume from a phase)."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file

    if dry_run:
        console.print(Panel(
            "[yellow]DRY RUN MODE — no resources will be created or modified[/yellow]",
            border_style="yellow",
        ))

    state = st.load_state()

    # Determine starting phase
    if from_phase:
        start = _normalize_phase_choice(from_phase)
        console.print(f"[yellow]Resuming from phase: {PHASE_LABELS[start]}[/yellow]")
    elif not dry_run:
        start = state.first_incomplete() or "collect"
        if start != "collect" and state.issuer.issuer_id:
            console.print(
                f"[cyan]Resuming deployment for issuer "
                f"[bold]{state.issuer.issuer_id}[/bold] "
                f"from phase: {PHASE_LABELS[start]}[/cyan]"
            )
    else:
        start = "collect"

    # Run phases in order starting from `start`
    started = False
    for phase in PHASE_ORDER:
        if phase == start:
            started = True
        if not started:
            continue
        # Skip completed phases unless explicitly requested via --from
        if state.is_done(phase) and from_phase != phase:
            console.print(f"  [dim]↷ Phase {PHASE_LABELS[phase]} already complete — skipping[/dim]")
            continue

        console.print()
        try:
            _run_phase(phase, state, dry_run=dry_run)
        except (KeyboardInterrupt, SystemExit):
            console.print("\n[yellow]Interrupted. State saved — re-run to resume.[/yellow]")
            sys.exit(1)
        except Exception as exc:
            console.print(f"\n[red]Phase {PHASE_LABELS[phase]} failed:[/red]\n  {exc}")
            console.print("[yellow]State saved — fix the issue and re-run to resume.[/yellow]")
            sys.exit(1)

    if not dry_run:
        console.print(Panel(
            "[bold green]All phases complete.[/bold green]\n"
            "Run [cyan]inji-issuer-deploy status[/cyan] to see the full summary.",
            border_style="green",
        ))


@main.command()
@click.option("--state-file", default=None)
def status(state_file: str | None):
    """Show the current deployment status."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file

    state = st.load_state()

    if not state.issuer.issuer_id:
        console.print("[dim]No deployment in progress. Run [cyan]inji-issuer-deploy run[/cyan] to start.[/dim]")
        return

    console.print(Panel(
        f"[bold]Issuer: {state.issuer.issuer_id}[/bold]\n"
        f"Domain: {state.issuer.base_domain}\n"
        f"Started: {state.created_at}\n"
        f"Updated: {state.updated_at}",
        title="Deployment status",
        border_style="cyan",
    ))

    t = Table(show_header=True, header_style="bold")
    t.add_column("Phase")
    t.add_column("Status")
    t.add_column("Completed at")
    t.add_column("Error")

    for name in PHASE_ORDER:
        p = state.phase(name)
        if p.completed:
            s = "[green]complete[/green]"
        elif p.error:
            s = "[red]failed[/red]"
        elif p.started_at:
            s = "[yellow]in progress[/yellow]"
        else:
            s = "[dim]pending[/dim]"
        t.add_row(
            PHASE_LABELS[name],
            s,
            p.completed_at or "—",
            p.error[:80] if p.error else "—",
        )
    console.print(t)


@main.command()
@click.option("--state-file", default=None)
@click.confirmation_option(prompt="This will clear all saved state. Continue?")
def reset(state_file: str | None):
    """Clear saved deployment state and start over."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    st.reset_state()
    console.print("[green]State cleared.[/green]")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, type=int, show_default=True)
def web(host: str, port: int):
    """Launch the lightweight web dashboard backed by the CLI engine."""
    try:
        import uvicorn
    except ImportError as exc:
        raise click.ClickException(
            "The web UI needs FastAPI/Uvicorn. Reinstall the project with: pip install -e ."
        ) from exc

    console.print(Panel(
        f"[bold cyan]Starting Inji Issuer Deploy UI[/bold cyan]\n"
        f"Open [link=http://{host}:{port}]http://{host}:{port}[/link] in your browser.\n"
        "The web app reuses the same state file and phase engine as the CLI.",
        border_style="cyan",
    ))
    uvicorn.run("inji_issuer_deploy.webapp:app", host=host, port=port, reload=False)


@main.group()
def bootstrap():
    """Prepare operator environments while keeping the CLI as the deployment engine."""
    pass


@bootstrap.command("ubuntu-onprem")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True)
@click.option("--with-k3s", is_flag=True, default=False, help="Also install k3s for a single-node lab cluster.")
@click.option("--write-script", default=None, help="Write the generated bootstrap script to a file.")
def bootstrap_ubuntu_onprem_command(dry_run: bool, with_k3s: bool, write_script: str | None):
    """Prepare an Ubuntu VPS for on-prem deployment dependencies."""
    bootstrap_ubuntu_onprem(dry_run=dry_run, with_k3s=with_k3s, write_script_path=write_script)


@main.group()
def phase():
    """Run a single deployment phase."""
    pass


@phase.command("collect")
@click.option("--state-file", default=None)
def phase_collect(state_file):
    """Phase 0 — collect issuer configuration."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("collect", state)


@phase.command("infra")
@click.option("--dry-run", is_flag=True)
@click.option("--state-file", default=None)
def phase_infra(dry_run, state_file):
    """Phase 1 — provision infrastructure using the selected provider/engine."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("infra", state, dry_run=dry_run)


@phase.command("aws-infra")
@click.option("--dry-run", is_flag=True)
@click.option("--state-file", default=None)
def phase_aws_alias(dry_run, state_file):
    """Legacy alias for Phase 1 — provision infrastructure."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("infra", state, dry_run=dry_run)


@phase.command("config")
@click.option("--dry-run", is_flag=True)
@click.option("--state-file", default=None)
def phase_config(dry_run, state_file):
    """Phase 2 — generate configuration files."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("config_gen", state, dry_run=dry_run)


@phase.command("deploy")
@click.option("--dry-run", is_flag=True)
@click.option("--state-file", default=None)
def phase_deploy(dry_run, state_file):
    """Phase 3 — deploy to Kubernetes via Helm."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("k8s_deploy", state, dry_run=dry_run)


@phase.command("register")
@click.option("--dry-run", is_flag=True)
@click.option("--state-file", default=None)
def phase_register(dry_run, state_file):
    """Phase 4 — register credentials and run smoke tests."""
    import os
    if state_file:
        os.environ[st.STATE_FILE_ENV] = state_file
    state = st.load_state()
    _run_phase("register", state, dry_run=dry_run)


if __name__ == "__main__":
    main()
