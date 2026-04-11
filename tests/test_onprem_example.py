from __future__ import annotations

import json
from pathlib import Path


def test_onprem_example_state_is_valid():
    example = Path("docs/examples/onprem-example-state.json")
    payload = json.loads(example.read_text(encoding="utf-8"))

    assert payload["provider_cfg"]["provider"] == "onprem"
    assert payload["provider_cfg"]["provisioner"] == "python"
    assert payload["issuer"]["shared_config_source_namespace"] == "platform-shared"
    assert payload["issuer"]["shared_configmaps"] == ["global-config", "issuer-common"]
