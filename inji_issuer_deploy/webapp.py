from __future__ import annotations

import io
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from rich.console import Console as _RichConsole

from inji_issuer_deploy import state as st
from inji_issuer_deploy.cloud import CloudProviderConfig, preflight_report
from inji_issuer_deploy.orchestrator import (
    PHASE_LABELS,
    list_artifacts,
    normalize_phase_choice,
    phase_gate,
    run_phase,
    state_snapshot,
    update_state_from_payload,
)

WEBUI_DIR = Path(__file__).with_name("webui")


class PhaseRunRequest(BaseModel):
    dry_run: bool = False


class IssuerConfigPayload(BaseModel):
    issuer_id: str = ""
    issuer_name: str = ""
    issuer_description: str = ""
    issuer_logo_url: str = ""
    base_domain: str = ""
    aws_region: str = "sa-east-1"
    aws_account_id: str = ""
    eks_cluster_name: str = ""
    rds_host: str = ""
    rds_port: int = 5432
    rds_admin_secret_arn: str = ""
    idperu_jwks_uri: str = ""
    idperu_issuer_uri: str = ""
    data_api_base_url: str = ""
    data_api_auth_type: str = "mtls"
    data_api_secret_arn: str = ""
    data_api_token_url: str = ""
    scope_mappings: list[dict] = Field(default_factory=list)
    document_number_claim: str = "individualId"
    filiation_claim: str = ""
    mimoto_issuers_s3_bucket: str = ""
    mimoto_issuers_s3_key: str = "mimoto-issuers-config.json"
    mimoto_service_namespace: str = "mimoto"
    mimoto_service_name: str = "mimoto"
    shared_config_source_namespace: str = "config-server"
    shared_configmaps: list[str] = Field(default_factory=list)
    helm_repo_name: str = "mosip"
    helm_repo_url: str = "https://mosip.github.io/mosip-helm"
    certify_chart_ref: str = "mosip/inji-certify"
    postgres_init_chart_ref: str = "mosip/postgres-init"
    postgres_init_chart_version: str = "0.0.1-develop"
    softhsm_chart_ref: str = "mosip/softhsm"
    softhsm_namespace: str = "softhsm"
    certify_image: str = "mosipid/inji-certify-with-plugins:0.12.2"
    chart_version: str = "0.12.2"
    softhsm_chart_version: str = "1.3.0-beta.2"
    node_selector: dict = Field(default_factory=dict)
    provision_db: bool = False
    provider: str = "onprem"
    provisioner: str = "python"
    onprem_registry_backend: str = "plain"
    onprem_secrets_backend: str = "k8s"
    onprem_cert_issuer_name: str = "letsencrypt-prod"
    onprem_cert_issuer_kind: str = "ClusterIssuer"


@contextmanager
def use_state_file(state_file: str | None):
    previous = os.environ.get(st.STATE_FILE_ENV)
    try:
        if state_file:
            os.environ[st.STATE_FILE_ENV] = state_file
        yield
    finally:
        if previous is None:
            os.environ.pop(st.STATE_FILE_ENV, None)
        else:
            os.environ[st.STATE_FILE_ENV] = previous


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


@contextmanager
def _redirect_console(buffer: io.StringIO):
    """
    Temporarily redirect all module-level Rich consoles to write to *buffer*
    instead of the server's stdout/stderr.  This gives the Web UI access to
    every console.print() line emitted by phase/provider modules.
    """
    import inji_issuer_deploy.phases.k8s_deploy as _k8s
    import inji_issuer_deploy.phases.infra as _infra
    import inji_issuer_deploy.phases.config_gen as _cfg
    import inji_issuer_deploy.phases.register as _reg
    import inji_issuer_deploy.cloud as _cloud
    import inji_issuer_deploy.providers.onprem as _onprem
    import inji_issuer_deploy.providers.aws as _aws
    import inji_issuer_deploy.providers.azure as _azure
    import inji_issuer_deploy.providers.gcp as _gcp

    buf_console = _RichConsole(
        file=buffer,
        highlight=False,
        markup=True,
        force_terminal=False,
        no_color=True,
    )

    _modules = [_k8s, _infra, _cfg, _reg, _cloud, _onprem, _aws, _azure, _gcp]
    _originals = [(m, getattr(m, "console", None)) for m in _modules]

    for m, _ in _originals:
        if hasattr(m, "console"):
            m.console = buf_console
    try:
        yield
    finally:
        for m, orig in _originals:
            if orig is not None:
                m.console = orig


