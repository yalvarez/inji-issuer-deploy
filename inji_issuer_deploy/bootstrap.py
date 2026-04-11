"""Bootstrap helpers for preparing an Ubuntu VPS as an on-prem operator host."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def ubuntu_onprem_steps(with_k3s: bool = False) -> list[tuple[str, str]]:
    steps = [
        (
            "Base packages",
            "sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git curl jq ca-certificates apt-transport-https",
        ),
        (
            "kubectl",
            "Install the Kubernetes apt repo, then: sudo apt-get update && sudo apt-get install -y kubectl",
        ),
        (
            "Helm",
            "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash",
        ),
        (
            "MOSIP repo",
            "helm repo add mosip https://mosip.github.io/mosip-helm && helm repo update",
        ),
    ]

    if with_k3s:
        steps.insert(
            3,
            (
                "k3s",
                "curl -sfL https://get.k3s.io | sh - ; sudo chmod 644 /etc/rancher/k3s/k3s.yaml",
            ),
        )
    else:
        steps.append(
            (
                "Cluster access",
                "Copy your kubeconfig to ~/.kube/config and verify kubectl config current-context",
            )
        )

    steps.extend(
        [
            (
                "Python tool",
                "python3 -m venv .venv && . .venv/bin/activate && pip install -e .",
            ),
            (
                "Validation",
                "kubectl cluster-info ; helm version ; INJI_STATE_FILE=/tmp/inji-bootstrap-state.json inji-issuer-deploy preflight || true",
            ),
        ]
    )
    return steps


def render_ubuntu_onprem_script(with_k3s: bool = False) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "export DEBIAN_FRONTEND=noninteractive",
        "sudo apt-get update",
        "sudo apt-get install -y python3 python3-venv python3-pip git curl jq ca-certificates apt-transport-https gnupg",
        "",
        "sudo mkdir -p /etc/apt/keyrings",
        "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg",
        "echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list >/dev/null",
        "sudo apt-get update",
        "sudo apt-get install -y kubectl",
        "",
        "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash",
    ]

    if with_k3s:
        lines.extend(
            [
                "",
                "curl -sfL https://get.k3s.io | sh -",
                "mkdir -p ~/.kube",
                "sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config",
                "sudo chown \"$(id -u)\":\"$(id -g)\" ~/.kube/config",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "mkdir -p ~/.kube",
                "echo '# Copy your cluster kubeconfig to ~/.kube/config before continuing'",
            ]
        )

    lines.extend(
        [
            "",
            "helm repo add mosip https://mosip.github.io/mosip-helm",
            "helm repo update",
            "",
            "python3 -m venv .venv",
            ". .venv/bin/activate",
            "pip install --upgrade pip",
            "pip install -e .",
            "",
            "kubectl config current-context || true",
            "kubectl cluster-info",
            "helm version",
            "BOOTSTRAP_STATE_FILE=/tmp/inji-bootstrap-state.json",
            "rm -f \"$BOOTSTRAP_STATE_FILE\"",
            "INJI_STATE_FILE=\"$BOOTSTRAP_STATE_FILE\" inji-issuer-deploy preflight || true",
            "echo 'Bootstrap validation finished. If preflight reported remaining issues, fix them and re-run: INJI_STATE_FILE=\"$BOOTSTRAP_STATE_FILE\" inji-issuer-deploy preflight'",
            "echo 'Ubuntu on-prem bootstrap complete.'",
            "",
        ]
    )
    return "\n".join(lines)


def write_script(path: str | Path, *, with_k3s: bool = False) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_ubuntu_onprem_script(with_k3s=with_k3s), encoding="utf-8")
    return target


def _print_plan(with_k3s: bool) -> None:
    console.print(Panel(
        "[bold]Ubuntu on-prem bootstrap plan[/bold]\n"
        "Prepare a VPS either as an operator host against an existing cluster or as a single-node [cyan]k3s[/cyan] lab.",
        border_style="cyan",
    ))
    table = Table(show_header=True, header_style="bold")
    table.add_column("Step")
    table.add_column("Action")
    for step, action in ubuntu_onprem_steps(with_k3s=with_k3s):
        table.add_row(step, action)
    console.print(table)


def _run_script(script_text: str) -> None:
    if platform.system().lower() != "linux":
        raise RuntimeError("Ubuntu bootstrap can only be executed from a Linux host. Use --dry-run or --write-script on Windows.")

    release = Path("/etc/os-release")
    if not release.exists() or "ubuntu" not in release.read_text(encoding="utf-8", errors="ignore").lower():
        raise RuntimeError("This bootstrap currently targets Ubuntu hosts. Re-run on Ubuntu or use --write-script for manual review.")

    subprocess.run(["bash", "-lc", script_text], check=True)


def bootstrap_ubuntu_onprem(*, dry_run: bool = True, with_k3s: bool = False, write_script_path: str | None = None) -> str:
    script_text = render_ubuntu_onprem_script(with_k3s=with_k3s)

    if write_script_path:
        target = write_script(write_script_path, with_k3s=with_k3s)
        console.print(f"[green]Bootstrap script written to[/green] [cyan]{target}[/cyan]")

    _print_plan(with_k3s=with_k3s)

    if dry_run:
        console.print("\n[yellow]Dry-run only.[/yellow] Review the plan or generated script, then run it on the Ubuntu VPS.")
        return script_text

    _run_script(script_text)
    console.print("[green]Ubuntu on-prem bootstrap completed.[/green]")
    return script_text
