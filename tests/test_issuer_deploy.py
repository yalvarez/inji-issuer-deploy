"""
Tests for inji-issuer-deploy.

Run with:  python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from inji_issuer_deploy.cloud import CloudProviderConfig
from inji_issuer_deploy.phases import aws_infra, infra, k8s_deploy
from inji_issuer_deploy.state import (
    DeployState, IssuerConfig, load_state, save_state,
    reset_state, STATE_FILE_ENV,
)
from inji_issuer_deploy.phases import config_gen


# ── fixtures ──────────────────────────────────────────────────

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Isolate state file to a temp directory per test."""
    state_file = str(tmp_path / "test-state.json")
    monkeypatch.setenv(STATE_FILE_ENV, state_file)
    return state_file


@pytest.fixture
def full_config() -> IssuerConfig:
    cfg = IssuerConfig(
        issuer_id="mtc",
        issuer_name="Ministerio de Transportes y Comunicaciones",
        issuer_description="Licencias de conducir emitidas por el MTC",
        issuer_logo_url="https://assets.gob.pe/mtc-logo.png",
        base_domain="certify.mtc.gob.pe",
        aws_region="sa-east-1",
        aws_account_id="123456789012",
        eks_cluster_name="INJI-prod",
        rds_host="inji-prod.xxxx.sa-east-1.rds.amazonaws.com",
        rds_port=5432,
        rds_admin_secret_arn="arn:aws:secretsmanager:sa-east-1:123456789012:secret:rds-admin",
        idperu_jwks_uri="https://idperu.gob.pe/v1/idperu/oauth/.well-known/jwks.json",
        idperu_issuer_uri="https://idperu.gob.pe/v1/idperu",
        data_api_base_url="https://api.licencias.mtc.gob.pe",
        data_api_auth_type="mtls",
        data_api_secret_arn="arn:aws:secretsmanager:sa-east-1:123456789012:secret:mtc-api",
        document_number_claim="individualId",
        filiation_claim="",
        scope_mappings=[
            {"scope": "licencia-conducir-a", "profile": "LICENCIA_A",
             "service": "ws-licencias", "display_name": "Licencia Clase A",
             "requires_filiation": False},
            {"scope": "licencia-conducir-b", "profile": "LICENCIA_B",
             "service": "ws-licencias", "display_name": "Licencia Clase B",
             "requires_filiation": False},
        ],
        mimoto_issuers_s3_bucket="inji-config-prod",
        mimoto_issuers_s3_key="mimoto-issuers-config.json",
        chart_version="0.12.2",
        softhsm_chart_version="1.3.0-beta.2",
    )
    return cfg


# ── state tests ───────────────────────────────────────────────

