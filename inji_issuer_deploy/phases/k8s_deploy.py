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
from rich.markup import escape as _markup_escape
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
        console.print(f"    [dim]{_markup_escape(stripped)}[/dim]")
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


def _dump_pod_logs(namespace: str, deployment: str) -> None:
    """Print recent pod logs from the NEWEST pod of a deployment (best-effort)."""
    console.print(f"\n  [yellow]Diagnostic — pod status and recent logs for {deployment}:[/yellow]")
    _run_streamed(["kubectl", "get", "pods", "-n", namespace,
                   "-l", f"app.kubernetes.io/instance={deployment}"], check=False)

    # Identify the newest pod by creation timestamp
    r = _run([
        "kubectl", "get", "pods", "-n", namespace,
        "-l", f"app.kubernetes.io/instance={deployment}",
        "--sort-by=.metadata.creationTimestamp",
        "-o", "jsonpath={.items[-1].metadata.name}",
    ], check=False)
    newest_pod = r.stdout.strip() if r.returncode == 0 else ""

    if newest_pod:
        console.print(f"\n  [dim]--- Logs from newest pod: {newest_pod} ---[/dim]")
        # Previous container crash logs (most useful in CrashLoopBackOff)
        _run_streamed([
            "kubectl", "logs", newest_pod, "-n", namespace, "--tail=80", "--previous",
        ], check=False)
        # Current (or last) container logs
        _run_streamed([
            "kubectl", "logs", newest_pod, "-n", namespace, "--tail=80",
        ], check=False)
    else:
        _run_streamed([
            "kubectl", "logs", f"deploy/{deployment}", "-n", namespace, "--tail=80",
        ], check=False)


def _wait_rollout(namespace: str, deployment: str, timeout: int = 1200) -> None:
    _step(f"waiting for {deployment} rollout ({timeout}s timeout)")
    try:
        _run_streamed([
            "kubectl", "rollout", "status", f"deployment/{deployment}",
            "-n", namespace, f"--timeout={timeout}s",
        ])
    except RuntimeError:
        _dump_pod_logs(namespace, deployment)
        raise
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
    """Copy a ConfigMap from one namespace to another.

    If the ConfigMap does not exist in the source namespace, a warning is
    printed and the function returns without error — the shared resource may
    simply not be present in this cluster and the deployment can continue.
    """
    r = _kubectl("get", "configmap", name, "-n", dest_ns, check=False)
    if r.returncode == 0:
        _skip(f"configmap {name} in {dest_ns}")
        return
    _step(f"copy configmap {name}: {src_ns} → {dest_ns}")
    get = _kubectl("get", "configmap", name, "-n", src_ns, "-o", "json", check=False)
    if get.returncode != 0:
        console.print(
            f"  [yellow]⚠[/yellow]  ConfigMap [bold]{name}[/bold] not found in "
            f"[bold]{src_ns}[/bold] — skipping copy. "
            "Create it manually if Certify requires it."
        )
        return
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


# ── step 2b/2c: on-prem infrastructure dependencies ──────────

def _is_incluster_host(host: str) -> bool:
    """True when host looks like a Kubernetes service name (no dots, not an IP)."""
    return bool(host) and "." not in host and any(c.isalpha() for c in host)


def _ensure_postgresql(cfg, ns: str, pg_manifest: Path) -> None:
    """Deploy an in-cluster PostgreSQL Deployment + Service for on-prem.

    Creates the 'postgres-postgresql' Secret (required by both the PostgreSQL
    container and the postgres-init Helm chart) before applying the manifest.
    Idempotent: skips if the Deployment already exists.
    """
    deploy_name = f"postgresql-{cfg.issuer_id}"
    r = _kubectl("get", "deployment", deploy_name, "-n", ns, check=False)
    if r.returncode == 0:
        _skip(f"postgresql deployment {deploy_name}")
        return

    _step(f"deploying in-cluster PostgreSQL ({deploy_name})")

    # Create superuser secret before the pod starts (postgres container reads it on init)
    import secrets as _sec
    pg_secret = "postgres-postgresql"
    r = _kubectl("get", "secret", pg_secret, "-n", ns, check=False)
    if r.returncode != 0:
        pg_password = _sec.token_urlsafe(16)
        _kubectl(
            "create", "secret", "generic", pg_secret,
            f"--from-literal=postgres-password={pg_password}",
            "-n", ns,
        )
        _ok(f"Secret {pg_secret} created in {ns}")

    _run(["kubectl", "apply", "-f", str(pg_manifest)])
    _run_streamed([
        "kubectl", "rollout", "status", f"deployment/{deploy_name}",
        "-n", ns, "--timeout=300s",
    ])
    _ok(f"PostgreSQL ready for {cfg.issuer_id}")


