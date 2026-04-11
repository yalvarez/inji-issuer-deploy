variable "provider" {
  description = "Target infrastructure provider: aws, azure, gcp, or onprem"
  type        = string
}

variable "issuer_id" {
  description = "Short issuer slug, e.g. mtc"
  type        = string
}

variable "issuer_name" {
  description = "Human-readable issuer name"
  type        = string
}

variable "issuer_description" {
  description = "Wallet description for the issuer"
  type        = string
}

variable "base_domain" {
  description = "Public base domain for Certify"
  type        = string
}

variable "region" {
  description = "Primary cloud region or location"
  type        = string
}

variable "kubernetes_cluster_name" {
  description = "Target Kubernetes cluster name"
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID when provider=aws"
  type        = string
  default     = ""
}

variable "rds_host" {
  description = "Shared PostgreSQL endpoint"
  type        = string
}

variable "rds_port" {
  description = "Shared PostgreSQL port"
  type        = number
}

variable "rds_admin_secret_ref" {
  description = "Secret reference holding DB admin credentials"
  type        = string
}

variable "mimoto_config_store" {
  description = "Object-store location for mimoto-issuers-config.json"
  type = object({
    bucket = string
    key    = string
  })
}

variable "provider_cfg" {
  description = <<-EOT
    Provider-specific settings captured by Phase 0.

    Common AWS keys used by this module:
      - aws_route53_zone_name
      - aws_manage_acm
      - aws_existing_acm_certificate_arn
      - aws_create_dns_record
      - aws_dns_record_name
      - aws_dns_target_name
      - aws_dns_target_zone_id
  EOT
  type        = any
  default     = {}
}
