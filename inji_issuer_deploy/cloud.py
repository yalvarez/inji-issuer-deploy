"""
Cloud provider abstraction layer.

Defines the CloudProvider interface and a credential-verification
step that runs before any cloud operation, with clear human-readable
explanations of what credentials are needed and how to provide them.

Supported providers:
  - aws       Amazon Web Services (boto3, current implementation)
  - azure     Microsoft Azure (azure-sdk)
  - gcp       Google Cloud Platform (google-cloud-*)
  - onprem    On-premise / self-hosted (Vault, Harbor, MinIO, cert-manager)
"""
from __future__ import annotations

import abc
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ── Provider config stored in IssuerConfig ────────────────────

@dataclass
class CloudProviderConfig:
    """
    Provider-specific connection details stored in state.
    Populated during Phase 0 credential verification.
    """
    provider: str = ""          # "aws" | "azure" | "gcp" | "onprem"
    provisioner: str = "python"  # "python" | "terraform"

    # AWS
    aws_profile: str = ""       # named profile from ~/.aws/credentials, or "" for default
    aws_auth_method: str = ""   # "profile" | "env" | "instance_profile" | "pod_identity"
    aws_route53_zone_name: str = ""            # optional hosted zone name for DNS automation
    aws_manage_acm: bool = False                # request/manage ACM cert in Terraform
    aws_existing_acm_certificate_arn: str = "" # reuse an existing ACM cert instead of creating one
    aws_create_dns_record: bool = False         # create a Route53 record when a target is known
    aws_dns_record_name: str = ""              # optional override for the issuer DNS record name
    aws_dns_target_name: str = ""              # ALB/CloudFront DNS target, if already known
    aws_dns_target_zone_id: str = ""           # Hosted zone ID for alias target (optional)

    # Azure
    azure_subscription_id: str = ""
    azure_resource_group: str = ""
    azure_auth_method: str = ""  # "cli" | "service_principal" | "managed_identity"

    # GCP
    gcp_project_id: str = ""
    gcp_auth_method: str = ""   # "adc" | "service_account_key" | "workload_identity"

    # On-premise
    onprem_vault_addr: str = ""          # HashiCorp Vault address (or "" to use K8s secrets)
    onprem_vault_token_env: str = ""     # env var name holding the Vault token
    onprem_harbor_url: str = ""          # Harbor registry URL
    onprem_harbor_project: str = ""      # Harbor project name
    onprem_minio_endpoint: str = ""      # MinIO endpoint (or "" to use K8s ConfigMap)
    onprem_minio_bucket: str = ""
    onprem_secrets_backend: str = "k8s"  # "vault" | "k8s"
    onprem_registry_backend: str = "harbor"  # "harbor" | "docker_hub" | "plain"


# ── Abstract interface ────────────────────────────────────────