def _install_redis_chart(cfg, ns: str) -> None:
    """Install Redis using the configured Helm chart."""
    release = f"redis-{cfg.issuer_id}"
    if _helm_release_exists(ns, release):
        _skip(f"Redis release {release}")
        return

    _step(f"installing Redis chart {cfg.redis_chart_ref} ({cfg.redis_chart_version})")
    _run_streamed([
        "helm", "-n", ns,
        "install", release,
        cfg.redis_chart_ref,
        "--version", cfg.redis_chart_version,
        "--set", "architecture=standalone",
        "--set", "auth.enabled=false",
        "--set", f"master.service.name=redis-{cfg.issuer_id}",
        "--wait",
    ])
    _ok(f"Redis installed for {cfg.issuer_id}")


def _ensure_redis(cfg, ns: str, redis_manifest: Path) -> None:
    """Deploy an in-cluster Redis Deployment + Service for on-prem.

    Idempotent: skips if the Deployment already exists.
    """
    r = _kubectl("get", "deployment", "redis", "-n", ns, check=False)
    if r.returncode == 0:
        _skip(f"redis deployment in {ns}")
        return

    _step(f"deploying Redis in {ns}")
    _run(["kubectl", "apply", "-f", str(redis_manifest)])
    _run_streamed([
        "kubectl", "rollout", "status", "deployment/redis",
        "-n", ns, "--timeout=120s",
    ])
    _ok(f"Redis ready for {cfg.issuer_id}")


# ── step 3: DB init ──────────────────────────────────────────

