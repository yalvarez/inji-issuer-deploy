"""
Tests for cloud provider abstraction layer.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from inji_issuer_deploy.cloud import (
    CloudProviderConfig,
    check_and_explain,
    _aws_no_creds_help,
    _azure_no_creds_help,
    _gcp_no_creds_help,
    get_provider,
)
from inji_issuer_deploy.state import DeployState, IssuerConfig, save_state, load_state, STATE_FILE_ENV


# ── fixtures ──────────────────────────────────────────────────

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    state_file = str(tmp_path / "test-state.json")
    monkeypatch.setenv(STATE_FILE_ENV, state_file)
    return state_file


@pytest.fixture
def issuer_cfg() -> IssuerConfig:
    return IssuerConfig(
        issuer_id="mtc",
        issuer_name="MTC",
        base_domain="certify.mtc.gob.pe",
        aws_region="sa-east-1",
        aws_account_id="123456789012",
        eks_cluster_name="INJI-prod",
        rds_host="inji-prod.rds.amazonaws.com",
        idperu_jwks_uri="https://idperu.gob.pe/jwks.json",
        idperu_issuer_uri="https://idperu.gob.pe/v1/idperu",
        data_api_base_url="https://api.mtc.gob.pe",
        data_api_auth_type="mtls",
        scope_mappings=[{"scope": "licencia-b", "profile": "LICENCIA_B",
                         "service": "ws-lic", "display_name": "Licencia B",
                         "requires_filiation": False}],
        mimoto_issuers_s3_bucket="inji-config",
    )


# ── CloudProviderConfig round-trip ────────────────────────────

class TestCloudProviderConfig:
    def test_default_provider_is_empty(self):
        pc = CloudProviderConfig()
        assert pc.provider == ""

    def test_serialises_to_dict(self):
        pc = CloudProviderConfig(provider="aws", aws_profile="my-profile")
        d = asdict(pc)
        assert d["provider"] == "aws"
        assert d["aws_profile"] == "my-profile"

    def test_round_trip_via_state(self, tmp_state, issuer_cfg):
        state = DeployState(issuer=issuer_cfg)
        pc = CloudProviderConfig(provider="gcp", gcp_project_id="my-project")
        state.provider_cfg = asdict(pc)
        save_state(state)

        loaded = load_state()
        assert loaded.provider_cfg["provider"] == "gcp"
        assert loaded.provider_cfg["gcp_project_id"] == "my-project"

    def test_provider_cfg_preserved_across_phases(self, tmp_state, issuer_cfg):
        state = DeployState(issuer=issuer_cfg)
        state.provider_cfg = asdict(CloudProviderConfig(provider="onprem",
                                                        onprem_secrets_backend="vault"))
        state.mark_done("collect")
        save_state(state)

        loaded = load_state()
        assert loaded.provider_cfg["onprem_secrets_backend"] == "vault"
        assert loaded.is_done("collect")


# ── AWS credential check ──────────────────────────────────────

class TestAWSCredentials:
    def test_no_credentials_returns_false(self, monkeypatch):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)

        with patch("boto3.Session") as mock_session:
            mock_session.return_value.client.return_value.get_caller_identity.side_effect = \
                Exception("No credentials")
            pc = CloudProviderConfig(provider="aws")
            ok, msg = check_and_explain(pc)
            assert not ok
            assert "AWS" in msg.upper() or "credential" in msg.lower()

    def test_env_var_credentials_detected(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secrettest")

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/deploy",
        }
        # _check_aws calls boto3.client("sts") in the default chain path
        with patch("boto3.client", return_value=mock_sts):
            pc = CloudProviderConfig(provider="aws")
            ok, msg = check_and_explain(pc)
            assert ok
            assert "123456789012" in msg
            assert pc.aws_auth_method == "env"

    def test_named_profile_detected(self, monkeypatch):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

        with patch("boto3.Session") as mock_session:
            mock_sts = MagicMock()
            mock_sts.get_caller_identity.return_value = {
                "Account": "999888777666",
                "Arn": "arn:aws:iam::999888777666:user/ops",
            }
            mock_session.return_value.client.return_value = mock_sts
            pc = CloudProviderConfig(provider="aws", aws_profile="my-profile")
            ok, msg = check_and_explain(pc)
            assert ok
            assert "999888777666" in msg
            assert pc.aws_auth_method == "profile"

    def test_no_creds_help_contains_instructions(self):
        msg = _aws_no_creds_help()
        assert "AWS_ACCESS_KEY_ID" in msg
        assert "aws configure" in msg
        assert "aws sso login" in msg

    def test_pod_identity_detected(self, monkeypatch):
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/token")
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/pod-role/session",
        }
        with patch("boto3.client", return_value=mock_sts):
            pc = CloudProviderConfig(provider="aws")
            ok, msg = check_and_explain(pc)
            assert ok
            assert pc.aws_auth_method == "pod_identity"


# ── GCP credential check ──────────────────────────────────────

class TestGCPCredentials:
    def test_no_sdk_returns_false(self, monkeypatch):
        import sys
        with patch.dict(sys.modules, {"google.auth": None,
                                       "google.auth.exceptions": None}):
            pc = CloudProviderConfig(provider="gcp")
            ok, msg = check_and_explain(pc)
            assert not ok

    def test_no_creds_help_contains_gcloud(self):
        msg = _gcp_no_creds_help()
        assert "gcloud" in msg
        assert "GOOGLE_APPLICATION_CREDENTIALS" in msg

    def test_service_account_key_detected(self, monkeypatch, tmp_path):
        pytest.importorskip("google.auth", reason="google-auth not installed")
        fake_key = tmp_path / "key.json"
        fake_key.write_text('{"type": "service_account"}')
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_key))

        import google.auth
        with patch.object(google.auth, "default", return_value=(MagicMock(), "my-project")):
            pc = CloudProviderConfig(provider="gcp", gcp_project_id="my-project")
            ok, msg = check_and_explain(pc)
            assert ok
            assert pc.gcp_auth_method == "service_account_key"


# ── Azure credential check ────────────────────────────────────

class TestAzureCredentials:
    def test_no_sdk_returns_false(self):
        import sys
        with patch.dict(sys.modules, {"azure.identity": None,
                                       "azure.mgmt.resource": None}):
            pc = CloudProviderConfig(provider="azure")
            ok, msg = check_and_explain(pc)
            assert not ok

    def test_no_creds_help_contains_az_login(self):
        msg = _azure_no_creds_help()
        assert "az login" in msg
        assert "AZURE_CLIENT_ID" in msg


# ── On-premise credential check ───────────────────────────────

class TestOnPremCredentials:
    def test_no_kubectl_returns_false(self, monkeypatch):
        with patch("shutil.which", return_value=None):
            pc = CloudProviderConfig(provider="onprem")
            ok, msg = check_and_explain(pc)
            assert not ok
            assert "kubectl" in msg

    def test_kubectl_available_but_cluster_unreachable(self, monkeypatch):
        def mock_which(cmd):
            return "/usr/bin/" + cmd if cmd in ("kubectl", "helm") else None

        with patch("shutil.which", side_effect=mock_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="refused")
                pc = CloudProviderConfig(provider="onprem")
                ok, msg = check_and_explain(pc)
                assert not ok
                assert "cluster" in msg.lower()

    def test_kubectl_and_helm_available(self, monkeypatch):
        def mock_which(cmd):
            return "/usr/bin/" + cmd if cmd in ("kubectl", "helm") else None

        with patch("shutil.which", side_effect=mock_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="Kubernetes control plane", stderr="")
                pc = CloudProviderConfig(provider="onprem")
                ok, msg = check_and_explain(pc)
                assert ok
                assert "kubectl" in msg
                assert "helm" in msg

    def test_vault_token_missing_when_configured(self, monkeypatch):
        monkeypatch.delenv("VAULT_TOKEN", raising=False)

        def mock_which(cmd):
            return "/usr/bin/" + cmd if cmd in ("kubectl", "helm") else None

        with patch("shutil.which", side_effect=mock_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="Kubernetes control plane")
                pc = CloudProviderConfig(
                    provider="onprem",
                    onprem_vault_addr="https://vault.internal",
                    onprem_vault_token_env="VAULT_TOKEN",
                )
                ok, msg = check_and_explain(pc)
                assert not ok
                assert "VAULT_TOKEN" in msg


# ── Provider factory ──────────────────────────────────────────

class TestProviderFactory:
    def test_aws_provider_returned(self, issuer_cfg):
        from inji_issuer_deploy.providers.aws import AWSProvider
        pc = CloudProviderConfig(provider="aws")
        with patch("boto3.Session"):
            p = get_provider(pc, issuer_cfg)
            assert isinstance(p, AWSProvider)
            assert p.name() == "aws"

    def test_azure_provider_returned(self, issuer_cfg):
        from inji_issuer_deploy.providers.azure import AzureProvider
        pc = CloudProviderConfig(provider="azure")
        p = get_provider(pc, issuer_cfg)
        assert isinstance(p, AzureProvider)
        assert p.name() == "azure"

    def test_gcp_provider_returned(self, issuer_cfg):
        from inji_issuer_deploy.providers.gcp import GCPProvider
        pc = CloudProviderConfig(provider="gcp")
        p = get_provider(pc, issuer_cfg)
        assert isinstance(p, GCPProvider)
        assert p.name() == "gcp"

    def test_onprem_provider_returned(self, issuer_cfg):
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        pc = CloudProviderConfig(provider="onprem")
        p = get_provider(pc, issuer_cfg)
        assert isinstance(p, OnPremProvider)
        assert p.name() == "onprem"

    def test_unknown_provider_raises(self, issuer_cfg):
        pc = CloudProviderConfig(provider="unknown-cloud")
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider(pc, issuer_cfg)


# ── AWS Provider — dry run plan ───────────────────────────────

class TestAWSProviderDryRun:
    def test_dry_run_lists_expected_resources(self, issuer_cfg):
        from inji_issuer_deploy.providers.aws import AWSProvider
        pc = CloudProviderConfig(provider="aws")
        with patch("boto3.Session"):
            p = AWSProvider(pc, issuer_cfg)
            plan = p.dry_run_plan("mtc", issuer_cfg)
        resource_types = [r[0] for r in plan]
        assert "EKS namespace" in resource_types
        assert "IAM role" in resource_types
        assert "ACM certificate" in resource_types
        resource_names = [r[1] for r in plan]
        assert any("inji-mtc" in n for n in resource_names)
        assert any("mtc/inji-certify" in n for n in resource_names)

    def test_dry_run_uses_issuer_id(self, issuer_cfg):
        from inji_issuer_deploy.providers.aws import AWSProvider
        pc = CloudProviderConfig(provider="aws")
        with patch("boto3.Session"):
            p = AWSProvider(pc, issuer_cfg)
            plan = p.dry_run_plan("mtc", issuer_cfg)
        all_text = " ".join(f"{t} {n}" for t, n in plan)
        assert "mtc" in all_text


# ── On-premise Provider — dry run plan ───────────────────────

class TestOnPremProviderDryRun:
    def test_dry_run_shows_harbor(self, issuer_cfg):
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        pc = CloudProviderConfig(
            provider="onprem",
            onprem_harbor_url="https://harbor.internal",
            onprem_harbor_project="inji-mtc",
        )
        p = OnPremProvider(pc, issuer_cfg)
        plan = p.dry_run_plan("mtc", issuer_cfg)
        resource_types = [r[0] for r in plan]
        assert "Registry repo" in resource_types
        resource_names = " ".join(r[1] for r in plan)
        assert "harbor.internal" in resource_names

    def test_dry_run_shows_vault_when_configured(self, issuer_cfg):
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        pc = CloudProviderConfig(
            provider="onprem",
            onprem_secrets_backend="vault",
            onprem_vault_addr="https://vault.internal",
        )
        p = OnPremProvider(pc, issuer_cfg)
        plan = p.dry_run_plan("mtc", issuer_cfg)
        resource_types = " ".join(r[0] for r in plan)
        assert "vault" in resource_types.lower()

    def test_dry_run_shows_minio_when_configured(self, issuer_cfg):
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        pc = CloudProviderConfig(
            provider="onprem",
            onprem_minio_endpoint="https://minio.internal",
            onprem_minio_bucket="inji-config",
        )
        p = OnPremProvider(pc, issuer_cfg)
        plan = p.dry_run_plan("mtc", issuer_cfg)
        resource_types = " ".join(r[0] for r in plan)
        assert "MinIO" in resource_types

    def test_dry_run_shows_configmap_without_minio(self, issuer_cfg):
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        pc = CloudProviderConfig(provider="onprem")
        p = OnPremProvider(pc, issuer_cfg)
        plan = p.dry_run_plan("mtc", issuer_cfg)
        resource_types = " ".join(r[0] for r in plan)
        assert "ConfigMap" in resource_types