class CloudProvider(abc.ABC):
    """
    Abstract interface for all cloud operations the tool needs.
    Each provider implementation wraps one cloud's SDK.
    """

    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    def verify_credentials(self) -> tuple[bool, str]:
        """Returns (ok, message). Called before any operation."""
        ...

    # ── Container registry ─────────────────────────────────
    @abc.abstractmethod
    def ensure_registry_repo(self, repo_name: str) -> str:
        """Ensure a container image repository exists. Returns its URI."""
        ...

    # ── Secrets store ──────────────────────────────────────
    @abc.abstractmethod
    def ensure_secret(self, name: str, description: str,
                      placeholder: dict) -> str:
        """
        Ensure a secret exists (create with placeholder if not).
        Returns a reference string (ARN / resource ID / path).
        """
        ...

    @abc.abstractmethod
    def read_secret(self, reference: str) -> dict:
        """Read and return the secret value as a dict."""
        ...

    # ── Workload identity ──────────────────────────────────
    @abc.abstractmethod
    def ensure_workload_identity(self, issuer_id: str, namespace: str,
                                  cfg) -> str:
        """
        Ensure a workload identity (IAM role / Azure MI / GCP SA / K8s SA)
        exists and is bound to the given K8s namespace.
        Returns a reference string for the identity.
        """
        ...

    # ── DNS ────────────────────────────────────────────────
    @abc.abstractmethod
    def find_dns_zone(self, domain: str) -> str | None:
        """Find the managed DNS zone that covers domain. Returns zone ID or None."""
        ...

    # ── TLS certificate ────────────────────────────────────
    @abc.abstractmethod
    def ensure_tls_certificate(self, domain: str) -> str | None:
        """
        Ensure a TLS certificate exists for the domain.
        Returns a reference string or None if manual issuance is needed.
        """
        ...

    # ── Config file store (mimoto config) ─────────────────
    @abc.abstractmethod
    def read_config_file(self, bucket: str, key: str) -> dict:
        """Read a JSON config file from object/blob storage or a ConfigMap."""
        ...

    @abc.abstractmethod
    def write_config_file(self, bucket: str, key: str, data: dict) -> None:
        """Write a JSON config file to object/blob storage or a ConfigMap."""
        ...

    # ── Dry-run plan ───────────────────────────────────────
    @abc.abstractmethod
    def dry_run_plan(self, issuer_id: str, cfg) -> list[tuple[str, str]]:
        """Returns [(resource_type, name)] for the dry-run table."""
        ...


# ── Credential checker (runs in Phase 0) ─────────────────────

def check_and_explain(provider_cfg: CloudProviderConfig) -> tuple[bool, str]:
    """
    Human-friendly credential check before any cloud operation.
    Returns (ok, explanation).
    """
    p = provider_cfg.provider
    if p == "aws":
        return _check_aws(provider_cfg)
    elif p == "azure":
        return _check_azure(provider_cfg)
    elif p == "gcp":
        return _check_gcp(provider_cfg)
    elif p == "onprem":
        return _check_onprem(provider_cfg)
    return False, f"Unknown provider: {p!r}"


def _check_aws(cfg: CloudProviderConfig) -> tuple[bool, str]:
    """
    Checks the boto3 credential chain and tells the operator exactly
    which method was found (or which is missing).
    """
    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError

    # 1. Try explicit profile
    if cfg.aws_profile:
        try:
            session = boto3.Session(profile_name=cfg.aws_profile)
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            account = identity["Account"]
            arn = identity["Arn"]
            cfg.aws_auth_method = "profile"
            return True, f"AWS profile '{cfg.aws_profile}' → account {account} ({arn})"
        except Exception as e:
            return False, (
                f"AWS profile '{cfg.aws_profile}' not found or invalid.\n"
                f"  Error: {e}\n"
                f"  Run:  aws configure --profile {cfg.aws_profile}\n"
                f"  Or check: cat ~/.aws/credentials"
            )

    # 2. Try default chain (env vars, default profile, instance profile, etc.)
    try:
        import boto3
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account = identity["Account"]
        arn = identity["Arn"]

        # Detect which method was used
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            method = "environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)"
            cfg.aws_auth_method = "env"
        elif os.environ.get("AWS_PROFILE"):
            method = f"AWS_PROFILE env var ({os.environ['AWS_PROFILE']})"
            cfg.aws_auth_method = "profile"
        elif os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
            method = "ECS container role"
            cfg.aws_auth_method = "instance_profile"
        elif os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
            method = "EKS Pod Identity / IRSA (web identity token)"
            cfg.aws_auth_method = "pod_identity"
        else:
            method = "~/.aws/credentials (default profile or instance profile)"
            cfg.aws_auth_method = "profile"

        return True, f"AWS credentials OK via {method}\n  Account: {account}\n  Identity: {arn}"

    except NoCredentialsError:
        return False, _aws_no_creds_help()
    except ClientError as e:
        return False, f"AWS credentials found but invalid: {e}\n{_aws_no_creds_help()}"
    except Exception as e:
        return False, f"AWS credential check failed: {e}\n{_aws_no_creds_help()}"


