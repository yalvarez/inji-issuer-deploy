from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from fastapi.testclient import TestClient

from inji_issuer_deploy.cli import main
from inji_issuer_deploy.cloud import CloudProviderConfig, preflight_report
from inji_issuer_deploy.state import DeployState, IssuerConfig, save_state, STATE_FILE_ENV
from inji_issuer_deploy.webapp import create_app


class TestOnPremPreflightReport:
    def test_report_includes_cluster_and_configmap_checks(self):
        cfg = CloudProviderConfig(provider="onprem", onprem_secrets_backend="k8s")
        issuer = IssuerConfig(
            issuer_id="demo",
            base_domain="demo.example.org",
            mimoto_service_namespace="mimoto",
            shared_config_source_namespace="platform-shared",
            shared_configmaps=["global-config", "issuer-common"],
            rds_host="postgres.internal",
        )

        def mock_which(cmd):
            return f"/usr/bin/{cmd}" if cmd in ("kubectl", "helm") else None

        def mock_run(args, capture_output=True, text=True, check=False):
            cmd = tuple(args)
            if cmd == ("kubectl", "config", "current-context"):
                return MagicMock(returncode=0, stdout="demo-context\n", stderr="")
            if cmd == ("kubectl", "cluster-info", "--request-timeout=5s"):
                return MagicMock(returncode=0, stdout="Kubernetes control plane", stderr="")
            if cmd == ("helm", "repo", "list"):
                return MagicMock(returncode=0, stdout="mosip\thttps://mosip.github.io/mosip-helm\n", stderr="")
            if cmd == ("kubectl", "get", "crd", "certificates.cert-manager.io"):
                return MagicMock(returncode=0, stdout="certificates.cert-manager.io", stderr="")
            if cmd == ("kubectl", "get", "clusterissuer", "letsencrypt-prod"):
                return MagicMock(returncode=0, stdout="letsencrypt-prod", stderr="")
            if cmd == ("kubectl", "get", "namespace", "mimoto"):
                return MagicMock(returncode=0, stdout="mimoto Active", stderr="")
            if cmd == ("kubectl", "get", "namespace", "platform-shared"):
                return MagicMock(returncode=0, stdout="platform-shared Active", stderr="")
            if cmd == ("kubectl", "get", "configmap", "global-config", "-n", "platform-shared"):
                return MagicMock(returncode=0, stdout="global-config", stderr="")
            if cmd == ("kubectl", "get", "configmap", "issuer-common", "-n", "platform-shared"):
                return MagicMock(returncode=0, stdout="issuer-common", stderr="")
            raise AssertionError(f"Unexpected command: {args}")

        with patch("shutil.which", side_effect=mock_which), patch("subprocess.run", side_effect=mock_run):
            report = preflight_report(cfg, issuer)

        assert report["ok"] is True
        check_names = [item["name"] for item in report["checks"]]
        assert "kubectl" in check_names
        assert "shared-configmaps" in check_names
        assert any(item["status"] == "ok" for item in report["checks"])


class TestPreflightCLIAndAPI:
    def test_cli_preflight_command_prints_report(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

        state = DeployState()
        state.issuer.issuer_id = "demo"
        state.issuer.shared_config_source_namespace = "platform-shared"
        state.issuer.shared_configmaps = ["global-config"]
        state.provider_cfg = {"provider": "onprem", "provisioner": "python"}
        save_state(state)

        report = {
            "ok": False,
            "provider": "onprem",
            "summary": "1 check requires attention.",
            "checks": [
                {"name": "helm", "label": "Helm", "status": "error", "detail": "helm not found"},
            ],
        }

        with patch("inji_issuer_deploy.cli.preflight_report", return_value=report):
            runner = CliRunner()
            result = runner.invoke(main, ["preflight", "--state-file", str(state_file)])

        assert result.exit_code == 1
        assert "On-prem readiness report" in result.output
        assert "helm not found" in result.output

    def test_web_preflight_endpoint_returns_structured_checks(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
        state = DeployState()
        state.provider_cfg = {"provider": "onprem", "provisioner": "python"}
        save_state(state)

        report = {
            "ok": True,
            "provider": "onprem",
            "summary": "All checks passed.",
            "checks": [
                {"name": "kubectl", "label": "kubectl", "status": "ok", "detail": "cluster reachable"},
            ],
        }

        with patch("inji_issuer_deploy.webapp.preflight_report", return_value=report):
            client = TestClient(create_app())
            response = client.post("/api/preflight", params={"state_file": str(state_file)})

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["checks"][0]["name"] == "kubectl"
