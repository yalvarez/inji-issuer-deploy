"""
Phase 4 — credential registration and validation.

1. Waits for Certify to be healthy
2. Registers each credential configuration via POST /credential-configurations
3. Runs smoke tests:
   - GET /.well-known/openid-credential-issuer
   - GET mimoto /issuers (checks the new issuer appears)
4. Generates the final report
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from inji_issuer_deploy.state import DeployState, save_state

console = Console()

HEALTH_TIMEOUT_SECS = 300
HEALTH_POLL_INTERVAL = 10


# ── helpers ───────────────────────────────────────────────────

def _step(msg: str) -> None:
    console.print(f"  [cyan]→[/cyan] {msg}")


def _ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def _warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/yellow]  {msg}")


# ── health check ──────────────────────────────────────────────

def _wait_healthy(base_url: str) -> None:
    health_url = f"{base_url}/actuator/health"
    _step(f"waiting for Certify health check at {health_url}")
    deadline = time.time() + HEALTH_TIMEOUT_SECS
    last_err = ""
    while time.time() < deadline:
        try:
            r = httpx.get(health_url, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "UP":
                    _ok("Certify is healthy")
                    return
        except Exception as e:
            last_err = str(e)
        time.sleep(HEALTH_POLL_INTERVAL)
        console.print(f"  [dim]... still waiting ({int(deadline - time.time())}s left)[/dim]")
    raise TimeoutError(
        f"Certify did not become healthy within {HEALTH_TIMEOUT_SECS}s. "
        f"Last error: {last_err}"
    )


# ── credential config registration ───────────────────────────

def _build_credential_config(scope_mapping: dict, cfg) -> dict:
    """
    Builds a CredentialConfigurationDTO payload for one credential type.
    Mirrors the structure used by RENIEC — LDP-VC with Ed25519Signature2020.
    """
    scope = scope_mapping["scope"]
    display_name = scope_mapping.get("display_name", scope.replace("-", " ").title())

    # Build a minimal Velocity template for the VC
    # Consumers should customise this per credential type
    vc_template = _default_vc_template(scope, display_name, cfg)

    return {
        "credentialConfigKeyId": scope,
        "credentialFormat": "ldp_vc",
        "scope": scope,
        "contextURLs": [
            "https://www.w3.org/2018/credentials/v1",
            f"https://{cfg.base_domain}/vc/context/{scope}/v1",
        ],
        "credentialTypes": [
            "VerifiableCredential",
            f"{display_name.replace(' ', '')}Credential",
        ],
        "signatureCryptoSuite": "Ed25519Signature2020",
        "signatureAlgo": "Ed25519Signature2020",
        "didUrl": f"did:web:{cfg.base_domain.replace('.', ':')}",
        "metaDataDisplay": [{
            "name": display_name,
            "locale": "es",
        }],
        "displayOrder": ["id", "issuer", "issuanceDate"],
        "vcTemplate": _b64(vc_template),
        "credentialSubjectDefinition": _subject_definition(scope_mapping),
        "pluginConfigurations": [
            {"reniec_profile":     scope_mapping["profile"]},
            {"reniec_service":     scope_mapping["service"]},
            {"requires_filiation": str(scope_mapping.get("requires_filiation", False)).lower()},
        ],
    }


def _default_vc_template(scope: str, display_name: str, cfg) -> str:
    """Minimal Velocity template — customise per credential type."""
    return f"""{{
  "@context": [
    "https://www.w3.org/2018/credentials/v1",
    "https://{cfg.base_domain}/vc/context/{scope}/v1"
  ],
  "issuer": "${{{_issuer}}}",
  "type": ["VerifiableCredential", "{display_name.replace(' ', '')}Credential"],
  "issuanceDate": "${{{_validFrom}}}",
  "expirationDate": "${{{_validUntil}}}",
  "credentialSubject": {{
    "id": "${{{_holderId}}}"
  }}
}}"""


_issuer = "_issuer"
_validFrom = "validFrom"
_validUntil = "validUntil"
_holderId = "_holderId"


def _subject_definition(scope_mapping: dict) -> dict:
    """Returns a minimal credentialSubjectDefinition."""
    base = {
        "id": {
            "mandatory": True,
            "display": [{"name": "Identifier", "locale": "es"}],
        },
    }
    if scope_mapping.get("requires_filiation"):
        base["relatedPersonId"] = {
            "mandatory": True,
            "display": [{"name": "Persona relacionada", "locale": "es"}],
        }
    return base


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


def _register_credential(base_url: str, payload: dict) -> dict:
    """POST /credential-configurations. Returns the response body."""
    url = f"{base_url}/credential-configurations"
    r = httpx.post(url, json=payload, timeout=30, verify=False)
    if r.status_code == 409:
        # Already exists
        return {"status": "EXISTING", "id": payload["credentialConfigKeyId"]}
    r.raise_for_status()
    return r.json()


# ── smoke tests ───────────────────────────────────────────────

def _check_wellknown(base_url: str, issuer_id: str,
                     expected_scopes: list[str]) -> dict:
    url = f"{base_url}/.well-known/openid-credential-issuer"
    _step(f"GET {url}")
    try:
        r = httpx.get(url, timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
        supported = data.get("credential_configurations_supported", {})
        found_scopes = list(supported.keys())
        missing = [s for s in expected_scopes if s not in found_scopes]
        if missing:
            _warn(f".well-known missing scopes: {missing}")
        else:
            _ok(f".well-known OK — {len(supported)} credential type(s) registered")
        return {"status": "ok", "credential_types": found_scopes}
    except Exception as e:
        _fail(f".well-known check failed: {e}")
        return {"status": "error", "error": str(e)}


def _check_mimoto(mimoto_base_url: str, issuer_id: str) -> dict:
    url = f"{mimoto_base_url}/issuers"
    _step(f"GET {url}")
    try:
        # Some mimoto deployments reject the default TLS negotiation (TLSV1_ALERT_INTERNAL_ERROR).
        # Using a custom SSL context that forces TLS 1.2 and disables hostname/cert verification
        # covers both cases: plain verify=False and strict TLS version mismatches.
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        r = httpx.get(url, timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
        issuers = data.get("issuers", [])
        ids = [i.get("issuer_id") for i in issuers]
        if issuer_id in ids:
            _ok(f"issuer {issuer_id!r} visible in mimoto /issuers")
            return {"status": "ok", "visible": True}
        else:
            _warn(f"issuer {issuer_id!r} NOT yet visible in mimoto /issuers. "
                  f"Mimoto may still be reloading — retry in a few minutes.")
            return {"status": "warning", "visible": False}
    except ssl.SSLError as e:
        _fail(f"mimoto /issuers SSL handshake failed: {e}")
        _warn(
            "This usually means mimoto requires mTLS (client certificate) or has a "
            "TLS misconfiguration. Verify with: curl -v --insecure " + url
        )
        return {"status": "error", "error": str(e)}
    except Exception as e:
        _fail(f"mimoto /issuers check failed: {e}")
        return {"status": "error", "error": str(e)}


# ── report ────────────────────────────────────────────────────

def _print_report(cfg, results: dict) -> None:
    console.print("\n")
    console.print(Panel(
        f"[bold green]Issuer {cfg.issuer_id!r} deployment complete[/bold green]",
        border_style="green",
    ))

    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("Endpoint")
    t.add_column("URL")
    t.add_column("Status")

    certify_url = f"https://{cfg.base_domain}/v1/certify"
    rows = [
        ("Certify .well-known",
         f"{certify_url}/issuance/.well-known/openid-credential-issuer",
         results.get("wellknown", {}).get("status", "?"),
        ),
        ("Certify credential endpoint",
         f"{certify_url}/issuance/credential",
         "deployed",
        ),
        ("mimoto issuer listing",
         f"visible={results.get('mimoto', {}).get('visible', '?')}",
         results.get("mimoto", {}).get("status", "?"),
        ),
    ]
    for name, url, status in rows:
        status_str = (
            "[green]ok[/green]" if status == "ok" else
            "[yellow]warning[/yellow]" if status == "warning" else
            "[red]error[/red]"
        )
        t.add_row(name, url, status_str)
    console.print(t)

    if cfg.scope_mappings:
        s = Table(title="Registered credentials", show_header=True)
        s.add_column("Scope")
        s.add_column("Registration status")
        for m in cfg.scope_mappings:
            reg = results.get("registrations", {}).get(m["scope"], {})
            s_status = "[green]registered[/green]" if reg.get("status") in ("ACTIVE", "EXISTING") \
                       else "[red]failed[/red]"
            s.add_row(m["scope"], s_status)
        console.print(s)

    console.print("\n[bold]Next steps:[/bold]")
    console.print(
        "  1. [yellow]Update Secrets Manager secrets[/yellow] with real values:\n"
        f"     • inji/{cfg.issuer_id}/db-credentials\n"
        f"     • inji/{cfg.issuer_id}/data-api-credentials\n"
        f"     • inji/{cfg.issuer_id}/softhsm-pin\n"
    )
    console.print(
        "  2. [yellow]Validate DNS[/yellow] — ensure "
        f"{cfg.base_domain} resolves to the ALB.\n"
    )
    console.print(
        "  3. [yellow]Register IDPeru OIDC client[/yellow] — "
        f"add client_id=inji-wallet-{cfg.issuer_id} with\n"
        f"     allowed scopes: "
        + ", ".join(m["scope"] for m in cfg.scope_mappings) + "\n"
    )
    console.print(
        "  4. [yellow]Customise VC templates[/yellow] — the registered templates\n"
        "     contain only placeholder fields. Update them via:\n"
        f"     PUT {cfg.base_domain}/v1/certify/credential-configurations/{{scope}}\n"
    )
    console.print(
        "  5. [yellow]Test end-to-end[/yellow] — open the Inji wallet, "
        f"select {cfg.issuer_name!r}, and download a credential.\n"
    )


# ── main ─────────────────────────────────────────────────────

def run(state: DeployState, dry_run: bool = False) -> None:
    console.print(Panel(
        "[bold]Phase 4 — Credential registration and validation[/bold]",
        border_style="cyan",
    ))

    cfg = state.issuer
    certify_base = f"https://{cfg.base_domain}/v1/certify"
    if getattr(cfg, "mimoto_base_url", ""):
        mimoto_base = cfg.mimoto_base_url.rstrip("/")
    else:
        mimoto_domain = f"mimoto.{'.'.join(cfg.base_domain.split('.')[-2:])}"
        mimoto_base = f"https://{mimoto_domain}/v1/mimoto"

    if dry_run:
        console.print(f"[yellow]DRY RUN — would POST to {certify_base}/credential-configurations[/yellow]")
        for m in cfg.scope_mappings:
            console.print(f"  [dim]→ register scope: {m['scope']}[/dim]")
        return

    state.mark_started("register")
    results: dict[str, Any] = {}

    try:
        # 1. Wait for health
        console.print("\n  [bold]1. Health check[/bold]")
        try:
            _wait_healthy(certify_base)
        except TimeoutError as e:
            _warn(str(e))
            _warn("Proceeding with registration attempt anyway.")

        # 2. Register credential configurations
        console.print("\n  [bold]2. Registering credential configurations[/bold]")
        registrations: dict[str, dict] = {}
        for mapping in cfg.scope_mappings:
            scope = mapping["scope"]
            _step(f"registering scope {scope!r}")
            payload = _build_credential_config(mapping, cfg)
            try:
                resp = _register_credential(certify_base, payload)
                registrations[scope] = resp
                if resp.get("status") == "EXISTING":
                    console.print(f"  [dim]↷ {scope} already registered[/dim]")
                else:
                    _ok(f"scope {scope!r} registered → id={resp.get('id', '?')}")
            except Exception as e:
                registrations[scope] = {"status": "error", "error": str(e)}
                _fail(f"failed to register {scope!r}: {e}")
        results["registrations"] = registrations

        # 3. Smoke tests
        console.print("\n  [bold]3. Smoke tests[/bold]")
        expected_scopes = [m["scope"] for m in cfg.scope_mappings]
        results["wellknown"] = _check_wellknown(certify_base, cfg.issuer_id, expected_scopes)
        results["mimoto"]    = _check_mimoto(mimoto_base, cfg.issuer_id)

    except Exception as exc:
        state.mark_failed("register", str(exc))
        save_state(state)
        raise

    state.mark_done("register", results)
    save_state(state)

    # Print final report
    _print_report(cfg, results)
