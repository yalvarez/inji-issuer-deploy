# Ubuntu VPS on-prem bootstrap

This project now supports using an **Ubuntu VPS** as the operator host for the on-prem path.

There are two supported modes:

1. **Operator host only** — the VPS runs the CLI/UI, `kubectl`, and `helm` against an existing Kubernetes cluster.
2. **Single-node lab** — the VPS also installs `k3s` for a lightweight on-prem rehearsal environment.

> The design remains **CLI-first**. The VPS bootstrap only prepares dependencies and cluster access so the same `inji-issuer-deploy` engine can run there.

## Quick start

### Dry-run the bootstrap plan

```bash
inji-issuer-deploy bootstrap ubuntu-onprem --dry-run
```

### Generate a script for the VPS

```bash
inji-issuer-deploy bootstrap ubuntu-onprem --dry-run --with-k3s --write-script ./bootstrap-ubuntu-onprem.sh
```

Copy that script to the Ubuntu machine and execute it there.

### Run directly on Ubuntu

```bash
inji-issuer-deploy bootstrap ubuntu-onprem --with-k3s --no-dry-run
```

## What it installs

- `python3`, `python3-venv`, `python3-pip`
- `git`, `curl`, `jq`
- `kubectl`
- `helm`
- MOSIP Helm repo
- optional `k3s`

## After bootstrap

Validate the environment:

```bash
kubectl config current-context
kubectl cluster-info
helm version
inji-issuer-deploy phase infra --dry-run
```

Then continue with the normal workflow:

```bash
inji-issuer-deploy phase collect
inji-issuer-deploy run
```
