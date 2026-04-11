# First real on-prem runbook (Ubuntu VPS + k3s)

This runbook is the recommended path for the **first real installation** of `inji-issuer-deploy` in a controlled on-prem environment.

It assumes:

- an **Ubuntu VPS** will act as the operator host
- the same VPS may also host a **single-node `k3s`** cluster for the first controlled rollout
- the **CLI remains the deployment engine**; the web UI is optional

---

## 1. Target profile for the first real run

Use the simplest supported combination first:

- `provider=onprem`
- `provisioner=python`
- `onprem_registry_backend=plain`
- `onprem_secrets_backend=k8s`
- `mimoto` config via `ConfigMap`
- TLS via `cert-manager`

> Keep the first real installation intentionally simple. Harbor, Vault, MinIO, and Terraform can be added later after the baseline flow is proven.

---

## 2. Recommended topology

### Option A — best for first validation
- 1 Ubuntu VPS
- `k3s` installed on that VPS
- the repo, CLI, and optionally the web UI running from the same machine

### Option B — also supported
- 1 Ubuntu VPS as operator host only
- an existing Kubernetes cluster elsewhere
- `kubectl` on the VPS pointing to that cluster

---

## 3. Bootstrap the Ubuntu VPS

### Generate the bootstrap script from the repo

```bash
inji-issuer-deploy bootstrap ubuntu-onprem --dry-run --with-k3s --write-script ./bootstrap-ubuntu-onprem.sh
```

### Or run directly on Ubuntu

```bash
inji-issuer-deploy bootstrap ubuntu-onprem --with-k3s --no-dry-run
```

This prepares:

- Python and venv tooling
- `kubectl`
- `helm`
- MOSIP Helm repo
- optional `k3s`

---

## 4. Validate the host and cluster

Before touching the real deploy phases, verify the operator environment:

```bash
kubectl config current-context
kubectl cluster-info
helm version
inji-issuer-deploy preflight
```

You should not continue until the required checks are green or clearly understood.

---

## 5. Install `cert-manager` if needed

If the preflight reports that `cert-manager` is missing, install it first.

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --set crds.enabled=true
```

Then verify:

```bash
kubectl get pods -n cert-manager
kubectl get crd certificates.cert-manager.io
```

If you already have an internal CA or an existing `ClusterIssuer`, use that issuer name in Phase 0.

---

## 6. Prepare the minimum shared services

For the first real run, make sure these are available:

1. **PostgreSQL** reachable from the cluster
2. **`mimoto`** already deployed
3. the namespace holding shared configuration exists
4. the shared ConfigMaps expected by your environment exist

Example checks:

```bash
kubectl get ns
kubectl get ns mimoto
kubectl get ns platform-shared
kubectl get configmap -n platform-shared
```

If your environment does **not** use the RENIEC defaults, that is fine — just set the correct values in Phase 0.

---

## 7. Collect the issuer configuration

Start the normal flow:

```bash
inji-issuer-deploy phase collect
```

Recommended values for the first real install:

- provider: `onprem`
- provisioner: `python`
- registry backend: `plain`
- secrets backend: `k8s`
- cert issuer kind: `ClusterIssuer`
- cert issuer name: `letsencrypt-prod` or your internal issuer
- shared config source namespace: your actual source namespace
- shared ConfigMaps: only the ones your cluster really has

---

## 8. Run the deployment in controlled stages

### Stage 1 — readiness check

```bash
inji-issuer-deploy preflight
```

### Stage 2 — infrastructure dry run

```bash
inji-issuer-deploy phase infra --dry-run
```

### Stage 3 — generate config

```bash
inji-issuer-deploy phase config
```

Inspect the generated files under:

```text
.inji-deploy/<issuer_id>/
```

Important files to review:

- `certify-<issuer_id>.properties`
- `helm-values-certify.yaml`
- `helm-values-softhsm.yaml`
- `db-init-values.yaml`
- `mimoto-issuer-patch.json`

### Stage 4 — deploy dry run

```bash
inji-issuer-deploy phase deploy --dry-run
```

### Stage 5 — real execution

```bash
inji-issuer-deploy run
```

If something fails mid-run, fix the issue and rerun the same command. The state file allows the tool to resume.

---

## 9. Post-deploy verification checklist

After the first real run, verify at least:

```bash
kubectl get pods -A
kubectl get svc -A
kubectl get ingress -A
kubectl get certificate -A
```

And from the application side:

- the issuer namespace exists
- `inji-certify` pods are healthy
- SoftHSM is healthy
- the `.well-known` endpoint responds
- `mimoto` includes the new issuer entry

If DNS/TLS is already wired, also test:

```bash
curl -k https://<base-domain>/.well-known/openid-credential-issuer
```

---

## 10. Recovery guidance

If the first attempt does not complete:

1. run:
   ```bash
   inji-issuer-deploy status
   ```
2. fix the reported issue
3. rerun:
   ```bash
   inji-issuer-deploy preflight
   inji-issuer-deploy run
   ```

Do **not** reset the state unless you really want to start over.

---

## 11. What to postpone until after the first success

These are useful, but not necessary for the first production-style validation:

- Harbor registry integration
- Vault-backed secrets
- MinIO-backed `mimoto` config storage
- Terraform-managed infra around the cluster
- multi-node production hardening

The goal of this runbook is to get to the **first successful end-to-end on-prem install** with the smallest possible set of moving parts.
