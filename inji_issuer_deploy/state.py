"""
State manager.
Persists deployment progress to a JSON file so the tool can resume
after a failure without re-creating already-provisioned resources.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_FILE_ENV = "INJI_STATE_FILE"
DEFAULT_STATE_FILE = "inji-deploy-state.json"
PHASE_ORDER = ["collect", "infra", "config_gen", "k8s_deploy", "register"]
PHASE_ALIASES = {
    "collect": "collect",
    "infra": "infra",
    "aws_infra": "infra",
    "aws-infra": "infra",
    "config": "config_gen",
    "config_gen": "config_gen",
    "deploy": "k8s_deploy",
    "k8s_deploy": "k8s_deploy",
    "register": "register",
}


def normalize_phase_name(name: str) -> str:
    return PHASE_ALIASES.get(name, name)


@dataclass
class IssuerConfig:
    """All inputs collected in Phase 0."""
    issuer_id: str = ""                   # slug, e.g. "mtc"
    issuer_name: str = ""                 # display name, e.g. "Ministerio de Transportes"
    issuer_description: str = ""          # wallet description
    issuer_logo_url: str = ""             # URL to logo PNG/SVG
    base_domain: str = ""                 # e.g. "certify.mtc.gob.pe"
    aws_region: str = "sa-east-1"
    aws_account_id: str = ""
    eks_cluster_name: str = ""
    rds_host: str = ""                    # shared RDS endpoint
    rds_port: int = 5432
    rds_admin_secret_arn: str = ""        # Secrets Manager ARN for RDS admin creds
    idperu_jwks_uri: str = ""             # IDPeru JWKS endpoint
    idperu_issuer_uri: str = ""           # IDPeru issuer URI
    data_api_base_url: str = ""           # Issuer's own data API base URL
    data_api_auth_type: str = "mtls"      # "mtls" | "oauth2" | "apikey"
    data_api_secret_arn: str = ""         # ARN of secret containing API credentials
    data_api_token_url: str = ""          # Only for oauth2 auth type
    scope_mappings: list[dict] = field(default_factory=list)  # [{scope, profile, service}]
    document_number_claim: str = "individualId"  # IDPeru claim name for the national ID
    filiation_claim: str = ""             # IDPeru claim for filiation ID (empty = not needed)
    mimoto_issuers_s3_bucket: str = ""    # S3 bucket holding mimoto-issuers-config.json
    mimoto_issuers_s3_key: str = "mimoto-issuers-config.json"
    mimoto_service_namespace: str = "mimoto"
    mimoto_service_name: str = "mimoto"
    certify_image: str = "mosipid/inji-certify-with-plugins:0.12.2"
    chart_version: str = "0.12.2"
    softhsm_chart_version: str = "1.3.0-beta.2"
    node_selector: dict = field(default_factory=dict)


@dataclass
class PhaseStatus:
    completed: bool = False
    started_at: str = ""
    completed_at: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class DeployState:
    issuer: IssuerConfig = field(default_factory=IssuerConfig)
    provider_cfg: dict = field(default_factory=dict)  # CloudProviderConfig serialised
    phases: dict[str, PhaseStatus] = field(
        default_factory=lambda: {name: PhaseStatus() for name in PHASE_ORDER}
    )
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())

    # ── helpers ───────────────────────────────────────────────

    def phase(self, name: str) -> PhaseStatus:
        return self.phases[normalize_phase_name(name)]

    def mark_started(self, name: str) -> None:
        p = self.phase(name)
        p.started_at = _now()
        p.error = ""
        self.updated_at = _now()

    def mark_done(self, name: str, outputs: dict | None = None) -> None:
        p = self.phase(name)
        p.completed = True
        p.completed_at = _now()
        p.outputs = outputs or {}
        self.updated_at = _now()

    def mark_failed(self, name: str, error: str) -> None:
        self.phase(name).error = error
        self.updated_at = _now()

    def is_done(self, name: str) -> bool:
        return self.phase(name).completed

    def first_incomplete(self) -> str | None:
        for name in PHASE_ORDER:
            if not self.phase(name).completed:
                return name
        return None

    def output(self, phase: str, key: str, default: Any = None) -> Any:
        return self.phase(phase).outputs.get(key, default)


# ── persistence ───────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path() -> Path:
    return Path(os.environ.get(STATE_FILE_ENV, DEFAULT_STATE_FILE))


def load_state() -> DeployState:
    path = _state_path()
    if not path.exists():
        return DeployState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    state = DeployState()
    # issuer
    for k, v in raw.get("issuer", {}).items():
        if hasattr(state.issuer, k):
            setattr(state.issuer, k, v)
    # provider config
    state.provider_cfg = raw.get("provider_cfg", {})
    # phases
    for name, pd in raw.get("phases", {}).items():
        norm_name = normalize_phase_name(name)
        if norm_name in state.phases:
            ps = state.phases[norm_name]
            for k, v in pd.items():
                if hasattr(ps, k):
                    setattr(ps, k, v)
    state.created_at = raw.get("created_at", _now())
    state.updated_at = raw.get("updated_at", _now())
    return state


def save_state(state: DeployState) -> None:
    path = _state_path()
    path.write_text(json.dumps(asdict(state), indent=2, default=str), encoding="utf-8")


def reset_state() -> None:
    _state_path().unlink(missing_ok=True)
