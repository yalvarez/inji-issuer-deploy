from pathlib import Path


def test_first_real_onprem_runbook_exists_and_contains_core_steps():
    path = Path("docs/onprem-first-real-runbook.md")
    assert path.exists()

    text = path.read_text(encoding="utf-8")
    assert "bootstrap ubuntu-onprem" in text
    assert "inji-issuer-deploy preflight" in text
    assert "phase infra --dry-run" in text
    assert "phase deploy --dry-run" in text
    assert "k3s" in text.lower()
