"""
Phase 3 — Kubernetes deployment.

Deploys the Certify stack into the issuer's Kubernetes namespace using
the Helm charts and configuration generated in Phase 2.

Steps (in order):
  1. Copy shared ConfigMaps/Secrets from source namespaces
  2. Apply the issuer ConfigMap
  3. Init DB via postgres-init Helm chart
  4. Install SoftHSM via Helm
  5. Install inji-certify via Helm
  6. Patch mimoto-issuers-config.json in the configured object store and trigger rollout
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from inji_issuer_deploy.cloud import CloudProviderConfig, get_provider
from inji_issuer_deploy.state import DeployState, save_state

console = Console()


# ── helpers ───────────────────────────────────────────────────

def _run(cmd: list[str], check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _run_streamed(cmd: list[str], check: bool = True) -> int:
    """Run a command, streaming its output directly to the console in real time."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    collected: list[str] = []
    for line in proc.stdout:
        stripped = line.rstrip()
        collected.append(stripped)
        console.print(f"    [dim]{stripped}[/dim]")
    proc.wait()
    if check and proc.returncode != 0:
        # Include the last 40 lines of output so the caller can see what failed
        tail = "\n".join(collected[-40:]) if collected else "<no output>"
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n\n"
            f"Output:\n{tail}"
        )
    return proc.returncode


def _helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["helm", *args], check=check)


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["kubectl", *args], check=check)


def _step(msg: str) -> None:
    console.print(f"  [cyan]→[/cyan] {msg}")


def _ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _skip(msg: str) -> None:
    console.print(f"  [dim]↷ {msg} — already installed, skipping[/dim]")


def _wait_rollout(namespace: str, deployment: str, timeout: int = 300) -> None:
    _step(f"waiting for {deployment} rollout ({timeout}s timeout)")
    _run_streamed([
        "kubectl", "rollout", "status", f"deployment/{deployment}",
        "-n", namespace, f"--timeout={timeout}s",
    ])
    _ok(f"{deployment} is ready")


def _helm_release_exists(namespace: str, release: str) -> bool:
    r = _helm("status", release, "-n", namespace, check=False)
    return r.returncode == 0


def _ensure_namespace(ns: str) -> None:
    r = _kubectl("get", "namespace", ns, check=False)
    if r.returncode == 0:
        return
    _kubectl("create", "namespace", ns, check=False)
    _kubectl("label", "namespace", ns, "istio-injection=enabled", "--overwrite", check=False)


def _resolve_provider(state: DeployState, cfg):
    raw_pc = getattr(state, "provider_cfg", None) or {}
    provider_cfg = CloudProviderConfig(**raw_pc) if isinstance(raw_pc, dict) else raw_pc
    if not provider_cfg.provider:
        provider_cfg.provider = "aws"
    return get_provider(provider_cfg, cfg)


