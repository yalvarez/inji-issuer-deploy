import os

from fastapi.testclient import TestClient

from inji_issuer_deploy import state as st
from inji_issuer_deploy.webapp import create_app


def test_webapp_health_and_phase_metadata():
    client = TestClient(create_app())

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True}

    phases = client.get("/api/phases")
    assert phases.status_code == 200
    payload = phases.json()
    assert payload["phases"][0]["name"] == "collect"
    assert payload["phases"][1]["name"] == "infra"


def test_state_endpoint_returns_saved_state(tmp_path):
    state_file = tmp_path / "state.json"
    os.environ[st.STATE_FILE_ENV] = str(state_file)

    state = st.DeployState()
    state.issuer.issuer_id = "demo-onprem"
    state.issuer.issuer_name = "Demo OnPrem"
    state.issuer.base_domain = "demo.example.org"
    state.provider_cfg = {"provider": "onprem", "provisioner": "python"}
    state.mark_done("collect")
    st.save_state(state)

    client = TestClient(create_app())
    response = client.get("/api/state", params={"state_file": str(state_file)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["issuer"]["issuer_id"] == "demo-onprem"
    assert payload["provider_cfg"]["provider"] == "onprem"
    assert payload["phases"][0]["status"] == "complete"


def test_save_config_updates_state_and_marks_collect_done(tmp_path):
    state_file = tmp_path / "web-state.json"
    client = TestClient(create_app())

    payload = {
        "issuer_id": "gui-demo",
        "issuer_name": "GUI Demo",
        "base_domain": "gui.example.org",
        "issuer_description": "Dashboard-driven demo issuer",
        "provider": "onprem",
        "provisioner": "python",
        "data_api_base_url": "https://api.example.org",
        "idperu_jwks_uri": "https://id.example.org/jwks",
        "idperu_issuer_uri": "https://id.example.org/issuer",
    }

    response = client.post(
        "/api/issuer-config",
        params={"state_file": str(state_file)},
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    os.environ[st.STATE_FILE_ENV] = str(state_file)
    state = st.load_state()
    assert state.issuer.issuer_id == "gui-demo"
    assert state.issuer.issuer_name == "GUI Demo"
    assert state.provider_cfg["provider"] == "onprem"
    assert state.provider_cfg["provisioner"] == "python"
    assert state.is_done("collect") is True


def test_webapp_blocks_running_later_phase_before_prerequisites(tmp_path):
    state_file = tmp_path / "wizard-state.json"
    os.environ[st.STATE_FILE_ENV] = str(state_file)

    state = st.DeployState()
    state.issuer.issuer_id = "wizard-demo"
    state.provider_cfg = {"provider": "onprem", "provisioner": "python"}
    state.mark_done("collect")
    st.save_state(state)

    client = TestClient(create_app())
    response = client.post(
        "/api/run/phase/deploy",
        params={"state_file": str(state_file)},
        json={"dry_run": True},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "infrastructure" in payload["error"].lower() or "complete 1" in payload["error"].lower()
