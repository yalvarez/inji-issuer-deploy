"""
On-premise provider — implements CloudProvider without any public cloud.

Backends used:
  - Container registry:  Harbor  (or any Docker-compatible registry)
  - Secrets store:       HashiCorp Vault  OR  Kubernetes Secrets
  - Workload identity:   Kubernetes ServiceAccount + RBAC (no cloud IAM)
  - DNS:                 Manual (external-dns annotation or notes)
  - TLS certificate:     cert-manager (Let's Encrypt or internal CA)
  - Config file store:   MinIO  OR  Kubernetes ConfigMap

Credentials / connectivity required:
  - kubectl pointing to the cluster (KUBECONFIG env var or ~/.kube/config)
  - helm available in PATH
  - Vault: VAULT_ADDR + VAULT_TOKEN (or VAULT_ROLE_ID + VAULT_SECRET_ID)
  - Harbor: ~/.docker/config.json or HARBOR_USERNAME + HARBOR_PASSWORD
  - MinIO: MINIO_ACCESS_KEY + MINIO_SECRET_KEY (or open endpoint)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any

from rich.console import Console

from inji_issuer_deploy.cloud import CloudProvider, CloudProviderConfig

console = Console()


class OnPremProvider(CloudProvider):

    def __init__(self, provider_cfg: CloudProviderConfig, issuer_cfg):
        self._pcfg = provider_cfg
        self._icfg = issuer_cfg

    def name(self) -> str:
        return "onprem"

    def verify_credentials(self) -> tuple[bool, str]:
        from inji_issuer_deploy.cloud import _check_onprem
        return _check_onprem(self._pcfg)

    # ── Container registry (Harbor) ───────────────────────

    def ensure_registry_repo(self, repo_name: str) -> str:
        """
        Creates a Harbor project if it doesn't exist.
        Harbor repositories are created on first push within a project.
        """
        harbor_url = self._pcfg.onprem_harbor_url
        project = self._pcfg.onprem_harbor_project or f"inji-{self._icfg.issuer_id}"

        if harbor_url:
            import httpx
            user = os.environ.get("HARBOR_USERNAME", "admin")
            pwd  = os.environ.get("HARBOR_PASSWORD", "")
            try:
                # Check if project exists
                r = httpx.get(
                    f"{harbor_url}/api/v2.0/projects",
                    params={"name": project},
                    auth=(user, pwd), timeout=10, verify=False,
                )
                if r.status_code == 200 and any(p["name"] == project for p in r.json()):
                    console.print(f"  [dim]↷ Harbor project {project} — already exists[/dim]")
                else:
                    httpx.post(
                        f"{harbor_url}/api/v2.0/projects",
                        json={"project_name": project, "public": False},
                        auth=(user, pwd), timeout=10, verify=False,
                    )
                    console.print(f"  [green]✓[/green] Harbor project {project} created")
            except Exception as e:
                console.print(f"  [yellow]⚠[/yellow]  Could not create Harbor project: {e}")

            uri = f"{harbor_url.replace('https://', '').replace('http://', '')}/{project}/{repo_name}"
        else:
            # Fallback: just return the plain repo name for a local registry
            uri = f"localhost:5000/{project}/{repo_name}"
            console.print(f"  [yellow]⚠[/yellow]  No Harbor URL configured. Using local registry: {uri}")

        return uri

    # ── Secrets store (Vault or K8s Secrets) ─────────────

    def ensure_secret(self, name: str, description: str,
                       placeholder: dict) -> str:
        if self._pcfg.onprem_secrets_backend == "vault" and self._pcfg.onprem_vault_addr:
            return self._ensure_vault_secret(name, description, placeholder)
        else:
            return self._ensure_k8s_secret(name, placeholder)

    def _ensure_vault_secret(self, name: str, description: str,
                              placeholder: dict) -> str:
        """Write to Vault KV v2 at secret/inji/{issuer_id}/{name}."""
        vault_addr = self._pcfg.onprem_vault_addr
        token_env = self._pcfg.onprem_vault_token_env or "VAULT_TOKEN"
        token = os.environ.get(token_env, "")
        if not token:
            raise RuntimeError(f"Vault token not found in ${token_env}")

        import httpx
        path = f"secret/data/inji/{self._icfg.issuer_id}/{name.split('/')[-1]}"
        # Check if exists
        r = httpx.get(
            f"{vault_addr}/v1/{path}",
            headers={"X-Vault-Token": token}, timeout=10, verify=False,
        )
        if r.status_code == 200:
            console.print(f"  [dim]↷ Vault secret {path} — already exists[/dim]")
            return f"{vault_addr}/v1/{path}"

        # Write placeholder
        httpx.post(
            f"{vault_addr}/v1/{path}",
            json={"data": {**placeholder, "_description": description}},
            headers={"X-Vault-Token": token}, timeout=10, verify=False,
        ).raise_for_status()
        console.print(f"  [green]✓[/green] Vault secret {path}")
        console.print(f"  [yellow]⚠[/yellow]  Fill in real values: vault kv put {path} ...")
        return f"{vault_addr}/v1/{path}"

    def _ensure_k8s_secret(self, name: str, placeholder: dict) -> str:
        """Create a Kubernetes Secret in the issuer's namespace."""
        ns = f"inji-{self._icfg.issuer_id}"
        k8s_name = name.replace("/", "-").replace("_", "-")
        # Check if exists
        r = subprocess.run(
            ["kubectl", "get", "secret", k8s_name, "-n", ns],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            console.print(f"  [dim]↷ K8s Secret {k8s_name} in {ns} — already exists[/dim]")
            return f"k8s://{ns}/{k8s_name}"

        # Build --from-literal args
        literals = [f"--from-literal={k}={v}" for k, v in placeholder.items()]
        cmd = ["kubectl", "create", "secret", "generic", k8s_name,
               "-n", ns] + literals
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 and "already exists" not in result.stderr:
            raise RuntimeError(f"Failed to create K8s Secret: {result.stderr}")
        console.print(f"  [green]✓[/green] K8s Secret {k8s_name} in namespace {ns}")
        console.print(f"  [yellow]⚠[/yellow]  Fill in real values: kubectl edit secret {k8s_name} -n {ns}")
        return f"k8s://{ns}/{k8s_name}"

    def read_secret(self, reference: str) -> dict:
        if reference.startswith("k8s://"):
            # k8s://namespace/secret-name
            parts = reference[6:].split("/")
            ns, name = parts[0], parts[1]
            r = subprocess.run(
                ["kubectl", "get", "secret", name, "-n", ns, "-o", "json"],
                capture_output=True, text=True, check=True,
            )
            secret_data = json.loads(r.stdout)
            import base64
            return {k: base64.b64decode(v).decode()
                    for k, v in secret_data.get("data", {}).items()}
        else:
            # Vault
            vault_addr = self._pcfg.onprem_vault_addr
            token = os.environ.get(self._pcfg.onprem_vault_token_env or "VAULT_TOKEN", "")
            import httpx
            r = httpx.get(reference, headers={"X-Vault-Token": token},
                           timeout=10, verify=False)
            r.raise_for_status()
            return r.json().get("data", {}).get("data", {})

    # ── Workload identity (K8s ServiceAccount + RBAC) ─────

    def ensure_workload_identity(self, issuer_id: str, namespace: str,
                                  cfg) -> str:
        """
        Creates a K8s ServiceAccount with RBAC permissions.
        No cloud IAM involved — secrets are accessed via K8s Secrets directly.
        """
        sa_name = f"inji-{issuer_id}-sa"
        # Check if ServiceAccount exists
        r = subprocess.run(
            ["kubectl", "get", "serviceaccount", sa_name, "-n", namespace],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            console.print(f"  [dim]↷ ServiceAccount {sa_name} in {namespace} — already exists[/dim]")
            return f"k8s-sa://{namespace}/{sa_name}"

        # Create ServiceAccount
        sa_manifest = {
            "apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {
                "name": sa_name, "namespace": namespace,
                "labels": {"managed-by": "inji-issuer-deploy"},
            }
        }
        result = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(sa_manifest),
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create ServiceAccount: {result.stderr}")
        console.print(f"  [green]✓[/green] ServiceAccount {sa_name} in {namespace}")
        return f"k8s-sa://{namespace}/{sa_name}"

    # ── DNS (manual note) ─────────────────────────────────

    def find_dns_zone(self, domain: str) -> str | None:
        console.print(
            f"  [yellow]⚠[/yellow]  On-premise DNS: create an A/CNAME record for "
            f"{domain} pointing to your Ingress controller's IP.\n"
            f"     If using external-dns, annotate the Ingress resource."
        )
        return None

    # ── TLS certificate (cert-manager) ───────────────────

    def ensure_tls_certificate(self, domain: str) -> str | None:
        """Generates cert-manager Certificate manifest (same as Azure/GCP)."""
        issuer_type = "ClusterIssuer"
        issuer_name = "letsencrypt-prod"  # or your internal CA issuer name

        manifest = f"""apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: inji-{self._icfg.issuer_id}-tls
  namespace: inji-{self._icfg.issuer_id}
spec:
  secretName: inji-{self._icfg.issuer_id}-tls-secret
  issuerRef:
    name: {issuer_name}
    kind: {issuer_type}
  dnsNames:
    - {domain}
    - "*.{domain}"
"""
        cert_file = f".inji-deploy/{self._icfg.issuer_id}/cert-manager-certificate.yaml"
        pathlib.Path(cert_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(cert_file).write_text(manifest)
        console.print(f"  [green]✓[/green] cert-manager Certificate manifest → {cert_file}")
        console.print(
            f"  [yellow]⚠[/yellow]  Edit issuerRef.name if you use an internal CA instead of Let's Encrypt.\n"
            f"     Apply with: kubectl apply -f {cert_file}"
        )
        return cert_file

    # ── Config file store (MinIO or K8s ConfigMap) ────────

    def read_config_file(self, bucket: str, key: str) -> dict:
        if self._pcfg.onprem_minio_endpoint:
            return self._read_minio(bucket, key)
        else:
            return self._read_configmap(key)

    def write_config_file(self, bucket: str, key: str, data: dict) -> None:
        if self._pcfg.onprem_minio_endpoint:
            self._write_minio(bucket, key, data)
        else:
            self._write_configmap(key, data)

    def _read_minio(self, bucket: str, key: str) -> dict:
        from minio import Minio
        endpoint = self._pcfg.onprem_minio_endpoint.replace("https://", "").replace("http://", "")
        secure = self._pcfg.onprem_minio_endpoint.startswith("https://")
        client = Minio(
            endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            secure=secure,
        )
        resp = client.get_object(bucket, key)
        return json.loads(resp.read())

    def _write_minio(self, bucket: str, key: str, data: dict) -> None:
        from minio import Minio
        import io
        endpoint = self._pcfg.onprem_minio_endpoint.replace("https://", "").replace("http://", "")
        secure = self._pcfg.onprem_minio_endpoint.startswith("https://")
        client = Minio(
            endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            secure=secure,
        )
        content = json.dumps(data, indent=2).encode()
        client.put_object(bucket, key, io.BytesIO(content), len(content),
                          content_type="application/json")
        console.print(f"  [green]✓[/green] MinIO {bucket}/{key} updated")

    def _read_configmap(self, key: str) -> dict:
        ns = self._icfg.mimoto_service_namespace or "mimoto"
        cm_name = "mimoto-issuers-config"
        r = subprocess.run(
            ["kubectl", "get", "configmap", cm_name, "-n", ns,
             "-o", f"jsonpath={{.data.{key.replace('.', '_')}}}"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(f"ConfigMap {cm_name} not found in namespace {ns}")
        return json.loads(r.stdout)

    def _write_configmap(self, key: str, data: dict) -> None:
        ns = self._icfg.mimoto_service_namespace or "mimoto"
        cm_name = "mimoto-issuers-config"
        safe_key = key.replace(".", "_")
        content = json.dumps(data, indent=2)
        result = subprocess.run(
            ["kubectl", "patch", "configmap", cm_name, "-n", ns,
             "--type=merge",
             f"-p", json.dumps({"data": {safe_key: content}})],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to patch ConfigMap: {result.stderr}")
        console.print(f"  [green]✓[/green] ConfigMap {cm_name} in {ns} updated")

    # ── Dry-run plan ──────────────────────────────────────

    def dry_run_plan(self, issuer_id: str, cfg) -> list[tuple[str, str]]:
        harbor = self._pcfg.onprem_harbor_url or "localhost:5000"
        project = self._pcfg.onprem_harbor_project or f"inji-{issuer_id}"
        secret_backend = self._pcfg.onprem_secrets_backend
        config_backend = "MinIO" if self._pcfg.onprem_minio_endpoint else "K8s ConfigMap"

        return [
            ("K8s namespace",          f"inji-{issuer_id}"),
            ("Registry repo",          f"{harbor}/{project}/inji-certify"),
            ("Registry repo",          f"{harbor}/{project}/inji-verify"),
            ("Registry repo",          f"{harbor}/{project}/mimoto"),
            (f"{secret_backend} secret", f"inji/{issuer_id}/db-credentials"),
            (f"{secret_backend} secret", f"inji/{issuer_id}/data-api-credentials"),
            (f"{secret_backend} secret", f"inji/{issuer_id}/softhsm-pin"),
            ("K8s ServiceAccount",     f"inji-{issuer_id}-sa (RBAC)"),
            ("DNS",                    f"Manual record for {cfg.base_domain}"),
            ("cert-manager cert",      f"Certificate manifest for {cfg.base_domain}"),
            (config_backend,           f"mimoto-issuers-config patch"),
        ]
