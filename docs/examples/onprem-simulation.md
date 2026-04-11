# On-prem simulation walkthrough

This walkthrough lets you exercise the on-prem path **without touching any real cloud**.

## 1. Use the example state

Copy the example state into a temporary file in the repo root:

```powershell
Copy-Item .\docs\examples\onprem-example-state.json .\demo-onprem-state.json
```

## 2. Review the simulated profile

It assumes:

- `provider=onprem`
- `provisioner=python`
- `secrets backend=k8s`
- `registry backend=plain`
- `mimoto` config via `ConfigMap`
- `cert-manager` via `ClusterIssuer/letsencrypt-prod`
- shared ConfigMaps from namespace `platform-shared`

Edit any values in `demo-onprem-state.json` to match your target environment.

## 3. Walk the full cycle in safe mode

### Phase 1 — infra dry run

```powershell
.\.venv\Scripts\python.exe -m inji_issuer_deploy.cli phase infra --dry-run --state-file demo-onprem-state.json
```

### Phase 2 — config generation

```powershell
.\.venv\Scripts\python.exe -m inji_issuer_deploy.cli phase config --state-file demo-onprem-state.json
```

Expected output directory:

```text
.inji-deploy/demo-onprem/
```

### Phase 3 — deploy dry run

```powershell
.\.venv\Scripts\python.exe -m inji_issuer_deploy.cli phase deploy --dry-run --state-file demo-onprem-state.json
```

This shows:

- which shared ConfigMaps would be copied
- which Helm charts would be used
- which namespace would be targeted
- how the mimoto config would be patched

## 4. What to inspect

After the simulation, verify:

- `.inji-deploy/demo-onprem/certify-demo-onprem.properties`
- `.inji-deploy/demo-onprem/helm-values-certify.yaml`
- `.inji-deploy/demo-onprem/helm-values-softhsm.yaml`
- `.inji-deploy/demo-onprem/k8s-configmap.yaml`
- `.inji-deploy/demo-onprem/mimoto-issuer-patch.json`

## 5. When you're ready for the first real on-prem install

Use the same flow, but point `kubectl` to your real cluster and replace the placeholders in Phase 0 or the state file.
