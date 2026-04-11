"""
Azure provider — implements CloudProvider using the Azure SDK.

Credential resolution (DefaultAzureCredential chain):
  1. Environment variables: AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID
  2. Workload Identity (AKS with federated credentials)
  3. Managed Identity (VM, VMSS, App Service)
  4. Azure CLI: az login
  5. Azure PowerShell

Required SDK packages:
  pip install azure-identity azure-mgmt-containerregistry azure-keyvault-secrets
              azure-storage-blob azure-mgmt-dns azure-mgmt-network
              azure-mgmt-resource
"""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console

from inji_issuer_deploy.cloud import CloudProvider, CloudProviderConfig

console = Console()


class AzureProvider(CloudProvider):

    def __init__(self, provider_cfg: CloudProviderConfig, issuer_cfg):
        self._pcfg = provider_cfg
        self._icfg = issuer_cfg
        self._credential = None  # lazy-init

    def _cred(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()
        return self._credential

    def name(self) -> str:
        return "azure"

    def verify_credentials(self) -> tuple[bool, str]:
        from inji_issuer_deploy.cloud import _check_azure
        return _check_azure(self._pcfg)

    # ── Container registry (ACR) ──────────────────────────

    def ensure_registry_repo(self, repo_name: str) -> str:
        """
        In ACR, repositories are created implicitly on first push.
        We just return the URI the push will target.
        """
        # ACR registry name is derived from the issuer_id
        registry_name = f"inji{self._icfg.issuer_id}acr".replace("-", "")
        login_server = f"{registry_name}.azurecr.io"
        uri = f"{login_server}/{repo_name}"
        console.print(f"  [dim]→ ACR repository will be created on first push: {uri}[/dim]")
        return uri

    # ── Secrets store (Key Vault) ─────────────────────────

    def ensure_secret(self, name: str, description: str,
                       placeholder: dict) -> str:
        from azure.keyvault.secrets import SecretClient
        vault_url = f"https://inji-{self._icfg.issuer_id}-kv.vault.azure.net"
        client = SecretClient(vault_url=vault_url, credential=self._cred())

        # Sanitize name for Key Vault (only alphanumeric + hyphens)
        kv_name = name.replace("/", "-").replace("_", "-")

        try:
            existing = client.get_secret(kv_name)
            console.print(f"  [dim]↷ Key Vault secret {kv_name} — already exists[/dim]")
            return f"{vault_url}/secrets/{kv_name}"
        except Exception:
            pass

        client.set_secret(
            kv_name,
            json.dumps(placeholder),
            content_type="application/json",
            tags={"managed-by": "inji-issuer-deploy", "description": description[:250]},
        )
        console.print(f"  [green]✓[/green] Key Vault secret {kv_name}")
        console.print(f"  [yellow]⚠[/yellow]  Fill in real values at: {vault_url}/secrets/{kv_name}")
        return f"{vault_url}/secrets/{kv_name}"

    def read_secret(self, reference: str) -> dict:
        from azure.keyvault.secrets import SecretClient
        # reference format: https://vault.vault.azure.net/secrets/name
        parts = reference.split("/secrets/")
        vault_url = parts[0]
        secret_name = parts[1].split("/")[0] if len(parts) > 1 else ""
        client = SecretClient(vault_url=vault_url, credential=self._cred())
        val = client.get_secret(secret_name).value
        return json.loads(val)

    # ── Workload identity (Azure Managed Identity / AKS) ──

    def ensure_workload_identity(self, issuer_id: str, namespace: str,
                                  cfg) -> str:
        """
        Creates a User-Assigned Managed Identity and sets up AKS Workload Identity
        federation for the given K8s namespace/serviceaccount.
        Returns the client ID of the managed identity.
        """
        from azure.mgmt.msi import ManagedServiceIdentityClient
        sub_id = self._pcfg.azure_subscription_id
        rg = self._pcfg.azure_resource_group
        identity_name = f"inji-{issuer_id}-identity"

        msi_client = ManagedServiceIdentityClient(self._cred(), sub_id)
        try:
            identity = msi_client.user_assigned_identities.get(rg, identity_name)
            console.print(f"  [dim]↷ Managed Identity {identity_name} — already exists[/dim]")
            return identity.client_id
        except Exception:
            pass

        from azure.mgmt.msi.models import Identity
        identity = msi_client.user_assigned_identities.create_or_update(
            rg, identity_name,
            Identity(location=cfg.get("azure_location", "eastus"),
                     tags={"managed-by": "inji-issuer-deploy"})
        )
        console.print(f"  [green]✓[/green] Managed Identity {identity_name} → client_id={identity.client_id}")
        console.print(
            f"  [yellow]⚠[/yellow]  Configure AKS Workload Identity federation manually:\n"
            f"     az aks update --enable-oidc-issuer --enable-workload-identity ...\n"
            f"     az identity federated-credential create --identity-name {identity_name} ..."
        )
        return identity.client_id

    # ── DNS (Azure DNS) ───────────────────────────────────

    def find_dns_zone(self, domain: str) -> str | None:
        from azure.mgmt.dns import DnsManagementClient
        sub_id = self._pcfg.azure_subscription_id
        dns_client = DnsManagementClient(self._cred(), sub_id)
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            zone_name = ".".join(parts[i:])
            try:
                zones = list(dns_client.zones.list())
                for zone in zones:
                    if zone.name == zone_name:
                        console.print(f"  [green]✓[/green] Azure DNS zone {zone_name} found")
                        return zone.id
            except Exception:
                pass
        console.print(f"  [yellow]⚠[/yellow]  No Azure DNS zone found for {domain} — create DNS record manually")
        return None

    # ── TLS certificate ───────────────────────────────────

    def ensure_tls_certificate(self, domain: str) -> str | None:
        """
        In AKS, cert-manager with Let's Encrypt is the standard approach.
        This method generates the cert-manager Certificate manifest to apply.
        """
        manifest = f"""apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: inji-{self._icfg.issuer_id}-tls
  namespace: inji-{self._icfg.issuer_id}
spec:
  secretName: inji-{self._icfg.issuer_id}-tls-secret
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
    - {domain}
    - "*.{domain}"
"""
        cert_file = f".inji-deploy/{self._icfg.issuer_id}/cert-manager-certificate.yaml"
        import pathlib
        pathlib.Path(cert_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(cert_file).write_text(manifest)
        console.print(f"  [green]✓[/green] cert-manager Certificate manifest → {cert_file}")
        console.print(f"  [yellow]⚠[/yellow]  Apply with: kubectl apply -f {cert_file}")
        return cert_file

    # ── Config file store (Azure Blob Storage) ────────────

    def read_config_file(self, bucket: str, key: str) -> dict:
        from azure.storage.blob import BlobServiceClient
        account_name = bucket  # treat bucket as storage account name
        container = "config"
        client = BlobServiceClient(
            f"https://{account_name}.blob.core.windows.net",
            credential=self._cred(),
        )
        blob = client.get_blob_client(container=container, blob=key)
        return json.loads(blob.download_blob().readall())

    def write_config_file(self, bucket: str, key: str, data: dict) -> None:
        from azure.storage.blob import BlobServiceClient
        account_name = bucket
        container = "config"
        client = BlobServiceClient(
            f"https://{account_name}.blob.core.windows.net",
            credential=self._cred(),
        )
        blob = client.get_blob_client(container=container, blob=key)
        blob.upload_blob(json.dumps(data, indent=2), overwrite=True,
                         content_settings={"content_type": "application/json"})
        console.print(f"  [green]✓[/green] Blob {account_name}/{container}/{key} updated")

    # ── Dry-run plan ──────────────────────────────────────

    def dry_run_plan(self, issuer_id: str, cfg) -> list[tuple[str, str]]:
        registry_name = f"inji{issuer_id}acr".replace("-", "")
        return [
            ("AKS namespace",          f"inji-{issuer_id}"),
            ("ACR repository",         f"{registry_name}.azurecr.io/{issuer_id}/inji-certify"),
            ("ACR repository",         f"{registry_name}.azurecr.io/{issuer_id}/inji-verify"),
            ("ACR repository",         f"{registry_name}.azurecr.io/{issuer_id}/mimoto"),
            ("Key Vault secret",       f"inji-{issuer_id}-db-credentials"),
            ("Key Vault secret",       f"inji-{issuer_id}-data-api-credentials"),
            ("Key Vault secret",       f"inji-{issuer_id}-softhsm-pin"),
            ("Managed Identity",       f"inji-{issuer_id}-identity (AKS Workload Identity)"),
            ("Azure DNS",              f"zone lookup for {cfg.base_domain}"),
            ("cert-manager cert",      f"Certificate manifest for {cfg.base_domain}"),
            ("Blob Storage patch",     f"{cfg.mimoto_issuers_s3_bucket}/config/{cfg.mimoto_issuers_s3_key}"),
        ]
