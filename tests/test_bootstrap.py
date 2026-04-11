from pathlib import Path

from click.testing import CliRunner

from inji_issuer_deploy.bootstrap import render_ubuntu_onprem_script
from inji_issuer_deploy.cli import main


def test_render_ubuntu_onprem_script_contains_required_dependencies():
    script = render_ubuntu_onprem_script(with_k3s=False)

    assert "apt-get update" in script
    assert "python3 python3-venv python3-pip git curl jq" in script
    assert "kubectl" in script
    assert "helm repo add mosip https://mosip.github.io/mosip-helm" in script
    assert "k3s" not in script


def test_render_ubuntu_onprem_script_includes_k3s_when_requested():
    script = render_ubuntu_onprem_script(with_k3s=True)

    assert "curl -sfL https://get.k3s.io | sh -" in script


def test_render_ubuntu_onprem_script_uses_preflight_not_infra_phase():
    script = render_ubuntu_onprem_script(with_k3s=True)

    assert "inji-issuer-deploy preflight" in script
    assert "phase infra --dry-run" not in script


def test_cli_bootstrap_dry_run_outputs_plan_and_writes_script(tmp_path):
    runner = CliRunner()
    script_path = tmp_path / "bootstrap-ubuntu-onprem.sh"

    result = runner.invoke(
        main,
        [
            "bootstrap",
            "ubuntu-onprem",
            "--dry-run",
            "--with-k3s",
            "--write-script",
            str(script_path),
        ],
    )

    assert result.exit_code == 0
    assert "Ubuntu on-prem bootstrap plan" in result.output
    assert "kubectl" in result.output
    assert "helm" in result.output
    assert script_path.exists()
    content = script_path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in content
    assert "curl -sfL https://get.k3s.io | sh -" in content