def create_app() -> FastAPI:
    app = FastAPI(
        title="inji-issuer-deploy UI",
        version="0.1.0",
        summary="Thin web layer over the inji-issuer-deploy CLI engine",
    )

    app.mount("/assets", StaticFiles(directory=WEBUI_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEBUI_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/phases")
    def phases() -> dict[str, list[dict[str, str]]]:
        return {
            "phases": [
                {"name": name, "label": label}
                for name, label in PHASE_LABELS.items()
            ]
        }

    @app.get("/api/state")
    def get_state(state_file: str | None = Query(default=None)) -> dict[str, Any]:
        with use_state_file(state_file):
            state = st.load_state()
            return state_snapshot(state)

    @app.post("/api/issuer-config")
    def save_issuer_config(
        payload: IssuerConfigPayload,
        state_file: str | None = Query(default=None),
    ) -> dict[str, Any]:
        with use_state_file(state_file):
            state = st.load_state()
            updated = update_state_from_payload(state, _model_dump(payload))
            return {
                "ok": True,
                "message": "Phase 0 inputs saved using the shared CLI state model.",
                "state": state_snapshot(updated),
            }

    @app.post("/api/preflight")
    def preflight_check(state_file: str | None = Query(default=None)) -> dict[str, Any]:
        with use_state_file(state_file):
            state = st.load_state()
            provider_cfg = CloudProviderConfig(**(state.provider_cfg or {}))
            if not provider_cfg.provider:
                provider_cfg.provider = "onprem"
            if not provider_cfg.provisioner:
                provider_cfg.provisioner = "python"
            return preflight_report(provider_cfg, state.issuer)

    @app.post("/api/run/phase/{phase_name}")
    def run_phase_endpoint(
        phase_name: str,
        request: PhaseRunRequest,
        state_file: str | None = Query(default=None),
    ) -> JSONResponse:
        if st.normalize_phase_name(phase_name) == "collect":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "Use /api/issuer-config for Phase 0 in the web UI.",
                },
            )

        try:
            normalize_phase_choice(phase_name)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        buffer = io.StringIO()
        with use_state_file(state_file):
            state = st.load_state()
            gate = phase_gate(state, phase_name)
            if gate["locked"]:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "phase": st.normalize_phase_name(phase_name),
                        "dry_run": request.dry_run,
                        "error": gate["locked_reason"],
                        "state": state_snapshot(state),
                    },
                )
            try:
                with _redirect_console(buffer):
                    run_phase(phase_name, state, dry_run=request.dry_run)
            except Exception as exc:
                return JSONResponse(
                    status_code=200,
                    content={
                        "ok": False,
                        "phase": st.normalize_phase_name(phase_name),
                        "dry_run": request.dry_run,
                        "error": str(exc),
                        "logs": buffer.getvalue(),
                        "state": state_snapshot(st.load_state()),
                    },
                )

            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "phase": st.normalize_phase_name(phase_name),
                    "dry_run": request.dry_run,
                    "logs": buffer.getvalue(),
                    "state": state_snapshot(st.load_state()),
                },
            )

    @app.get("/api/artifacts")
    def artifacts(state_file: str | None = Query(default=None)) -> dict[str, Any]:
        with use_state_file(state_file):
            state = st.load_state()
            return {"artifacts": list_artifacts(state)}

    @app.get("/api/artifacts/{artifact_name}")
    def artifact_contents(
        artifact_name: str,
        state_file: str | None = Query(default=None),
    ) -> PlainTextResponse:
        with use_state_file(state_file):
            state = st.load_state()
            if not state.issuer.issuer_id:
                return PlainTextResponse("No issuer configured yet.", status_code=404)

            artifact_dir = Path(".inji-deploy") / state.issuer.issuer_id
            artifact_path = (artifact_dir / artifact_name).resolve()
            if artifact_dir.resolve() not in artifact_path.parents and artifact_path != artifact_dir.resolve():
                return PlainTextResponse("Invalid artifact path.", status_code=400)
            if not artifact_path.exists() or not artifact_path.is_file():
                return PlainTextResponse("Artifact not found.", status_code=404)
            return PlainTextResponse(artifact_path.read_text(encoding="utf-8"))

    return app


app = create_app()
