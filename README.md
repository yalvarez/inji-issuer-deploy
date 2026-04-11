# inji-issuer-deploy

CLI tool to deploy a new Inji VC issuer that replicates the RENIEC production stack on Kubernetes, with support for AWS/EKS and on-premise environments.

## What it does

Given a set of configuration inputs, the tool provisions all infrastructure and deploys all services needed for a new credential issuer:

| Phase | What it does |
|-------|-------------|
| **0 — collect** | Interactive CLI collects issuer config (domain, IDPeru endpoints, data API, credential types) |
| **1 — infra** | Provisions namespace, registries, secrets, workload identity, DNS/TLS using the selected provider or a Terraform handoff |
| **2 — config** | Renders `certify-{id}.properties`, Helm values, K8s ConfigMaps, mimoto issuer entry from templates |
| **3 — deploy** | Helm install: DB init → SoftHSM → inji-certify. Patches `mimoto-issuers-config.json` in S3 |
| **4 — register** | POSTs credential configurations to Certify API. Runs smoke tests against `.well-known` and mimoto |

All phases are **idempotent** — safe to re-run. State is persisted to `inji-deploy-state.json`.

## Prerequisites

### Recommended first real install: on-prem MVP

- Python 3.11+
- `kubectl` pointing to your target Kubernetes cluster
- `helm` with the MOSIP repo (`helm repo add mosip https://mosip.github.io/mosip-helm`)
- A shared PostgreSQL endpoint already available
- A running `mimoto` deployment and its config source (`ConfigMap` or `MinIO`)
- `cert-manager` installed in the cluster
- Optional: Harbor, Vault, and/or MinIO if you want those backends

### Cloud path (secondary for now)

- AWS CLI configured with credentials for the target account
- Existing RENIEC-pattern stack on EKS

## Installation

```bash
pip install inji-issuer-deploy
```

Or from source:

```bash
git clone https://github.com/IUGO-RENIEC/inji-issuer-deploy
cd inji-issuer-deploy
pip install -e .
```

## Quick start

### Web UI (MVP)

The CLI remains the **source of truth**. The new web dashboard is only a thin operational layer over the same Python phase engine and `inji-deploy-state.json`, so future cloud support stays CLI-first as well.

```bash
pip install -e .
inji-issuer-deploy web
```

Then open:

```text
http://127.0.0.1:8000
```

In the MVP:
- **Phase 0** is handled through a form in the browser
- **Phases 1–4** call the same backend logic used by the CLI
- the current state and generated artifacts remain visible in one place

### On-prem MVP (recommended)

```bash
# Optional: prepare an Ubuntu VPS as the operator host or a single-node k3s lab
inji-issuer-deploy bootstrap ubuntu-onprem --dry-run

# First run: choose provider=onprem and provisioner=python in Phase 0
inji-issuer-deploy phase collect
inji-issuer-deploy phase infra --dry-run
inji-issuer-deploy phase config --dry-run
inji-issuer-deploy run
```

If you want to rehearse the cycle before the first real deployment, use the guides in:

```text
docs/examples/onprem-simulation.md
docs/onprem-ubuntu-vps.md
docs/onprem-first-real-runbook.md
```

### General workflow

```bash
# Full interactive deployment
inji-issuer-deploy run

# Preview without making changes
inji-issuer-deploy run --dry-run

# Validate readiness before a real deployment
inji-issuer-deploy preflight

# Check status of an in-progress deployment
inji-issuer-deploy status

# Resume a failed deployment
inji-issuer-deploy run

# Run a single phase
inji-issuer-deploy phase collect
inji-issuer-deploy phase infra --dry-run
# legacy alias still supported:
inji-issuer-deploy phase aws-infra --dry-run
inji-issuer-deploy phase config
inji-issuer-deploy phase deploy
inji-issuer-deploy phase register

# Start over
inji-issuer-deploy reset
```