def _init_db(cfg, ns: str, db_init_values: Path, provider, db_secret_ref: str | None = None) -> None:
    release = f"postgres-init-{cfg.issuer_id}"
    chart_ref = getattr(cfg, "postgres_init_chart_ref", "mosip/postgres-init")
    chart_version = getattr(cfg, "postgres_init_chart_version", "0.0.1-develop")
    if _helm_release_exists(ns, release):
        _skip(f"Helm release {release}")
        return

    # Read the app-user DB password directly from the k8s Secret we created in
    # the same phase (works on all providers; avoids dependency on the secret
    # store backend being configured correctly for on-prem).
    db_password = "CHANGE_ME"
    import base64
    r = _run(
        ["kubectl", "get", "secret", f"inji-{cfg.issuer_id}-db-secret",
         "-n", ns, "-o", "jsonpath={.data.password}"],
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            db_password = base64.b64decode(r.stdout.strip()).decode()
        except Exception:
            pass

    if db_password == "CHANGE_ME":
        # Fallback: read from the secret store backend
        secret_ref = db_secret_ref or f"inji/{cfg.issuer_id}/db-credentials"
        try:
            secret = provider.read_secret(secret_ref)
            db_password = secret.get("password", "CHANGE_ME")
        except Exception:
            console.print(
                "  [yellow]⚠[/yellow]  Could not read the DB password from k8s Secret or "
                "secret backend. Using placeholder — update it first."
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

def _helm_adopt(ns: str, kind: str, name: str, release: str) -> None:
    """
    Patch an existing resource with the Helm ownership labels/annotations so
    `helm install` can adopt it instead of failing with 'invalid ownership metadata'.
    """
    _kubectl(
        "label", kind, name, "-n", ns,
        "app.kubernetes.io/managed-by=Helm",
        "--overwrite", check=False,
    )
    _kubectl(
        "annotate", kind, name, "-n", ns,
        f"meta.helm.sh/release-name={release}",
        f"meta.helm.sh/release-namespace={ns}",
        "--overwrite", check=False,
    )


def _install_certify(cfg, ns: str,
                     certify_properties: Path,
                     helm_values: Path,
                     provider: str = "onprem") -> None:
    """Install or upgrade the inji-certify Helm release.

    Uses `helm upgrade --install` so that:
    - First run: installs the release.
    - Subsequent runs: upgrades with the current values, ensuring any
      configuration changes (new env vars, updated properties, etc.)
      are always applied without requiring a manual helm upgrade.
    """
    release = f"inji-certify-{cfg.issuer_id}"
    chart_ref = getattr(cfg, "certify_chart_ref", "mosip/inji-certify")
    upgrading = _helm_release_exists(ns, release)
    _step(f"{'upgrading' if upgrading else 'installing'} inji-certify {release}")

    # Adopt any pre-existing resources (e.g. SA created by Phase 1) into this release
    sa_name = getattr(cfg, "certify_service_account", f"inji-{cfg.issuer_id}-sa")
    _helm_adopt(ns, "serviceaccount", sa_name, release)

    # Apply the properties ConfigMap — use replace (delete+create) so changes
    # in the .properties file are always reflected even on re-runs.
    cm_name = f"certify-{cfg.issuer_id}-props"
    _run(["kubectl", "delete", "configmap", cm_name, "-n", ns], check=False)
    _run([
        "kubectl", "create", "configmap", cm_name,
        "-n", ns,
        f"--from-file=certify-{cfg.issuer_id}.properties={certify_properties}",
    ])

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
        "upgrade", "--install", release,
        chart_ref,
        "-f", str(helm_values),
        "--version", cfg.chart_version,
        *extra_sets,
    ])
    _wait_rollout(ns, f"inji-certify-{cfg.issuer_id}")
    _ok(f"inji-certify {'upgraded' if upgrading else 'installed'} for {cfg.issuer_id}")


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

    # Always derive out_dir from issuer_id — never from stored state paths,
    # which can be relative and break when CWD differs between runs.
    out_dir = Path(".inji-deploy") / cfg.issuer_id

    # Resolve generated file paths
    certify_props  = out_dir / f"certify-{cfg.issuer_id}.properties"
    helm_values    = out_dir / "helm-values-certify.yaml"
    softhsm_vals   = out_dir / "helm-values-softhsm.yaml"
    configmap_f    = out_dir / "k8s-configmap.yaml"
    mimoto_patch   = out_dir / "mimoto-issuer-patch.json"
    db_init_vals   = out_dir / "db-init-values.yaml"
    redis_manifest = out_dir / "k8s-redis.yaml"
    pg_manifest    = out_dir / "k8s-postgresql.yaml"

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

    import secrets
    try:
        _ensure_namespace(ns)

        # --- Ensure SoftHSM share ConfigMap exists ---
        softhsm_cm = f"softhsm-certify-{cfg.issuer_id}-share"
        r = _kubectl("get", "configmap", softhsm_cm, "-n", ns, check=False)
        if r.returncode != 0:
            # Try to copy from softhsm namespace, else create empty
            softhsm_ns = getattr(cfg, "softhsm_namespace", "softhsm")
            rsrc = _kubectl("get", "configmap", softhsm_cm, "-n", softhsm_ns, check=False)
            if rsrc.returncode == 0:
                _copy_configmap(softhsm_ns, ns, softhsm_cm)
            else:
                _step(f"creating empty ConfigMap {softhsm_cm} in {ns}")
                _kubectl("create", "configmap", softhsm_cm, "-n", ns)
                _ok(f"ConfigMap {softhsm_cm} created in {ns}")

        # 1. Copy shared ConfigMaps
        console.print("\n  [bold]1. Shared ConfigMaps[/bold]")
        shared_source_ns = getattr(cfg, "shared_config_source_namespace", "config-server")
        shared_configmaps = _shared_configmaps(cfg)
        if shared_configmaps:
            for cm_name in shared_configmaps:
                _copy_configmap(shared_source_ns, ns, cm_name)
        else:
            _skip("shared ConfigMaps")

        # 2. On-prem infrastructure dependencies (PostgreSQL + Redis)
        if provider_name == "onprem":
            console.print("\n  [bold]2. On-prem infrastructure (PostgreSQL + Redis)[/bold]")
            provision_db_flag = getattr(cfg, "provision_db", False)
            if provision_db_flag and _is_incluster_host(cfg.rds_host) and pg_manifest.exists():
                _ensure_postgresql(cfg, ns, pg_manifest)
            elif provision_db_flag and not _is_incluster_host(cfg.rds_host):
                _skip(f"PostgreSQL deployment (external host: {cfg.rds_host})")
            if redis_manifest.exists():
                _ensure_redis(cfg, ns, redis_manifest)
            else:
                console.print("  [yellow]⚠[/yellow]  k8s-redis.yaml not found — run 'phase config' first")

        # --- Ensure DB Secret exists if provisioning DB ---
        db_secret = f"inji-{cfg.issuer_id}-db-secret"
        provision_db = getattr(cfg, "provision_db", False)
        if provision_db:
            r = _kubectl("get", "secret", db_secret, "-n", ns, check=False)
            if r.returncode != 0:
                db_user = f"dbuser_{cfg.issuer_id}"
                db_pass = secrets.token_urlsafe(16)
                _step(f"creating Secret {db_secret} in {ns}")
                _kubectl(
                    "create", "secret", "generic", db_secret,
                    f"--from-literal=username={db_user}",
                    f"--from-literal=password={db_pass}",
                    "-n", ns
                )
                _ok(f"Secret {db_secret} created in {ns}")
                state.db_credentials = {"username": db_user, "password": db_pass, "secret_name": db_secret}
                save_state(state)

        # 3. Apply issuer ConfigMap
        console.print("\n  [bold]3. Issuer ConfigMap[/bold]")
        _apply_configmap(configmap_f, ns)

        # 4. DB init (only when provision_db is requested)
        console.print("\n  [bold]4. Database initialization[/bold]")
        provision_db = getattr(cfg, "provision_db", False)
        if provision_db:
            _init_db(
                cfg,
                ns,
                db_init_vals,
                provider,
                infra_outputs.get("db_secret_ref") or infra_outputs.get("db_secret_arn"),
            )
            outputs["db_initialized"] = True
        else:
            _skip("DB init (provision_db=false — using external PostgreSQL)")
            outputs["db_initialized"] = False

        # 5. SoftHSM
        console.print("\n  [bold]5. SoftHSM[/bold]")
        _install_softhsm(cfg, ns, softhsm_vals)
        outputs["softhsm_installed"] = True

        # 6. Certify
        console.print("\n  [bold]6. inji-certify[/bold]")
        _install_certify(cfg, ns, certify_props, helm_values, provider=provider_name)
        outputs["certify_installed"] = True
        outputs["certify_url"] = f"https://{cfg.base_domain}/v1/certify"

        # 7. Mimoto patch
        console.print("\n  [bold]7. Mimoto registration[/bold]")
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
    provision_db = getattr(cfg, "provision_db", False)
    pg_label = (
        f"kubectl apply k8s-postgresql.yaml (service: {cfg.rds_host})"
        if provision_db and _is_incluster_host(cfg.rds_host)
        else f"skipped (external host: {cfg.rds_host})"
    )
    rows = [
        ("1. ConfigMaps",    f"copy {shared_label} from {shared_source_ns} → {ns}"),
        ("2a. PostgreSQL",   pg_label),
        ("2b. Redis",        f"kubectl apply k8s-redis.yaml -n {ns}"),
        ("3. ConfigMap",     f"kubectl apply k8s-configmap.yaml -n {ns}"),
        ("4. DB init",       f"helm install postgres-init-{cfg.issuer_id} from {getattr(cfg, 'postgres_init_chart_ref', 'mosip/postgres-init')}"),
        ("5. SoftHSM",       f"helm install softhsm-certify-{cfg.issuer_id} in {getattr(cfg, 'softhsm_namespace', 'softhsm')}"),
        ("6. Certify",       f"helm install inji-certify-{cfg.issuer_id} from {getattr(cfg, 'certify_chart_ref', 'mosip/inji-certify')} v{cfg.chart_version}"),
        ("7. Mimoto",        f"Config-store patch + kubectl rollout restart {cfg.mimoto_service_name}"),
    ]
    for s, a in rows:
        t.add_row(s, a)
    console.print(t)
