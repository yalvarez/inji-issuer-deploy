# Terraform bootstrap for `inji-issuer-deploy`

This folder is the starting point for the **hybrid provisioning model**:

- **Terraform** owns cloud infrastructure and long-lived resources
- **`inji-issuer-deploy`** owns interactive data collection, config generation, Helm deployment, registration, and smoke tests

## Current status

This scaffold defines the **interface** between the CLI and Terraform.
Provider-specific resources can now be added incrementally in `main.tf` or `modules/`.

## CLI → Terraform handoff

After running Phase 0 with `provisioner=terraform`, the CLI generates:

```text
.inji-deploy/<issuer_id>/terraform.tfvars.json
```

Use it like this:

```bash
terraform -chdir=terraform init
terraform -chdir=terraform plan  -var-file=../.inji-deploy/<issuer_id>/terraform.tfvars.json
terraform -chdir=terraform apply -var-file=../.inji-deploy/<issuer_id>/terraform.tfvars.json
```

Then import the outputs back into the CLI state:

```bash
inji-issuer-deploy phase infra
# or simply:
inji-issuer-deploy run
```

## AWS-specific knobs

For `provider=aws`, the generated `provider_cfg` can include these optional keys:

- `aws_route53_zone_name` — hosted zone name like `mtc.gob.pe`
- `aws_manage_acm` — request/validate an ACM certificate automatically
- `aws_existing_acm_certificate_arn` — reuse an existing certificate instead of creating one
- `aws_create_dns_record` — create the issuer Route53 record when the target is known
- `aws_dns_record_name` — override the DNS record name (defaults to `base_domain`)
- `aws_dns_target_name` — ALB / CloudFront hostname for the record target
- `aws_dns_target_zone_id` — alias target zone ID; leave blank to create a CNAME instead

The Secrets Manager placeholder values are created with `ignore_changes`, so later manual secret updates will not be overwritten by Terraform.

If some AWS resources already exist, import them before `apply` to keep the workflow idempotent. Typical examples:

```bash
terraform -chdir=terraform import 'aws_ecr_repository.repos["inji-certify"]' mtc/inji-certify
terraform -chdir=terraform import 'aws_ecr_repository.repos["inji-verify"]'  mtc/inji-verify
terraform -chdir=terraform import 'aws_ecr_repository.repos["mimoto"]'       mtc/mimoto
terraform -chdir=terraform import 'aws_secretsmanager_secret.db[0]'            inji/mtc/db-credentials
terraform -chdir=terraform import 'aws_iam_role.pod_identity[0]'               inji-mtc-pod-role
```

## Expected outputs for the CLI

Keep these output names stable so the CLI can resume from Phase 2:

- `namespace`
- `registry_uris`
- `db_secret_ref`
- `data_api_secret_ref`
- `hsm_secret_ref`
- `workload_identity_ref`
- `pod_identity_role_arn`
- `dns_zone_id`
- `tls_cert_ref`
- `db_name`

## Recommended next steps

1. Add provider-specific resources in `main.tf`
2. Keep outputs mapped to the contract above
3. Re-run the CLI from `phase config` after `terraform apply`