## Configuration inputs (Phase 0)

The tool asks for:

| Input | Example | Notes |
|-------|---------|-------|
| Issuer ID | `mtc` | Slug used in all resource names |
| Display name | `Ministerio de Transportes` | Shown in the wallet |
| Base domain | `certify.mtc.gob.pe` | Certify will be exposed here |
| AWS account ID | `123456789012` | The issuer's own account |
| EKS cluster name | `INJI-prod` | Existing shared cluster |
| RDS host | `inji-prod.xxxx.rds.amazonaws.com` | Shared RDS endpoint |
| IDPeru JWKS URI | `https://idperu.gob.pe/.../.well-known/jwks.json` | Token validation |
| IDPeru issuer URI | `https://idperu.gob.pe/v1/idperu` | |
| IDPeru claim name | `individualId` | Claim in the token with the citizen's DNI |
| Data API base URL | `https://api.licencias.mtc.gob.pe` | Issuer's own data source |
| Data API auth type | `mtls` / `oauth2` / `apikey` | |
| Credential types | one or more scopes + profiles | e.g. `licencia-conducir` → `LICENCIA_B` |

## On-prem MVP profile

For the first real on-prem installation, the simplest supported combination is:

- **provider:** `onprem`
- **provisioner:** `python`
- **registry backend:** `plain` or `harbor`
- **secrets backend:** `k8s`
- **mimoto config backend:** `ConfigMap` first, `MinIO` later if needed
- **TLS:** `cert-manager` using either `ClusterIssuer/letsencrypt-prod` or your internal CA

This avoids any dependency on public cloud credentials and completes the full operator cycle inside Kubernetes.

## Provisioning model

The recommended operating mode is now **hybrid**:

- **Terraform** for cloud infrastructure provisioning and stateful resources
- **`inji-issuer-deploy`** for interactive input collection, config rendering, Helm deployment, registration, and smoke tests

If Phase 0 is configured with `provisioner=terraform`, Phase 1 will generate:

```text
.inji-deploy/{issuer_id}/terraform.tfvars.json
```

Use that file from the `terraform/` folder, then re-run `inji-issuer-deploy phase infra` (or `inji-issuer-deploy run`) so the CLI can import the Terraform outputs and continue.

For AWS, the Terraform bootstrap now supports optional automation for:
- `Route53` hosted zone lookup / DNS records
- `ACM` certificate creation and DNS validation
- placeholder `Secrets Manager` entries that are safe to update manually later

## Architecture context

```
IDPeru (shared auth)
  ↓ issues access tokens with citizen DNI
Certify {issuer} (per-issuer, isolated)
  ↓ validates token against IDPeru JWKS
  ↓ passes claims to data provider plugin
Data API {issuer}
  ↓ returns identity data for the DNI
Certify signs VC → returns to wallet

mimoto (shared directory)
  ↓ lists all issuers for the wallet
inji-wallet → shows all available issuers
```

Each issuer runs in its own EKS namespace with its own database schema,
Secrets Manager secrets, and IAM role. The wallet and mimoto are shared.

## After deployment — manual steps

The tool will remind you, but these must be done manually:

1. **Update Secrets Manager** — fill in the real values for:
   - `inji/{id}/db-credentials` — DB password
   - `inji/{id}/data-api-credentials` — cert/key or client secret
   - `inji/{id}/softhsm-pin` — strong random PIN for the HSM

2. **DNS** — point `{base_domain}` to the ALB endpoint

3. **IDPeru registration** — register the new OIDC client with IDPeru:
   - `client_id`: `inji-wallet-{issuer_id}`
   - Allowed scopes: the scopes you defined in Phase 0

4. **Customise VC templates** — the registered templates contain minimal
   placeholder fields. Update them via the Certify API:
   ```
   PUT https://{base_domain}/v1/certify/credential-configurations/{scope}
   ```

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

## License

MPL-2.0
