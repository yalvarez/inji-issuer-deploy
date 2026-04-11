"""
Phase 0 — data collection.

Asks the operator for the minimum required inputs, derives everything
else automatically, and validates before saving to state.
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from inji_issuer_deploy.cloud import (
    CloudProviderConfig,
    check_and_explain,
    print_credential_status,
)
from inji_issuer_deploy.state import DeployState, IssuerConfig, save_state

console = Console()


# ── helpers ───────────────────────────────────────────────────

def _ask(prompt: str, default: str = "", secret: bool = False,
         validator=None, hint: str = "") -> str:
    while True:
        full_prompt = f"  [bold cyan]{prompt}[/bold cyan]"
        if hint:
            full_prompt += f" [dim]({hint})[/dim]"
        if default:
            full_prompt += f" [dim]\\[{default}][/dim]"
        console.print(full_prompt)
        if secret:
            import getpass
            value = getpass.getpass("  > ").strip()
        else:
            value = input("  > ").strip()
        if not value and default:
            value = default
        if not value:
            console.print("  [red]Required — cannot be empty.[/red]")
            continue
        if validator:
            err = validator(value)
            if err:
                console.print(f"  [red]{err}[/red]")
                continue
        return value


def _ask_optional(prompt: str, default: str = "", validator=None,
                  hint: str = "") -> str:
    while True:
        full_prompt = f"  [bold cyan]{prompt}[/bold cyan]"
        if hint:
            full_prompt += f" [dim]({hint})[/dim]"
        if default:
            full_prompt += f" [dim]\\[{default}][/dim]"
        console.print(full_prompt)
        value = input("  > ").strip()
        if not value:
            value = default
        if value and validator:
            err = validator(value)
            if err:
                console.print(f"  [red]{err}[/red]")
                continue
        return value


def _ask_bool(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        console.print(f"  [bold cyan]{prompt}[/bold cyan] [dim]{suffix}[/dim]")
        raw = input("  > ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        console.print("  [red]Please enter y or n.[/red]")


def _ask_choice(prompt: str, choices: list[str], default: str = "") -> str:
    for i, c in enumerate(choices, 1):
        marker = " [dim](default)[/dim]" if c == default else ""
        console.print(f"  [dim]{i})[/dim] {c}{marker}")
    while True:
        console.print(f"  [bold cyan]{prompt}[/bold cyan]")
        raw = input("  > ").strip()
        if not raw and default:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        if raw in choices:
            return raw
        console.print(f"  [red]Pick a number 1–{len(choices)} or type the value.[/red]")


def _slug(value: str) -> str | None:
    """Returns error string if invalid slug, else None."""
    if not re.match(r'^[a-z][a-z0-9-]{1,30}$', value):
        return "Must be lowercase letters, numbers, hyphens. Start with a letter. Max 31 chars."
    return None


def _url(value: str) -> str | None:
    if not value.startswith(("http://", "https://")):
        return "Must start with http:// or https://"
    return None


def _domain(value: str) -> str | None:
    if not re.match(r'^[a-z0-9][a-z0-9.\-]+\.[a-z]{2,}$', value):
        return "Must be a valid domain (e.g. certify.mtc.gob.pe)"
    return None


def _load_provider_cfg(state: DeployState) -> CloudProviderConfig:
    raw_pc = getattr(state, "provider_cfg", None) or {}
    provider_cfg = CloudProviderConfig(**raw_pc) if isinstance(raw_pc, dict) else raw_pc
    if not provider_cfg.provider:
        provider_cfg.provider = "aws"
    if not provider_cfg.provisioner:
        provider_cfg.provisioner = "python"
    return provider_cfg


# ── scope mapping wizard ──────────────────────────────────────

def _collect_scope_mappings() -> list[dict]:
    """
    Collect one or more credential type definitions.
    Each maps an OAuth scope → a RENIEC-style profile + service endpoint.
    """
    console.print("\n  [bold]Credential types[/bold]")
    console.print("  Define each credential this issuer will emit.")
    console.print("  [dim]Example for MTC: scope=licencia-conducir, profile=LICENCIA_B, service=ws-licencias[/dim]\n")

    mappings: list[dict] = []
    while True:
        console.print(f"  [bold]Credential #{len(mappings) + 1}[/bold]")
        scope = _ask(
            "OAuth scope",
            hint="e.g. licencia-conducir  (used in the access token)",
            validator=lambda v: None if re.match(r'^[a-z][a-z0-9-]{1,60}$', v)
                                else "Lowercase letters, numbers, hyphens only"
        )
        profile = _ask(
            "Backend profile name",
            hint="value sent in the request body to your data API, e.g. LICENCIA_CONDUCIR_B",
        )
        service = _ask(
            "API service path / endpoint suffix",
            hint="appended to base URL, e.g. ws-licencias  or  /v1/licencias/obtener",
        )
        display_name = _ask(
            "Display name (shown in wallet)",
            hint="e.g. Licencia de Conducir Clase B",
        )
        needs_filiation = _ask_bool(
            "Does this credential require a secondary person ID (filiation)?",
            default=False,
        )
        mappings.append({
            "scope": scope,
            "profile": profile,
            "service": service,
            "display_name": display_name,
            "requires_filiation": needs_filiation,
        })
        if not _ask_bool("\n  Add another credential type?", default=False):
            break
    return mappings


# ── main collector ────────────────────────────────────────────

def run(state: DeployState) -> None:
    console.print(Panel(
        "[bold]Phase 0 — Issuer configuration[/bold]\n"
        "Answer the questions below. Press Enter to accept defaults.\n"
        "All inputs are saved to [cyan]inji-deploy-state.json[/cyan] — "
        "you can re-run the tool to resume.",
        title="inji-issuer-deploy",
        border_style="cyan",
    ))

    cfg: IssuerConfig = state.issuer
    provider_cfg = _load_provider_cfg(state)

    # ── 1. Identity ──────────────────────────────────────────
    console.print("\n[bold underline]1. Issuer identity[/bold underline]")

    cfg.issuer_id = _ask(
        "Issuer ID (slug)",
        default=cfg.issuer_id or "",
        hint="short unique id, e.g. mtc  —  used in resource names",
        validator=_slug,
    )
    cfg.issuer_name = _ask(
        "Issuer display name",
        default=cfg.issuer_name or "",
        hint="shown in the wallet, e.g. Ministerio de Transportes y Comunicaciones",
    )
    cfg.issuer_description = _ask(
        "Short description",
        default=cfg.issuer_description or f"Credentials issued by {cfg.issuer_name}",
        hint="shown in the wallet below the issuer name",
    )
    cfg.issuer_logo_url = _ask(
        "Logo URL",
        default=cfg.issuer_logo_url or "",
        hint="https://... PNG or SVG, publicly accessible",
        validator=_url,
    )

    # ── 2. Deployment target ─────────────────────────────────
    console.print("\n[bold underline]2. Deployment target[/bold underline]")

    provider_cfg.provider = _ask_choice(
        "Infrastructure provider",
        choices=["aws", "azure", "gcp", "onprem"],
        default=provider_cfg.provider or "aws",
    )
    provider_cfg.provisioner = _ask_choice(
        "Provisioning engine",
        choices=["python", "terraform"],
        default=provider_cfg.provisioner or "python",
    )

    cfg.base_domain = _ask(
        "Base domain for this issuer",
        default=cfg.base_domain or f"certify.{cfg.issuer_id}.gob.pe",
        hint="e.g. certify.mtc.gob.pe — the Certify service will be exposed here",
        validator=_domain,
    )
    cfg.aws_region = _ask(
        "Primary cloud region / location",
        default=cfg.aws_region or "sa-east-1",
        hint="e.g. sa-east-1, eastus, southamerica-east1",
    )
    cfg.eks_cluster_name = _ask(
        "Kubernetes cluster name",
        default=cfg.eks_cluster_name or "INJI-prod",
        hint="EKS / AKS / GKE or on-prem cluster name",
    )

    if provider_cfg.provider != "aws":
        cfg.aws_account_id = ""

    if provider_cfg.provider == "aws":
        cfg.aws_account_id = _ask(
            "AWS account ID (12 digits)",
            default=cfg.aws_account_id or "",
            hint="the issuer's own AWS account — not the shared platform account",
            validator=lambda v: None if re.match(r'^\d{12}$', v) else "Must be 12 digits",
        )
        provider_cfg.aws_profile = _ask_optional(
            "AWS profile (optional)",
            default=provider_cfg.aws_profile or "",
            hint="leave blank to use the default AWS credential chain",
        )
        provider_cfg.aws_route53_zone_name = _ask_optional(
            "Route53 hosted zone name (optional)",
            default=provider_cfg.aws_route53_zone_name or "",
            hint="e.g. mtc.gob.pe — lets Terraform automate DNS and ACM validation",
        )
        provider_cfg.aws_existing_acm_certificate_arn = _ask_optional(
            "Existing ACM certificate ARN (optional)",
            default=provider_cfg.aws_existing_acm_certificate_arn or "",
            hint="reuse an existing wildcard/exact cert instead of requesting a new one",
        )
        provider_cfg.aws_manage_acm = _ask_bool(
            "Should Terraform request/manage an ACM certificate?",
            default=provider_cfg.aws_manage_acm or not bool(provider_cfg.aws_existing_acm_certificate_arn),
        )
        provider_cfg.aws_create_dns_record = _ask_bool(
            "Should Terraform create the issuer Route53 record when a target is known?",
            default=provider_cfg.aws_create_dns_record,
        )
        if provider_cfg.aws_create_dns_record:
            provider_cfg.aws_dns_record_name = _ask_optional(
                "DNS record name",
                default=provider_cfg.aws_dns_record_name or cfg.base_domain,
                hint="defaults to the issuer base domain",
            )
            provider_cfg.aws_dns_target_name = _ask_optional(
                "DNS target name (optional)",
                default=provider_cfg.aws_dns_target_name or "",
                hint="ALB/CloudFront hostname if you already know it",
            )
            provider_cfg.aws_dns_target_zone_id = _ask_optional(
                "DNS target zone ID (optional)",
                default=provider_cfg.aws_dns_target_zone_id or "",
                hint="needed only for Route53 alias records; leave blank for a CNAME",
            )
    elif provider_cfg.provider == "azure":
        cfg.aws_account_id = ""
        provider_cfg.azure_subscription_id = _ask(
            "Azure subscription ID",
            default=provider_cfg.azure_subscription_id or "",
        )
        provider_cfg.azure_resource_group = _ask(
            "Azure resource group",
            default=provider_cfg.azure_resource_group or "",
        )
    elif provider_cfg.provider == "gcp":
        provider_cfg.gcp_project_id = _ask(
            "GCP project ID",
            default=provider_cfg.gcp_project_id or "",
        )
    else:
        provider_cfg.onprem_secrets_backend = _ask_choice(
            "On-prem secrets backend",
            choices=["k8s", "vault"],
            default=provider_cfg.onprem_secrets_backend or "k8s",
        )
        provider_cfg.onprem_vault_addr = _ask_optional(
            "Vault address (optional)",
            default=provider_cfg.onprem_vault_addr or "",
            hint="leave blank to use Kubernetes Secrets only",
        )
        if provider_cfg.onprem_vault_addr:
            provider_cfg.onprem_vault_token_env = _ask(
                "Vault token environment variable",
                default=provider_cfg.onprem_vault_token_env or "VAULT_TOKEN",
            )
        provider_cfg.onprem_harbor_url = _ask_optional(
            "Harbor registry URL (optional)",
            default=provider_cfg.onprem_harbor_url or "",
            hint="e.g. https://harbor.internal",
            validator=_url,
        )
        provider_cfg.onprem_harbor_project = _ask_optional(
            "Harbor project (optional)",
            default=provider_cfg.onprem_harbor_project or f"inji-{cfg.issuer_id}",
        )
        provider_cfg.onprem_minio_endpoint = _ask_optional(
            "MinIO endpoint (optional)",
            default=provider_cfg.onprem_minio_endpoint or "",
            hint="e.g. https://minio.internal",
            validator=_url,
        )
        provider_cfg.onprem_minio_bucket = _ask_optional(
            "MinIO bucket (optional)",
            default=provider_cfg.onprem_minio_bucket or "",
        )

    # ── 3. Shared infrastructure ─────────────────────────────
    console.print("\n[bold underline]3. Shared infrastructure[/bold underline]")

    cfg.rds_host = _ask(
        "PostgreSQL host",
        default=cfg.rds_host or "",
        hint="shared DB endpoint, e.g. inji-prod-postgres.xxxx.sa-east-1.rds.amazonaws.com",
    )
    cfg.rds_admin_secret_arn = _ask(
        "Database admin secret reference",
        default=cfg.rds_admin_secret_arn or "",
        hint="Secrets Manager / Key Vault / Secret Manager / Vault reference for the DB superuser",
    )
    cfg.mimoto_issuers_s3_bucket = _ask(
        "Mimoto config bucket / container",
        default=cfg.mimoto_issuers_s3_bucket or "",
        hint="S3 / Blob / GCS / MinIO location holding mimoto-issuers-config.json",
    )
    cfg.mimoto_issuers_s3_key = _ask(
        "Mimoto config object key / blob name",
        default=cfg.mimoto_issuers_s3_key or "mimoto-issuers-config.json",
    )
    cfg.mimoto_service_namespace = _ask(
        "mimoto Kubernetes namespace",
        default=cfg.mimoto_service_namespace or "mimoto",
    )
    cfg.mimoto_service_name = _ask(
        "mimoto Kubernetes deployment name",
        default=cfg.mimoto_service_name or "mimoto",
    )

    # ── 4. Identity provider (IDPeru) ─────────────────────────
    console.print("\n[bold underline]4. Identity provider — IDPeru[/bold underline]")
    console.print("  [dim]Certify validates access tokens against IDPeru's JWKS.[/dim]")

    cfg.idperu_jwks_uri = _ask(
        "IDPeru JWKS URI",
        default=cfg.idperu_jwks_uri or "",
        hint="e.g. https://idperu.gob.pe/v1/idperu/oauth/.well-known/jwks.json",
        validator=_url,
    )
    cfg.idperu_issuer_uri = _ask(
        "IDPeru issuer URI",
        default=cfg.idperu_issuer_uri or "",
        hint="e.g. https://idperu.gob.pe/v1/idperu",
        validator=_url,
    )
    cfg.document_number_claim = _ask(
        "IDPeru token claim name for the national ID",
        default=cfg.document_number_claim or "individualId",
        hint="the claim in the IDPeru access token that carries the citizen's DNI",
    )
    has_filiation = _ask_bool(
        "Will any credential require a secondary person ID (filiation claim)?",
        default=bool(cfg.filiation_claim),
    )
    if has_filiation:
        cfg.filiation_claim = _ask(
            "IDPeru claim name for filiation ID",
            default=cfg.filiation_claim or "relatedPersonId",
        )
    else:
        cfg.filiation_claim = ""

    # ── 5. Data API ───────────────────────────────────────────
    console.print("\n[bold underline]5. Issuer data API[/bold underline]")
    console.print("  [dim]The API that returns identity data given a national ID.[/dim]")
    console.print("  [dim]The plugin will POST to: {base_url}/{service}/ObtenerDatos[/dim]\n")

    cfg.data_api_base_url = _ask(
        "Data API base URL",
        default=cfg.data_api_base_url or "",
        hint="e.g. https://api.licencias.mtc.gob.pe",
        validator=_url,
    )
    console.print("\n  Authentication method toward the data API:")
    cfg.data_api_auth_type = _ask_choice(
        "Choose auth type",
        choices=["mtls", "oauth2", "apikey", "none"],
        default=cfg.data_api_auth_type or "mtls",
    )
    cfg.data_api_secret_arn = _ask(
        "Secret reference — data API credentials",
        default=cfg.data_api_secret_arn or "",
        hint="Secret reference containing cert/key (mtls), client_secret (oauth2), or api_key (apikey)",
    )
    if cfg.data_api_auth_type == "oauth2":
        cfg.data_api_token_url = _ask(
            "OAuth2 token URL for the data API",
            default=cfg.data_api_token_url or "",
            validator=_url,
        )

    # ── 6. Credential types ───────────────────────────────────
    console.print("\n[bold underline]6. Credential types[/bold underline]")
    if cfg.scope_mappings and _ask_bool(
        f"  {len(cfg.scope_mappings)} credential type(s) already configured. Keep them?",
        default=True,
    ):
        pass  # keep existing
    else:
        cfg.scope_mappings = _collect_scope_mappings()

    # ── 7. Kubernetes options ─────────────────────────────────
    console.print("\n[bold underline]7. Kubernetes options[/bold underline]")

    cfg.certify_image = _ask(
        "Certify container image",
        default=cfg.certify_image,
    )
    cfg.chart_version = _ask(
        "Inji-certify Helm chart version",
        default=cfg.chart_version,
    )

    # ── Summary ───────────────────────────────────────────────
    state.provider_cfg = asdict(provider_cfg)
    _print_summary(cfg, provider_cfg)
    if not _ask_bool("\nProceed with this configuration?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    save_state(state)

    console.print("\n[bold underline]Credential pre-check[/bold underline]")
    ok, message = check_and_explain(provider_cfg)
    print_credential_status(provider_cfg.provider, ok, message)
    if not ok:
        console.print(
            "[yellow]You can keep this configuration, but Phase 1 will require these credentials to be fixed.[/yellow]"
        )

    console.print("\n[green]Phase 0 complete — configuration saved.[/green]")


def _print_summary(cfg: IssuerConfig, provider_cfg: CloudProviderConfig | None = None) -> None:
    console.print("\n")
    t = Table(title="Configuration summary", show_header=True, header_style="bold cyan")
    t.add_column("Parameter", style="dim")
    t.add_column("Value")

    rows = [
        ("Issuer ID",        cfg.issuer_id),
        ("Display name",     cfg.issuer_name),
        ("Provider",         provider_cfg.provider if provider_cfg else "aws"),
        ("Provisioner",      provider_cfg.provisioner if provider_cfg else "python"),
        ("Base domain",      cfg.base_domain),
        ("Account / project", (
            cfg.aws_account_id if provider_cfg and provider_cfg.provider == "aws" else
            provider_cfg.azure_subscription_id if provider_cfg and provider_cfg.provider == "azure" else
            provider_cfg.gcp_project_id if provider_cfg and provider_cfg.provider == "gcp" else
            provider_cfg.onprem_harbor_url if provider_cfg and provider_cfg.provider == "onprem" else ""
        )),
        ("Region / location", cfg.aws_region),
        ("Kubernetes cluster", cfg.eks_cluster_name),
        ("DB host",          cfg.rds_host),
        ("IDPeru JWKS",      cfg.idperu_jwks_uri),
        ("IDPeru claim",     cfg.document_number_claim),
        ("Data API",         cfg.data_api_base_url),
        ("Data API auth",    cfg.data_api_auth_type),
        ("Credentials",      str(len(cfg.scope_mappings))),
        ("Certify image",    cfg.certify_image),
    ]
    if provider_cfg and provider_cfg.provider == "aws":
        rows.extend([
            ("AWS profile", provider_cfg.aws_profile),
            ("Route53 zone", provider_cfg.aws_route53_zone_name),
            ("Manage ACM", "yes" if provider_cfg.aws_manage_acm else "no"),
        ])
    for k, v in rows:
        t.add_row(k, v or "[red]<empty>[/red]")

    console.print(t)

    if cfg.scope_mappings:
        s = Table(title="Credential types", show_header=True, header_style="bold cyan")
        s.add_column("Scope")
        s.add_column("Profile")
        s.add_column("Service")
        s.add_column("Filiation")
        for m in cfg.scope_mappings:
            s.add_row(
                m["scope"], m["profile"], m["service"],
                "yes" if m.get("requires_filiation") else "no",
            )
        console.print(s)