class TestState:
    def test_new_state_has_all_phases(self, tmp_state):
        state = load_state()
        assert set(state.phases.keys()) == {
            "collect", "infra", "config_gen", "k8s_deploy", "register"
        }

    def test_legacy_aws_infra_alias_maps_to_infra(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra")
        save_state(state)

        loaded = load_state()
        assert loaded.is_done("infra")
        assert loaded.is_done("aws_infra")

    def test_round_trip_save_load(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        state.mark_started("collect")
        state.mark_done("collect", {"foo": "bar"})
        save_state(state)

        loaded = load_state()
        assert loaded.issuer.issuer_id == "mtc"
        assert loaded.issuer.base_domain == "certify.mtc.gob.pe"
        assert loaded.is_done("collect")
        assert loaded.phase("collect").outputs == {"foo": "bar"}

    def test_first_incomplete_returns_collect(self, tmp_state):
        state = load_state()
        assert state.first_incomplete() == "collect"

    def test_first_incomplete_skips_done_phases(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        state.mark_done("collect")
        state.mark_done("aws_infra")
        save_state(state)

        loaded = load_state()
        assert loaded.first_incomplete() == "config_gen"

    def test_first_incomplete_returns_none_when_all_done(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        for phase in ["collect", "aws_infra", "config_gen", "k8s_deploy", "register"]:
            state.mark_done(phase)
        save_state(state)

        loaded = load_state()
        assert loaded.first_incomplete() is None

    def test_mark_failed_stores_error(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        state.mark_started("aws_infra")
        state.mark_failed("aws_infra", "Timeout connecting to AWS")
        save_state(state)

        loaded = load_state()
        assert loaded.phase("aws_infra").error == "Timeout connecting to AWS"
        assert not loaded.is_done("aws_infra")

    def test_reset_state(self, tmp_state):
        state = DeployState()
        save_state(state)
        assert Path(tmp_state).exists()
        reset_state()
        assert not Path(tmp_state).exists()


# ── config generation tests ───────────────────────────────────

class TestConfigGen:
    def test_generates_all_files(self, tmp_state, full_config, tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {
            "db_name": "inji_mtc",
            "pod_identity_role_arn": "arn:aws:iam::123456789012:role/inji-mtc-pod-role",
        })

        config_gen.run(state, dry_run=False)

        out_dir = Path(tmp_path) / "mtc"
        assert (out_dir / "certify-mtc.properties").exists()
        assert (out_dir / "helm-values-certify.yaml").exists()
        assert (out_dir / "helm-values-softhsm.yaml").exists()
        assert (out_dir / "k8s-configmap.yaml").exists()
        assert (out_dir / "mimoto-issuer-patch.json").exists()
        assert (out_dir / "db-init-values.yaml").exists()

    def test_certify_properties_contains_idperu_jwks(self, tmp_state, full_config,
                                                       tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=False)

        props = (Path(tmp_path) / "mtc" / "certify-mtc.properties").read_text()
        assert "idperu.gob.pe" in props
        assert "mosip.certify.authn.jwk-set-uri" in props
        assert "individualId" in props

    def test_certify_properties_has_all_scopes(self, tmp_state, full_config,
                                                tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=False)

        props = (Path(tmp_path) / "mtc" / "certify-mtc.properties").read_text()
        assert "licencia-conducir-a" in props
        assert "LICENCIA_A" in props
        assert "licencia-conducir-b" in props
        assert "LICENCIA_B" in props

    def test_mimoto_patch_is_valid_json(self, tmp_state, full_config,
                                        tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=False)

        patch_file = Path(tmp_path) / "mtc" / "mimoto-issuer-patch.json"
        patch = json.loads(patch_file.read_text())
        assert patch["issuer_id"] == "mtc"
        assert patch["enabled"] == "true"
        assert "certify.mtc.gob.pe" in patch["wellknown_endpoint"]
        assert "certify.mtc.gob.pe" in patch["credential_issuer_host"]

    def test_mimoto_patch_has_correct_display(self, tmp_state, full_config,
                                               tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=False)

        patch = json.loads(
            (Path(tmp_path) / "mtc" / "mimoto-issuer-patch.json").read_text()
        )
        assert patch["display"][0]["name"] == "Ministerio de Transportes y Comunicaciones"
        assert patch["display"][0]["language"] == "es"

    def test_helm_values_contains_issuer_id(self, tmp_state, full_config,
                                             tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {
            "db_name": "inji_mtc",
            "pod_identity_role_arn": "arn:aws:iam::123456789012:role/inji-mtc-pod-role",
        })

        config_gen.run(state, dry_run=False)

        values = (Path(tmp_path) / "mtc" / "helm-values-certify.yaml").read_text()
        assert "inji-mtc" in values
        assert "certify.mtc.gob.pe" in values
        assert "0.12.2" in values

    def test_onprem_helm_values_omit_aws_role_annotation(self, tmp_state, full_config,
                                                          tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("infra", {
            "db_name": "inji_mtc",
            "pod_identity_role_arn": "k8s-sa://inji-mtc/inji-mtc-sa",
        })

        config_gen.run(state, dry_run=False)

        values = (Path(tmp_path) / "mtc" / "helm-values-certify.yaml").read_text(encoding="utf-8")
        assert "eks.amazonaws.com/role-arn" not in values

    def test_dry_run_does_not_write_files(self, tmp_state, full_config,
                                          tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=True)

        # Dry run should not write files but creates the directory
        out_dir = Path(tmp_path) / "mtc"
        props = out_dir / "certify-mtc.properties"
        assert not props.exists()

    def test_filiation_claim_included_when_set(self, tmp_state, full_config,
                                               tmp_path, monkeypatch):
        monkeypatch.setattr(config_gen, "OUTPUT_DIR_PREFIX", str(tmp_path))
        full_config.filiation_claim = "relatedPersonId"
        state = DeployState(issuer=full_config)
        state.mark_done("aws_infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})

        config_gen.run(state, dry_run=False)

        props = (Path(tmp_path) / "mtc" / "certify-mtc.properties").read_text()
        assert "relatedPersonId" in props
        assert "filiation-document-number-key" in props


# ── CLI smoke tests ───────────────────────────────────────────

class TestCLI:
    def test_status_empty(self, tmp_state):
        from click.testing import CliRunner
        from inji_issuer_deploy.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--state-file", tmp_state])
        assert result.exit_code == 0
        assert "No deployment" in result.output


class TestInfraPhase:
    def test_write_terraform_tfvars_includes_provider_cfg(self, full_config, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        provider_cfg = CloudProviderConfig(
            provider="aws",
            provisioner="terraform",
            aws_route53_zone_name="mtc.gob.pe",
            aws_manage_acm=True,
        )

        tfvars_path = infra._write_terraform_tfvars(full_config, provider_cfg)
        payload = json.loads(tfvars_path.read_text(encoding="utf-8"))

        assert payload["provider"] == "aws"
        assert payload["provider_cfg"]["aws_route53_zone_name"] == "mtc.gob.pe"
        assert payload["provider_cfg"]["aws_manage_acm"] is True

    def test_terraform_outputs_import_marks_infra_done(self, tmp_state, full_config, monkeypatch, tmp_path):
        state = DeployState(issuer=full_config)
        state.provider_cfg = {"provider": "aws", "provisioner": "terraform"}

        tfvars_path = tmp_path / "terraform.tfvars.json"
        monkeypatch.setattr(infra, "_write_terraform_tfvars", lambda cfg, provider_cfg: tfvars_path)
        monkeypatch.setattr(
            infra,
            "_load_terraform_outputs",
            lambda tf_dir, cfg: {
                "namespace": "inji-mtc",
                "db_name": "inji_mtc",
                "pod_identity_role_arn": "arn:aws:iam::123456789012:role/inji-mtc-pod-role",
            },
        )

        infra.run(state, dry_run=False)

        assert state.is_done("infra")
        assert state.output("infra", "db_name") == "inji_mtc"


class TestK8sDeploy:
    def test_deploy_dry_run_uses_configured_shared_resources(self, tmp_state, full_config):
        full_config.shared_config_source_namespace = "platform-shared"
        full_config.shared_configmaps = ["global-config", "issuer-common"]
        state = DeployState(issuer=full_config)
        state.mark_done("infra", {"db_name": "inji_mtc", "pod_identity_role_arn": ""})
        state.mark_done("config_gen", {
            f"certify-{full_config.issuer_id}.properties": f".inji-deploy/{full_config.issuer_id}/certify-{full_config.issuer_id}.properties"
        })
        save_state(state)

        from click.testing import CliRunner
        from inji_issuer_deploy.cli import main
        runner = CliRunner()
        result = runner.invoke(main, [
            "phase", "deploy", "--dry-run", "--state-file", tmp_state
        ])
        assert result.exit_code == 0
        assert "platform-shared" in result.output
        assert "global-config, issuer-common" in result.output

    def test_copy_configmap_does_not_try_empty_apply(self, monkeypatch):
        def fake_kubectl(*args, check=True):
            if args == ("get", "configmap", "config-server-share", "-n", "dest",):
                return subprocess.CompletedProcess(["kubectl", *args], 1, stdout="", stderr="not found")
            if args == ("get", "configmap", "config-server-share", "-n", "src", "-o", "json"):
                return subprocess.CompletedProcess(
                    ["kubectl", *args],
                    0,
                    stdout=json.dumps({
                        "metadata": {"name": "config-server-share", "namespace": "src"},
                        "data": {"foo": "bar"},
                    }),
                    stderr="",
                )
            raise AssertionError(f"Unexpected kubectl call: {args}")

        monkeypatch.setattr(k8s_deploy, "_kubectl", fake_kubectl)

        def fail_run(*args, **kwargs):
            raise AssertionError("_run should not be called for an empty kubectl apply")

        monkeypatch.setattr(k8s_deploy, "_run", fail_run)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            k8s_deploy._copy_configmap("src", "dest", "config-server-share")


class TestAwsInfra:
    def test_ensure_namespace_fails_cleanly_when_cluster_unreachable(self, monkeypatch):
        def fake_run(cmd, check=True):
            if cmd == ["kubectl", "get", "namespace", "inji-mtc"]:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="Unable to connect to the server: dial tcp [::1]:8080"
                )
            raise AssertionError(f"Unexpected kubectl call: {cmd}")

        monkeypatch.setattr(aws_infra, "_run", fake_run)

        with pytest.raises(RuntimeError, match="kubectl cannot reach the Kubernetes cluster"):
            aws_infra._ensure_namespace("inji-mtc")

    def test_dry_run_does_not_fail(self, tmp_state, full_config):
        """Dry run of collect phase should print plan and exit cleanly."""
        state = DeployState(issuer=full_config)
        state.mark_done("collect")
        state.mark_done("aws_infra", {
            "db_name": "inji_mtc",
            "pod_identity_role_arn": "arn:...",
        })
        save_state(state)

        from click.testing import CliRunner
        from inji_issuer_deploy.cli import main
        runner = CliRunner()
        result = runner.invoke(main, [
            "phase", "config", "--dry-run", "--state-file", tmp_state
        ])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output

    def test_reset_clears_state(self, tmp_state, full_config):
        state = DeployState(issuer=full_config)
        save_state(state)
        assert Path(tmp_state).exists()

        from click.testing import CliRunner
        from inji_issuer_deploy.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["reset", "--state-file", tmp_state], input="y\n")
        assert result.exit_code == 0
        assert not Path(tmp_state).exists()