def _aws_no_creds_help() -> str:
    return """
No AWS credentials found. The tool checks these sources in order:

  1. Environment variables (recommended for CI/CD):
       export AWS_ACCESS_KEY_ID=AKIA...
       export AWS_SECRET_ACCESS_KEY=...
       export AWS_SESSION_TOKEN=...  (if using temporary credentials)

  2. Named profile in ~/.aws/credentials:
       aws configure --profile my-profile
       then set aws_profile=my-profile in the tool

  3. AWS SSO / Identity Center:
       aws sso login --profile my-sso-profile

  4. EC2 Instance Profile / ECS Task Role / EKS Pod Identity:
       No configuration needed if running inside AWS infrastructure

  5. Assume Role (for cross-account access):
       export AWS_ROLE_ARN=arn:aws:iam::ACCOUNT:role/ROLE
       export AWS_WEB_IDENTITY_TOKEN_FILE=/path/to/token
"""


def _check_azure(cfg: CloudProviderConfig) -> tuple[bool, str]:
    """Check Azure CLI auth or Service Principal."""
    # Check if azure-identity is available
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import SubscriptionClient
    except ImportError:
        return False, (
            "Azure SDK not installed.\n"
            "  Run: pip install azure-identity azure-mgmt-resource azure-mgmt-containerregistry "
            "azure-keyvault-secrets azure-storage-blob azure-mgmt-dns azure-mgmt-network"
        )

    if not shutil.which("az"):
        if not (os.environ.get("AZURE_CLIENT_ID") and os.environ.get("AZURE_CLIENT_SECRET")):
            return False, _azure_no_creds_help()

    try:
        credential = DefaultAzureCredential()
        sub_client = SubscriptionClient(credential)
        subs = list(sub_client.subscriptions.list())
        if not subs:
            return False, "Azure credentials valid but no subscriptions accessible."
        sub_names = [s.display_name for s in subs[:3]]
        cfg.azure_auth_method = "managed_identity" if os.environ.get("MSI_ENDPOINT") else \
                                 "service_principal" if os.environ.get("AZURE_CLIENT_ID") else "cli"
        return True, f"Azure credentials OK ({cfg.azure_auth_method})\n  Subscriptions: {', '.join(sub_names)}"
    except Exception as e:
        return False, f"Azure credential check failed: {e}\n{_azure_no_creds_help()}"


def _azure_no_creds_help() -> str:
    return """
No Azure credentials found. Options:

  1. Azure CLI (recommended for interactive use):
       az login
       az account set --subscription YOUR_SUBSCRIPTION_ID

  2. Service Principal (recommended for CI/CD):
       export AZURE_TENANT_ID=...
       export AZURE_CLIENT_ID=...
       export AZURE_CLIENT_SECRET=...
       export AZURE_SUBSCRIPTION_ID=...

  3. Managed Identity (when running in Azure VM/AKS):
       No configuration needed — identity is assigned to the VM/pod

  Install the CLI: https://docs.microsoft.com/cli/azure/install-azure-cli
"""


def _check_gcp(cfg: CloudProviderConfig) -> tuple[bool, str]:
    """Check GCP Application Default Credentials."""
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        return False, (
            "GCP SDK not installed.\n"
            "  Run: pip install google-auth google-cloud-storage google-cloud-secret-manager "
            "google-cloud-container google-cloud-dns"
        )

    try:
        credentials, project = google.auth.default()
        project_id = cfg.gcp_project_id or project or "unknown"
        method = "service_account_key" if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") else \
                 "workload_identity" if os.environ.get("GOOGLE_CLOUD_PROJECT") else "gcloud_adc"
        cfg.gcp_auth_method = method
        return True, f"GCP credentials OK ({method})\n  Project: {project_id}"
    except Exception as e:
        return False, f"GCP credential check failed: {e}\n{_gcp_no_creds_help()}"


