"""
GCP provider — implements CloudProvider using the Google Cloud SDK.

Credential resolution (Application Default Credentials):
  1. GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service account key
  2. gcloud auth application-default login (interactive)
  3. Workload Identity Federation (GKE with WI configured)
  4. Compute Engine / Cloud Run default service account

Required SDK packages:
  pip install google-auth google-cloud-storage google-cloud-secret-manager
              google-cloud-container google-cloud-dns
"""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console

from inji_issuer_deploy.cloud import CloudProvider, CloudProviderConfig

console = Console()


class GCPProvider(CloudProvider):

    def __init__(self, provider_cfg: CloudProviderConfig, issuer_cfg):
        self._pcfg = provider_cfg
        self._icfg = issuer_cfg
        self._project = provider_cfg.gcp_project_id or issuer_cfg.__dict__.get("gcp_project_id", "")

    def name(self) -> str:
        return "gcp"

    def verify_credentials(self) -> tuple[bool, str]:
        from inji_issuer_deploy.cloud import _check_gcp
        return _check_gcp(self._pcfg)

    # ── Container registry (Artifact Registry) ────────────

    def ensure_registry_repo(self, repo_name: str) -> str:
        """
        Artifact Registry repositories need to be created explicitly.
        Returns the full image path.
        """
        from google.cloud import artifactregistry_v1
        region = self._icfg.__dict__.get("gcp_region", "southamerica-east1")
        registry_id = f"inji-{self._icfg.issuer_id}"
        parent = f"projects/{self._project}/locations/{region}"
        repo_resource = f"{parent}/repositories/{registry_id}"

        client = artifactregistry_v1.ArtifactRegistryClient()
        try:
            client.get_repository(name=repo_resource)
            console.print(f"  [dim]↷ Artifact Registry {registry_id} — already exists[/dim]")
        except Exception:
            from google.cloud.artifactregistry_v1.types import Repository
            req = artifactregistry_v1.CreateRepositoryRequest(
                parent=parent,
                repository_id=registry_id,
                repository=Repository(format_=Repository.Format.DOCKER,
                                       description=f"inji-{self._icfg.issuer_id} images"),
            )
            client.create_repository(request=req).result()
            console.print(f"  [green]✓[/green] Artifact Registry {registry_id} created")

        uri = f"{region}-docker.pkg.dev/{self._project}/{registry_id}/{repo_name}"
        return uri

    # ── Secrets store (Secret Manager) ───────────────────

    def ensure_secret(self, name: str, description: str,
                       placeholder: dict) -> str:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        secret_id = name.replace("/", "-").replace("_", "-")
        parent = f"projects/{self._project}"
        resource_name = f"{parent}/secrets/{secret_id}"

        try:
            client.get_secret(name=resource_name)
            console.print(f"  [dim]↷ Secret {secret_id} — already exists[/dim]")
        except Exception:
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {
                        "replication": {"automatic": {}},
                        "labels": {"managed-by": "inji-issuer-deploy"},
                        "annotations": {"description": description[:255]},
                    },
                }
            )
            client.add_secret_version(
                request={"parent": resource_name,
                          "payload": {"data": json.dumps(placeholder).encode()}}
            )
            console.print(f"  [green]✓[/green] Secret {secret_id}")
            console.print(f"  [yellow]⚠[/yellow]  Fill in real values at: GCP Console → Secret Manager → {secret_id}")

        return resource_name

    def read_secret(self, reference: str) -> dict:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = reference if "/versions/" in reference else f"{reference}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return json.loads(resp.payload.data.decode())

    # ── Workload identity (GKE Workload Identity) ─────────

    def ensure_workload_identity(self, issuer_id: str, namespace: str,
                                  cfg) -> str:
        """
        Creates a GCP Service Account and binds it to the K8s ServiceAccount
        in the issuer's namespace via Workload Identity.
        Returns the service account email.
        """
        import google.auth
        from googleapiclient.discovery import build

        sa_name = f"inji-{issuer_id}-sa"
        sa_email = f"{sa_name}@{self._project}.iam.gserviceaccount.com"

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        iam_service = build("iam", "v1", credentials=credentials)

        try:
            iam_service.projects().serviceAccounts().get(
                name=f"projects/{self._project}/serviceAccounts/{sa_email}"
            ).execute()
            console.print(f"  [dim]↷ Service Account {sa_email} — already exists[/dim]")
        except Exception:
            iam_service.projects().serviceAccounts().create(
                name=f"projects/{self._project}",
                body={
                    "accountId": sa_name,
                    "serviceAccount": {
                        "displayName": f"inji-certify issuer {issuer_id}",
                        "description": "Managed by inji-issuer-deploy",
                    }
                }
            ).execute()
            console.print(f"  [green]✓[/green] Service Account {sa_email}")

        # Bind to K8s ServiceAccount via Workload Identity annotation
        console.print(
            f"  [yellow]⚠[/yellow]  Annotate the K8s ServiceAccount for Workload Identity:\n"
            f"     kubectl annotate serviceaccount inji-{issuer_id}-sa \\\n"
            f"       -n inji-{issuer_id} \\\n"
            f"       iam.gke.io/gcp-service-account={sa_email}"
        )
        return sa_email

    # ── DNS (Cloud DNS) ───────────────────────────────────

    def find_dns_zone(self, domain: str) -> str | None:
        from google.cloud import dns
        client = dns.Client(project=self._project)
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            zone_suffix = ".".join(parts[i:]) + "."
            for zone in client.list_zones():
                if zone.dns_name == zone_suffix:
                    console.print(f"  [green]✓[/green] Cloud DNS zone {zone.name} for {domain}")
                    return zone.name
        console.print(f"  [yellow]⚠[/yellow]  No Cloud DNS zone for {domain} — create DNS record manually")
        return None

    # ── TLS certificate (cert-manager) ───────────────────

    def ensure_tls_certificate(self, domain: str) -> str | None:
        """Generates a cert-manager Certificate manifest (same as Azure approach)."""
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

    # ── Config file store (GCS) ───────────────────────────

    def read_config_file(self, bucket: str, key: str) -> dict:
        from google.cloud import storage
        client = storage.Client(project=self._project)
        blob = client.bucket(bucket).blob(key)
        return json.loads(blob.download_as_text())

    def write_config_file(self, bucket: str, key: str, data: dict) -> None:
        from google.cloud import storage
        client = storage.Client(project=self._project)
        blob = client.bucket(bucket).blob(key)
        blob.upload_from_string(json.dumps(data, indent=2),
                                content_type="application/json")
        console.print(f"  [green]✓[/green] gs://{bucket}/{key} updated")

    # ── Dry-run plan ──────────────────────────────────────

    def dry_run_plan(self, issuer_id: str, cfg) -> list[tuple[str, str]]:
        region = self._icfg.__dict__.get("gcp_region", "southamerica-east1")
        return [
            ("GKE namespace",          f"inji-{issuer_id}"),
            ("Artifact Registry repo", f"{region}-docker.pkg.dev/{self._project}/inji-{issuer_id}/inji-certify"),
            ("Artifact Registry repo", f"{region}-docker.pkg.dev/{self._project}/inji-{issuer_id}/inji-verify"),
            ("Artifact Registry repo", f"{region}-docker.pkg.dev/{self._project}/inji-{issuer_id}/mimoto"),
            ("Secret Manager secret",  f"inji-{issuer_id}-db-credentials"),
            ("Secret Manager secret",  f"inji-{issuer_id}-data-api-credentials"),
            ("Secret Manager secret",  f"inji-{issuer_id}-softhsm-pin"),
            ("Service Account",        f"inji-{issuer_id}-sa (GKE Workload Identity)"),
            ("Cloud DNS",              f"zone lookup for {cfg.base_domain}"),
            ("cert-manager cert",      f"Certificate manifest for {cfg.base_domain}"),
            ("GCS patch",              f"gs://{cfg.mimoto_issuers_s3_bucket}/{cfg.mimoto_issuers_s3_key}"),
        ]
