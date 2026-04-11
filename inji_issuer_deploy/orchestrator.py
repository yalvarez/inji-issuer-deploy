from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from inji_issuer_deploy import state as st
from inji_issuer_deploy.cloud import CloudProviderConfig
from inji_issuer_deploy.phases import collect, config_gen, infra, k8s_deploy, register

PHASE_ORDER = list(st.PHASE_ORDER)
PHASE_LABELS = {
    "collect": "0 — data collection",
    "infra": "1 — infrastructure provisioning",
    "config_gen": "2 — configuration generation",
    "k8s_deploy": "3 — Kubernetes deployment",
    "register": "4 — credential registration",
}


def normalize_phase_choice(name: str | None) -> str | None:
    if not name:
        return None
    norm = st.normalize_phase_name(name)
    if norm not in PHASE_ORDER:
        raise ValueError("Use one of: collect, infra, config, deploy, register")
    return norm


def run_phase(name: str, state: st.DeployState, dry_run: bool = False) -> None:
    """Dispatch to the same phase engine used by the CLI."""
    norm = normalize_phase_choice(name)
    if norm == "collect":
        collect.run(state)
        state.mark_done("collect")
        st.save_state(state)
    elif norm == "infra":
        infra.run(state, dry_run=dry_run)
    elif norm == "config_gen":
        config_gen.run(state, dry_run=dry_run)
    elif norm == "k8s_deploy":
        k8s_deploy.run(state, dry_run=dry_run)
    elif norm == "register":
        register.run(state, dry_run=dry_run)


def phase_gate(state: st.DeployState, name: str) -> dict[str, Any]:
    """Return whether a phase is unlocked based on completion of prior phases."""
    norm = normalize_phase_choice(name)
    idx = PHASE_ORDER.index(norm)
    missing = [phase for phase in PHASE_ORDER[:idx] if not state.is_done(phase)]
    locked = len(missing) > 0
    locked_reason = ""
    if missing:
        locked_reason = f"Complete {PHASE_LABELS[missing[0]]} before continuing to {PHASE_LABELS[norm]}."
    return {
        "phase": norm,
        "locked": locked,
        "locked_reason": locked_reason,
        "missing": missing,
    }


def _phase_status(state: st.DeployState, name: str) -> dict[str, Any]:
    phase = state.phase(name)
    gate = phase_gate(state, name)
    if phase.completed:
        status = "complete"
    elif phase.error:
        status = "failed"
    elif phase.started_at:
        status = "in-progress"
    else:
        status = "pending"

    return {
        "name": name,
        "label": PHASE_LABELS[name],
        "status": status,
        "started_at": phase.started_at,
        "completed_at": phase.completed_at,
        "error": phase.error,
        "outputs": phase.outputs,
        "locked": gate["locked"],
        "locked_reason": gate["locked_reason"],
    }


def list_artifacts(state: st.DeployState) -> list[dict[str, str]]:
    issuer_id = state.issuer.issuer_id
    if not issuer_id:
        return []

    artifact_dir = Path(".inji-deploy") / issuer_id
    if not artifact_dir.exists():
        return []

    items: list[dict[str, str]] = []
    for path in sorted(p for p in artifact_dir.iterdir() if p.is_file()):
        items.append({"name": path.name, "path": str(path)})
    return items


def state_snapshot(state: st.DeployState) -> dict[str, Any]:
    return {
        "issuer": asdict(state.issuer),
        "provider_cfg": state.provider_cfg,
        "phases": [_phase_status(state, name) for name in PHASE_ORDER],
        "next_phase": state.first_incomplete(),
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "artifacts": list_artifacts(state),
    }


def update_state_from_payload(state: st.DeployState, payload: dict[str, Any]) -> st.DeployState:
    """Persist Phase 0 inputs from a web form while keeping the CLI schema as the source of truth."""
    issuer_fields = {f.name for f in fields(st.IssuerConfig)}
    provider_fields = {f.name for f in fields(CloudProviderConfig)}

    for key, value in payload.items():
        if value is None or key in {"provider", "provisioner"}:
            continue
        if key == "shared_configmaps" and isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if key in issuer_fields:
            setattr(state.issuer, key, value)

    current_provider = dict(getattr(state, "provider_cfg", {}) or {})
    provider_cfg = CloudProviderConfig(**current_provider)

    for key in provider_fields:
        if key in payload and payload[key] not in (None, ""):
            setattr(provider_cfg, key, payload[key])

    if payload.get("provider"):
        provider_cfg.provider = str(payload["provider"])
    if payload.get("provisioner"):
        provider_cfg.provisioner = str(payload["provisioner"])

    state.provider_cfg = asdict(provider_cfg)
    state.mark_done("collect")
    st.save_state(state)
    return state