def _cfg_list(value, default: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(default or [])


def _shared_configmaps(cfg) -> list[str]:
    return _cfg_list(
        getattr(cfg, "shared_configmaps", None),
        default=["artifactory-share", "config-server-share"],
    )


# ── step 1: copy shared configmaps ───────────────────────────

def _copy_configmap(src_ns: str, dest_ns: str, name: str) -> None:
    """Copy a ConfigMap from one namespace to another."""
    r = _kubectl("get", "configmap", name, "-n", dest_ns, check=False)
    if r.returncode == 0:
        _skip(f"configmap {name} in {dest_ns}")
        return
    _step(f"copy configmap {name}: {src_ns} → {dest_ns}")
    get = _kubectl("get", "configmap", name, "-n", src_ns, "-o", "json")
    data = json.loads(get.stdout)
    # Strip source-namespace metadata
    data["metadata"] = {
        "name": data["metadata"]["name"],
        "namespace": dest_ns,
    }
    import subprocess as sp
    proc = sp.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(data),
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to copy configmap {name}: {proc.stderr}")
    _ok(f"configmap {name} copied to {dest_ns}")


def _copy_secret(src_ns: str, dest_ns: str, name: str) -> None:
    """Copy a Secret from one namespace to another."""
    r = _kubectl("get", "secret", name, "-n", dest_ns, check=False)
    if r.returncode == 0:
        _skip(f"secret {name} in {dest_ns}")
        return
    _step(f"copy secret {name}: {src_ns} → {dest_ns}")
    get = _kubectl("get", "secret", name, "-n", src_ns, "-o", "json")
    data = json.loads(get.stdout)
    data["metadata"] = {
        "name": data["metadata"]["name"],
        "namespace": dest_ns,
    }
    import subprocess as sp
    proc = sp.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(data),
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to copy secret {name}: {proc.stderr}")
    _ok(f"secret {name} copied to {dest_ns}")


# ── step 2: apply issuer configmap ───────────────────────────

def _apply_configmap(cfg_file: Path, ns: str) -> None:
    _step(f"applying ConfigMap to namespace {ns}")
    r = _run(["kubectl", "apply", "-f", str(cfg_file), "-n", ns])
    _ok("ConfigMap applied")


# ── step 3: DB init ──────────────────────────────────────────

def _init_db(cfg, ns: str, db_init_values: Path, provider, db_secret_ref: str | None = None) -> None:
    release = f"postgres-init-{cfg.issuer_id}"
    chart_ref = getattr(cfg, "postgres_init_chart_ref", "mosip/postgres-init")
    chart_version = getattr(cfg, "postgres_init_chart_version", "0.0.1-develop")
    if _helm_release_exists(ns, release):
        _skip(f"Helm release {release}")
        return
    _step(f"installing DB init Helm release {release}")
    secret_ref = db_secret_ref or f"inji/{cfg.issuer_id}/db-credentials"
    try:
        secret = provider.read_secret(secret_ref)
        db_password = secret.get("password", "CHANGE_ME")
    except Exception:
        db_password = "CHANGE_ME"
        console.print(
            "  [yellow]⚠[/yellow]  Could not read the DB password from the configured secret backend. "
            "Using placeholder — update it first."
        )

    _run_streamed([
        "helm", "-n", ns,
        "install", release,
        chart_ref,
        "-f", str(db_init_values),
        "--version", chart_version,
        "--set", f"dbUserPasswords.dbuserPassword={db_password}",
        "--wait", "--wait-for-jobs",
        "--timeout", "300s",
    ])
    _ok(f"DB init complete for inji_{cfg.issuer_id}")


# ── step 4: SoftHSM ──────────────────────────────────────────

def _install_softhsm(cfg, ns: str, softhsm_values: Path) -> None:
    softhsm_ns = getattr(cfg, "softhsm_namespace", "softhsm")
    chart_ref = getattr(cfg, "softhsm_chart_ref", "mosip/softhsm")
    release = f"softhsm-certify-{cfg.issuer_id}"
    if _helm_release_exists(softhsm_ns, release):
        _skip(f"SoftHSM release {release}")
        return
    _step(f"installing SoftHSM {release}")
    # Ensure namespace exists
    _kubectl("create", "namespace", softhsm_ns, check=False)
    _kubectl("label", "namespace", softhsm_ns,
             "istio-injection=enabled", "--overwrite", check=False)
    _run_streamed([
        "helm", "-n", softhsm_ns,
        "install", release,
        chart_ref,
        "-f", str(softhsm_values),
        "--version", cfg.softhsm_chart_version,
        "--wait",
    ])
    # Share the SoftHSM secret with the certify namespace
    _copy_secret(softhsm_ns, ns, f"softhsm-certify-{cfg.issuer_id}")
    _ok(f"SoftHSM installed for {cfg.issuer_id}")


# ── step 5: inji-certify ─────────────────────────────────────

def _install_certify(cfg, ns: str,
                     certify_properties: Path,
                     helm_values: Path,
                     provider: str = "onprem") -> None:
    release = f"inji-certify-{cfg.issuer_id}"
    chart_ref = getattr(cfg, "certify_chart_ref", "mosip/inji-certify")
    if _helm_release_exists(ns, release):
        _skip(f"inji-certify release {release}")
        return
    _step(f"installing inji-certify {release}")

    # Store the properties file as a ConfigMap in the namespace
    r = _run([
        "kubectl", "create", "configmap",
        f"certify-{cfg.issuer_id}-props",
        "-n", ns,
        f"--from-file=certify-{cfg.issuer_id}.properties={certify_properties}",
    ], check=False)
    if r.returncode != 0 and "already exists" not in (r.stderr or ""):
        raise RuntimeError(f"Failed to create certify configmap: {r.stderr}")

    # Safety-net --set overrides so even a stale values file works correctly
    extra_sets: list[str] = []
    if provider == "onprem":
        extra_sets += [
            "--set", "istio.enabled=false",
            "--set", "metrics.serviceMonitor.enabled=false",
        ]
    else:
        extra_sets += ["--set", f"istio.hosts[0]={cfg.base_domain}"]

    _run_streamed([
        "helm", "-n", ns,
        "install", release,
        chart_ref,
        "-f", str(helm_values),
        "--version", cfg.chart_version,
        *extra_sets,
    ])
    _wait_rollout(ns, f"inji-certify-{cfg.issuer_id}")
    _ok(f"inji-certify installed for {cfg.issuer_id}")


# ── step 6: mimoto patch ─────────────────────────────────────

def _patch_mimoto(cfg, mimoto_patch_file: Path, provider) -> None:
    """
    1. Download current mimoto-issuers-config.json from the configured object store
    2. Add the new issuer entry (if not already present)
    3. Upload updated config back
    4. Trigger mimoto pod rollout to reload config
    """
    _step("patching mimoto-issuers-config.json")

    # Download current config
    try:
        current_config = provider.read_config_file(
            cfg.mimoto_issuers_s3_bucket,
            cfg.mimoto_issuers_s3_key,
        )
    except Exception as e:
        raise RuntimeError(
            f"Could not read mimoto config from {cfg.mimoto_issuers_s3_bucket}/"
            f"{cfg.mimoto_issuers_s3_key}: {e}"
        )

    # Check if issuer already registered
    existing_ids = [i.get("issuer_id") for i in current_config.get("issuers", [])]
    if cfg.issuer_id in existing_ids:
        _skip(f"issuer {cfg.issuer_id} already in mimoto config")
        return

    # Add new entry
    new_entry = json.loads(mimoto_patch_file.read_text())
    current_config.setdefault("issuers", []).append(new_entry)

    # Upload updated config
    provider.write_config_file(
        cfg.mimoto_issuers_s3_bucket,
        cfg.mimoto_issuers_s3_key,
        current_config,
    )
    _ok(f"mimoto config updated in {cfg.mimoto_issuers_s3_bucket}/{cfg.mimoto_issuers_s3_key}")

    # Trigger mimoto rollout to pick up new config
    _step(f"triggering mimoto rollout in namespace {cfg.mimoto_service_namespace}")
    _kubectl(
        "rollout", "restart",
        f"deployment/{cfg.mimoto_service_name}",
        "-n", cfg.mimoto_service_namespace,
        check=False,
    )
    _ok("mimoto rollout triggered")


# ── main ─────────────────────────────────────────────────────

def run(state: DeployState, dry_run: bool = False) -> None:
    console.print(Panel(
        "[bold]Phase 3 — Kubernetes deployment[/bold]",
        border_style="cyan",
    ))

    cfg = state.issuer
    ns = f"inji-{cfg.issuer_id}"
    provider = _resolve_provider(state, cfg)
    infra_outputs = state.phase("infra").outputs
    gen_outputs = state.phase("config_gen").outputs
    out_dir = Path(gen_outputs.get(
        f"certify-{cfg.issuer_id}.properties",
        f".inji-deploy/{cfg.issuer_id}/certify-{cfg.issuer_id}.properties"
    )).parent

    # Resolve generated file paths
    certify_props = out_dir / f"certify-{cfg.issuer_id}.properties"
    helm_values   = out_dir / "helm-values-certify.yaml"
    softhsm_vals  = out_dir / "helm-values-softhsm.yaml"
    configmap_f   = out_dir / "k8s-configmap.yaml"
    mimoto_patch  = out_dir / "mimoto-issuer-patch.json"
    db_init_vals  = out_dir / "db-init-values.yaml"

    if dry_run:
        _print_dry_run(cfg, ns)
        return

    # Add Helm repo used by the configured chart refs
    repo_name = getattr(cfg, "helm_repo_name", "mosip")
    repo_url = getattr(cfg, "helm_repo_url", "https://mosip.github.io/mosip-helm")
    if repo_name and repo_url:
        _step(f"adding Helm repository {repo_name}")
        add_rc = _run_streamed(["helm", "repo", "add", repo_name, repo_url, "--force-update"], check=False)
        if add_rc not in (0, 1):  # 1 = already exists with same URL, harmless
            raise RuntimeError(f"helm repo add {repo_name} failed (exit {add_rc})")
        _step(f"updating Helm repository {repo_name}")
        _run_streamed(["helm", "repo", "update", repo_name])
        _ok("Helm repo updated")

    state.mark_started("k8s_deploy")
    outputs: dict = {}

    raw_pc = getattr(state, "provider_cfg", None) or {}
    provider_name = raw_pc.get("provider", "onprem") if isinstance(raw_pc, dict) else getattr(raw_pc, "provider", "onprem")

    try:
        _ensure_namespace(ns)

        # 1. Copy shared ConfigMaps
        console.print("\n  [bold]1. Shared ConfigMaps[/bold]")
        shared_source_ns = getattr(cfg, "shared_config_source_namespace", "config-server")
        shared_configmaps = _shared_configmaps(cfg)
        if shared_configmaps:
            for cm_name in shared_configmaps:
                _copy_configmap(shared_source_ns, ns, cm_name)
        else:
            _skip("shared ConfigMaps")

        # 2. Apply issuer ConfigMap
        console.print("\n  [bold]2. Issuer ConfigMap[/bold]")
        _apply_configmap(configmap_f, ns)

        # 3. DB init
        console.print("\n  [bold]3. Database initialization[/bold]")
        _init_db(
            cfg,
            ns,
            db_init_vals,
            provider,
            infra_outputs.get("db_secret_ref") or infra_outputs.get("db_secret_arn"),
        )
        outputs["db_initialized"] = True

        # 4. SoftHSM
        console.print("\n  [bold]4. SoftHSM[/bold]")
        _install_softhsm(cfg, ns, softhsm_vals)
        outputs["softhsm_installed"] = True

        # 5. Certify
        console.print("\n  [bold]5. inji-certify[/bold]")
        _install_certify(cfg, ns, certify_props, helm_values, provider=provider_name)
        outputs["certify_installed"] = True
        outputs["certify_url"] = f"https://{cfg.base_domain}/v1/certify"

        # 6. Mimoto patch
        console.print("\n  [bold]6. Mimoto registration[/bold]")
        _patch_mimoto(cfg, mimoto_patch, provider)
        outputs["mimoto_patched"] = True

    except Exception as exc:
        state.mark_failed("k8s_deploy", str(exc))
        save_state(state)
        raise

    state.mark_done("k8s_deploy", outputs)
    save_state(state)
    console.print(f"\n[green]Phase 3 complete — Certify running at "
                  f"https://{cfg.base_domain}/v1/certify[/green]")


def _print_dry_run(cfg, ns: str) -> None:
    from rich.table import Table
    t = Table(title="Kubernetes operations (dry run)", show_header=True)
    t.add_column("Step")
    t.add_column("Action")

    shared_source_ns = getattr(cfg, "shared_config_source_namespace", "config-server")
    shared_configmaps = _shared_configmaps(cfg)
    shared_label = ", ".join(shared_configmaps) if shared_configmaps else "<none>"
    rows = [
        ("1. ConfigMaps",  f"copy {shared_label} from {shared_source_ns} → {ns}"),
        ("2. ConfigMap",   f"kubectl apply k8s-configmap.yaml -n {ns}"),
        ("3. DB init",     f"helm install postgres-init-{cfg.issuer_id} from {getattr(cfg, 'postgres_init_chart_ref', 'mosip/postgres-init')}"),
        ("4. SoftHSM",     f"helm install softhsm-certify-{cfg.issuer_id} in {getattr(cfg, 'softhsm_namespace', 'softhsm')}"),
        ("5. Certify",     f"helm install inji-certify-{cfg.issuer_id} from {getattr(cfg, 'certify_chart_ref', 'mosip/inji-certify')} v{cfg.chart_version}"),
        ("6. Mimoto",      f"Config-store patch + kubectl rollout restart {cfg.mimoto_service_name}"),
    ]
    for s, a in rows:
        t.add_row(s, a)
    console.print(t)
