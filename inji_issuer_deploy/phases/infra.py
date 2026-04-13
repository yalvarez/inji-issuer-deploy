"""
Phase 1 — infrastructure provisioning (cloud-agnostic).

Delegates all cloud operations to the CloudProvider abstraction.
The same code path runs regardless of whether the target is
AWS, Azure, GCP, or on-premise.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from inji_issuer_deploy.cloud import get_provider, check_and_explain, print_credential_status
from inji_issuer_deploy.state import DeployState, save_state

console = Console()


def _step(msg: str) -> None:
    console.print(f"  [cyan]→[/cyan] {msg}")


def _ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _ensure_k8s_namespace(ns: str) -> None:
    """Create K8s namespace if it doesn't exist (provider-agnostic — always kubectl)."""
    _step(f"EKS/K8s namespace {ns!r}")
    r = subprocess.run(["kubectl", "get", "namespace", ns],
                       capture_output=True, text=True, check=False)
    if r.returncode == 0:
        console.print(f"  [dim]↷ namespace {ns} — already exists[/dim]")
        return
    r = subprocess.run(["kubectl", "create", "namespace", ns],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to create namespace {ns}: {r.stderr}")
    # Label for Istio injection
    subprocess.run(["kubectl", "label", "namespace", ns,
                    "istio-injection=enabled", "--overwrite"],
                   capture_output=True, check=False)
    _ok(f"namespace {ns} created")


def _resolve_provider_cfg(state: DeployState):
    from inji_issuer_deploy.cloud import CloudProviderConfig

    raw_pc = getattr(state, "provider_cfg", None) or {}
    provider_cfg = CloudProviderConfig(**raw_pc) if isinstance(raw_pc, dict) else raw_pc

    if not provider_cfg.provider:
        if state.issuer.aws_account_id:
            provider_cfg.provider = "aws"
        else:
            provider_cfg.provider = "onprem"
    if not provider_cfg.provisioner:
        provider_cfg.provisioner = "python"

    state.provider_cfg = asdict(provider_cfg)
    return provider_cfg


def _write_terraform_tfvars(cfg, provider_cfg) -> Path:
    out_dir = Path(".inji-deploy") / cfg.issuer_id
    out_dir.mkdir(parents=True, exist_ok=True)
    tfvars_path = out_dir / "terraform.tfvars.json"
    payload = {
        "provider": provider_cfg.provider,
        "issuer_id": cfg.issuer_id,
        "issuer_name": cfg.issuer_name,
        "issuer_description": cfg.issuer_description,
        "base_domain": cfg.base_domain,
        "region": cfg.aws_region,
        "kubernetes_cluster_name": cfg.eks_cluster_name,
        "aws_account_id": cfg.aws_account_id,
        "rds_host": cfg.rds_host,
        "rds_port": cfg.rds_port,
        "rds_admin_secret_ref": cfg.rds_admin_secret_arn,
        "mimoto_config_store": {
            "bucket": cfg.mimoto_issuers_s3_bucket,
            "key": cfg.mimoto_issuers_s3_key,
        },
        "provider_cfg": asdict(provider_cfg),
    }
    tfvars_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tfvars_path


def _terraform_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "terraform"


def _load_terraform_outputs(tf_dir: Path, cfg) -> dict | None:
    if not shutil.which("terraform"):
        return None

    result = subprocess.run(
        ["terraform", f"-chdir={tf_dir}", "output", "-json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    raw = json.loads(result.stdout or "{}")
    outputs = {name: meta.get("value") for name, meta in raw.items()}
    outputs.setdefault("namespace", f"inji-{cfg.issuer_id}")
    outputs.setdefault("db_name", f"inji_{cfg.issuer_id}")
    if outputs.get("workload_identity_ref") and not outputs.get("pod_identity_role_arn"):
        outputs["pod_identity_role_arn"] = outputs["workload_identity_ref"]
    return outputs


def run(state: DeployState, dry_run: bool = False) -> None:
    console.print(Panel(
        "[bold]Phase 1 — Infrastructure provisioning[/bold]",
        border_style="cyan",
    ))

    cfg = state.issuer
    provider_cfg = _resolve_provider_cfg(state)

    if not provider_cfg.provider:
        raise RuntimeError(
            "No infrastructure provider configured. Re-run Phase 0: "
            "inji-issuer-deploy phase collect"
        )

    ns = f"inji-{cfg.issuer_id}"

    if provider_cfg.provisioner == "terraform":
        tfvars_path = _write_terraform_tfvars(cfg, provider_cfg)
        tf_dir = _terraform_dir()
        imported_outputs = _load_terraform_outputs(tf_dir, cfg)

        if imported_outputs:
            state.mark_done("infra", imported_outputs)
            save_state(state)
            console.print(
                f"\n[green]Phase 1 complete — imported Terraform outputs from {tf_dir}."
                f" Resume continues with the next phase.[/green]"
            )
            return

        handoff_message = (
            "Terraform mode is selected. Apply the generated inputs from ./terraform "
            f"and then re-run Phase 1 so the CLI can import the outputs. tfvars: {tfvars_path}"
        )
        console.print(
            f"\n  [bold]Terraform handoff[/bold]\n"
            f"  Inputs generated at [cyan]{tfvars_path}[/cyan].\n"
            f"  After [cyan]terraform apply[/cyan], re-run [cyan]inji-issuer-deploy phase infra[/cyan]"
            f" or [cyan]inji-issuer-deploy run[/cyan] to import the outputs and continue."
        )
        console.print(
            f"  [dim]Suggested commands:\n"
            f"    terraform -chdir=terraform init\n"
            f"    terraform -chdir=terraform plan -var-file=../{tfvars_path.as_posix()}\n"
            f"    terraform -chdir=terraform apply -var-file=../{tfvars_path.as_posix()}[/dim]"
        )
        if dry_run:
            return
        state.mark_failed("infra", handoff_message)
        save_state(state)
        raise RuntimeError(handoff_message)

    # Verify credentials before touching anything
    console.print(f"\n  [bold]Verifying {provider_cfg.provider.upper()} credentials...[/bold]")
    ok, message = check_and_explain(provider_cfg, cfg)
    print_credential_status(provider_cfg.provider, ok, message)
    if not ok:
        raise RuntimeError(
            f"Credential check failed for provider '{provider_cfg.provider}'.\n"
            "Fix the issue above and re-run."
        )

    provider = get_provider(provider_cfg, cfg)

    if dry_run:
        _print_dry_run(provider, cfg.issuer_id, cfg)
        return

    state.mark_started("infra")
    outputs: dict = {}

    try:
        # 1. K8s namespace (always kubectl, provider-independent)
        console.print("\n  [bold]1. Kubernetes namespace[/bold]")
        _ensure_k8s_namespace(ns)
        outputs["namespace"] = ns

        # 2. Container registries
        console.print(f"\n  [bold]2. Container registries ({provider.name()})[/bold]")
        ecr_uris: dict[str, str] = {}
        for svc in ["inji-certify", "inji-verify", "mimoto"]:
            uri = provider.ensure_registry_repo(f"{cfg.issuer_id}/{svc}")
            ecr_uris[svc] = uri
        outputs["registry_uris"] = ecr_uris

        # 3. Secrets store
        console.print(f"\n  [bold]3. Secrets ({provider.name()})[/bold]")
        db_ref = provider.ensure_secret(
            name=f"inji/{cfg.issuer_id}/db-secret",
            description=f"Database credentials for inji_{cfg.issuer_id}",
            placeholder={"username": f"inji_{cfg.issuer_id}_user", "password": "CHANGE_ME"},
        )
        outputs["db_secret_ref"] = db_ref

        if not cfg.data_api_secret_arn:
            placeholder = {
                "mtls":   {"client_cert": "CHANGE_ME", "client_key": "CHANGE_ME"},
                "oauth2": {"client_id": "CHANGE_ME", "client_secret": "CHANGE_ME"},
                "apikey": {"api_key": "CHANGE_ME"},
                "none":   {},
            }.get(cfg.data_api_auth_type, {})
            api_ref = provider.ensure_secret(
                name=f"inji/{cfg.issuer_id}/data-api-credentials",
                description=f"Data API credentials for {cfg.issuer_id} ({cfg.data_api_auth_type})",
                placeholder=placeholder,
            )
            cfg.data_api_secret_arn = api_ref
            outputs["data_api_secret_ref"] = api_ref

        hsm_ref = provider.ensure_secret(
            name=f"inji/{cfg.issuer_id}/softhsm-pin",
            description=f"SoftHSM security PIN for inji-certify {cfg.issuer_id}",
            placeholder={"security-pin": "CHANGE_ME_STRONG_RANDOM"},
        )
        outputs["hsm_secret_ref"] = hsm_ref

        # 4. Workload identity
        console.print(f"\n  [bold]4. Workload identity ({provider.name()})[/bold]")
        identity_ref = provider.ensure_workload_identity(cfg.issuer_id, ns, cfg)
        outputs["workload_identity_ref"] = identity_ref
        # Keep the legacy compat key only for real AWS IAM role ARNs.
        outputs["pod_identity_role_arn"] = identity_ref if provider.name() == "aws" and str(identity_ref).startswith("arn:") else ""

        # 5. DNS
        console.print(f"\n  [bold]5. DNS ({provider.name()})[/bold]")
        zone_id = provider.find_dns_zone(cfg.base_domain)
        outputs["dns_zone_id"] = zone_id or ""

        # 6. TLS certificate
        console.print(f"\n  [bold]6. TLS certificate ({provider.name()})[/bold]")
        cert_ref = provider.ensure_tls_certificate(cfg.base_domain)
        outputs["tls_cert_ref"] = cert_ref or ""

        # 7. RDS/DB schema note (always manual or via postgres-init Helm job)
        console.print("\n  [bold]7. Database schema[/bold]")
        outputs["db_name"] = f"inji_{cfg.issuer_id}"
        console.print(
            f"  [yellow]⚠[/yellow]  Database schema inji_{cfg.issuer_id} will be initialised\n"
            f"     by the postgres-init Helm job in Phase 3."
        )

    except Exception as exc:
        state.mark_failed("infra", str(exc))
        save_state(state)
        raise

    state.mark_done("infra", outputs)
    save_state(state)
    console.print(
        f"\n[green]Phase 1 complete — resources provisioned on {provider.name().upper()}.[/green]"
    )


def _print_dry_run(provider, issuer_id: str, cfg) -> None:
    t = Table(title=f"Resources that will be created ({provider.name().upper()})",
              show_header=True)
    t.add_column("Resource type")
    t.add_column("Name / identifier")
    for r_type, r_name in provider.dry_run_plan(issuer_id, cfg):
        t.add_row(r_type, r_name)
    console.print(t)
