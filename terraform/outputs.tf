output "namespace" {
  description = "Target Kubernetes namespace for this issuer"
  value       = local.namespace
}

output "registry_uris" {
  description = "Container registry URIs keyed by service name"
  value = {
    for service, repo in aws_ecr_repository.repos :
    service => repo.repository_url
  }
}

output "db_secret_ref" {
  description = "Reference to the DB credentials secret"
  value       = try(aws_secretsmanager_secret.db[0].arn, null)
}

output "data_api_secret_ref" {
  description = "Reference to the Data API credentials secret"
  value       = try(aws_secretsmanager_secret.data_api[0].arn, null)
}

output "hsm_secret_ref" {
  description = "Reference to the SoftHSM secret"
  value       = try(aws_secretsmanager_secret.softhsm[0].arn, null)
}

output "workload_identity_ref" {
  description = "Reference to the pod/workload identity created by Terraform"
  value       = try(aws_iam_role.pod_identity[0].arn, null)
}

output "pod_identity_role_arn" {
  description = "Compatibility alias used by the CLI templates"
  value       = try(aws_iam_role.pod_identity[0].arn, null)
}

output "dns_zone_id" {
  description = "Managed DNS zone identifier"
  value       = try(data.aws_route53_zone.selected[0].zone_id, null)
}

output "issuer_dns_record_fqdn" {
  description = "FQDN of the Route53 record created for the issuer, if any"
  value       = try(aws_route53_record.issuer_alias[0].fqdn, try(aws_route53_record.issuer_cname[0].fqdn, null))
}

output "tls_cert_ref" {
  description = "Reference to the TLS certificate or manifest"
  value = local.use_existing_cert ? local.existing_acm_certificate_arn : try(
    aws_acm_certificate_validation.issuer[0].certificate_arn,
    try(aws_acm_certificate.issuer[0].arn, null),
  )
}

output "db_name" {
  description = "Logical database name expected by later phases"
  value       = local.db_name
}