def _gcp_no_creds_help() -> str:
    return """
No GCP credentials found. Options:

  1. gcloud CLI / Application Default Credentials (interactive use):
       gcloud auth application-default login

  2. Service Account Key (CI/CD):
       export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json

  3. Workload Identity Federation (GKE):
       No configuration needed if running inside GKE with WI configured

  Install the CLI: https://cloud.google.com/sdk/docs/install
"""


def _check_onprem(cfg: CloudProviderConfig) -> tuple[bool, str]:
    """Check on-premise tooling: kubectl, helm, vault (optional), harbor (optional)."""
    issues = []
    found = []

    # kubectl
    if shutil.which("kubectl"):
        r = subprocess.run(["kubectl", "cluster-info", "--request-timeout=5s"],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            found.append("kubectl: cluster reachable")
        else:
            issues.append("kubectl is installed but cannot reach the cluster. Check your kubeconfig.")
    else:
        issues.append("kubectl not found. Install: https://kubernetes.io/docs/tasks/tools/")

    # helm
    if shutil.which("helm"):
        found.append("helm: available")
    else:
        issues.append("helm not found. Install: https://helm.sh/docs/intro/install/")

    # Vault (optional)
    if cfg.onprem_vault_addr:
        vault_token_env = cfg.onprem_vault_token_env or "VAULT_TOKEN"
        if os.environ.get(vault_token_env):
            found.append(f"Vault: token found in ${vault_token_env}")
        else:
            issues.append(
                f"Vault address configured ({cfg.onprem_vault_addr}) "
                f"but ${vault_token_env} is not set."
            )

    # Harbor (optional — just check URL reachability)
    if cfg.onprem_harbor_url:
        import httpx
        try:
            r = httpx.get(f"{cfg.onprem_harbor_url}/api/v2.0/ping",
                          timeout=5, verify=False)
            if r.status_code in (200, 401):
                found.append(f"Harbor: reachable at {cfg.onprem_harbor_url}")
            else:
                issues.append(f"Harbor at {cfg.onprem_harbor_url} returned {r.status_code}")
        except Exception as e:
            issues.append(f"Cannot reach Harbor at {cfg.onprem_harbor_url}: {e}")

    if issues:
        return False, "On-premise checks failed:\n" + "\n".join(f"  • {i}" for i in issues)

    return True, "On-premise environment OK:\n" + "\n".join(f"  ✓ {f}" for f in found)


# ── Provider factory ──────────────────────────────────────────

def get_provider(provider_cfg: CloudProviderConfig, issuer_cfg) -> "CloudProvider":
    """Return the correct CloudProvider implementation."""
    p = provider_cfg.provider
    if p == "aws":
        from inji_issuer_deploy.providers.aws import AWSProvider
        return AWSProvider(provider_cfg, issuer_cfg)
    elif p == "azure":
        from inji_issuer_deploy.providers.azure import AzureProvider
        return AzureProvider(provider_cfg, issuer_cfg)
    elif p == "gcp":
        from inji_issuer_deploy.providers.gcp import GCPProvider
        return GCPProvider(provider_cfg, issuer_cfg)
    elif p == "onprem":
        from inji_issuer_deploy.providers.onprem import OnPremProvider
        return OnPremProvider(provider_cfg, issuer_cfg)
    raise ValueError(f"Unknown provider: {p!r}")


def print_credential_status(provider: str, ok: bool, message: str) -> None:
    """Pretty-print the credential check result."""
    if ok:
        console.print(Panel(
            f"[green]✓[/green] {message}",
            title=f"[bold]{provider.upper()} credentials[/bold]",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[red]✗[/red] {message}",
            title=f"[bold]{provider.upper()} credentials — action required[/bold]",
            border_style="red",
        ))
