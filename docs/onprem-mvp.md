# On-prem MVP runbook

This is the recommended path for the **first real installation** of `inji-issuer-deploy`.

## Target profile

- `provider=onprem`
- `provisioner=python`
- `onprem_registry_backend=plain` or `harbor`
- `onprem_secrets_backend=k8s`
- `mimoto` config via `ConfigMap` first
- TLS via `cert-manager`

## Minimum cluster prerequisites

1. `kubectl` can reach the target cluster
2. `helm` is installed
3. `cert-manager` is installed
4. Namespace `mimoto` exists with the `mimoto` deployment
5. Shared config resources expected by the deploy phase exist (for RENIEC-pattern stacks):
   - namespace `config-server`
   - ConfigMaps `artifactory-share`, `config-server-share`
6. A PostgreSQL endpoint is already available

## Suggested first execution

```bash
inji-issuer-deploy phase collect
# Choose:
#   provider=onprem
#   provisioner=python
#   secrets backend=k8s
#   config backend=ConfigMap
#   cert-manager issuer=ClusterIssuer/letsencrypt-prod (or your internal CA)
#   shared config source namespace and ConfigMaps that match your cluster

inji-issuer-deploy phase infra --dry-run
inji-issuer-deploy phase config --dry-run
inji-issuer-deploy run
```

## Cluster-specific knobs now supported

The deploy phase is no longer tied to a single RENIEC-style namespace layout. You can configure:

- `shared_config_source_namespace`
- `shared_configmaps` (comma-separated in Phase 0; leave blank to skip copying)
- `helm_repo_name` / `helm_repo_url`
- `certify_chart_ref`
- `postgres_init_chart_ref` / `postgres_init_chart_version`
- `softhsm_chart_ref` / `softhsm_namespace`
- `mimoto_service_namespace` / `mimoto_service_name`

## Simulation before the first real run

A ready-to-use example state and walkthrough are included here:

- `docs/examples/onprem-example-state.json`
- `docs/examples/onprem-simulation.md`
- `docs/onprem-first-real-runbook.md`

Use them to rehearse `infra --dry-run`, `config`, and `deploy --dry-run` without touching any cloud, then follow the final Ubuntu + `k3s` runbook for the first controlled real install.

## Optional upgrades later

- Switch secrets backend to Vault
- Switch config backend from ConfigMap to MinIO
- Switch registry backend from plain to Harbor
- Add Terraform later only if you need repeatable infra provisioning around the cluster
