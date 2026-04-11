terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  aws_enabled                  = var.provider == "aws"
  namespace                    = "inji-${var.issuer_id}"
  db_name                      = "inji_${var.issuer_id}"
  repo_names                   = toset(["inji-certify", "inji-verify", "mimoto"])
  route53_zone_name            = trimspace(try(var.provider_cfg.aws_route53_zone_name, try(var.provider_cfg.route53_zone_name, "")))
  manage_acm                   = try(var.provider_cfg.aws_manage_acm, try(var.provider_cfg.manage_acm, false))
  existing_acm_certificate_arn = trimspace(try(var.provider_cfg.aws_existing_acm_certificate_arn, ""))
  create_dns_record            = try(var.provider_cfg.aws_create_dns_record, false)
  dns_record_name              = trimspace(try(var.provider_cfg.aws_dns_record_name, var.base_domain))
  dns_target_name              = trimspace(try(var.provider_cfg.aws_dns_target_name, ""))
  dns_target_zone_id           = trimspace(try(var.provider_cfg.aws_dns_target_zone_id, ""))
  use_existing_cert            = local.existing_acm_certificate_arn != ""
  create_new_cert              = local.aws_enabled && local.manage_acm && !local.use_existing_cert
  create_alias_record          = local.aws_enabled && local.route53_zone_name != "" && local.create_dns_record && local.dns_target_name != "" && local.dns_target_zone_id != ""
  create_cname_record          = local.aws_enabled && local.route53_zone_name != "" && local.create_dns_record && local.dns_target_name != "" && local.dns_target_zone_id == ""

  common_tags = {
    issuer     = var.issuer_id
    managed-by = "inji-issuer-deploy"
  }
}

resource "aws_ecr_repository" "repos" {
  for_each = local.aws_enabled ? local.repo_names : toset([])

  name = "${var.issuer_id}/${each.value}"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, {
    service = each.value
  })
}

resource "aws_secretsmanager_secret" "db" {
  count = local.aws_enabled ? 1 : 0

  name        = "inji/${var.issuer_id}/db-credentials"
  description = "Database credentials for ${local.db_name}"
  tags        = local.common_tags
}

resource "aws_secretsmanager_secret_version" "db" {
  count = local.aws_enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.db[0].id
  secret_string = jsonencode({
    username = "inji_${var.issuer_id}_user"
    password = "CHANGE_ME"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "data_api" {
  count = local.aws_enabled ? 1 : 0

  name        = "inji/${var.issuer_id}/data-api-credentials"
  description = "Data API credentials for ${var.issuer_id}"
  tags        = local.common_tags
}

resource "aws_secretsmanager_secret_version" "data_api" {
  count = local.aws_enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.data_api[0].id
  secret_string = jsonencode(
    var.provider == "aws" ? {
      placeholder = "CHANGE_ME"
    } : {}
  )

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "softhsm" {
  count = local.aws_enabled ? 1 : 0

  name        = "inji/${var.issuer_id}/softhsm-pin"
  description = "SoftHSM security pin for ${var.issuer_id}"
  tags        = local.common_tags
}

resource "aws_secretsmanager_secret_version" "softhsm" {
  count = local.aws_enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.softhsm[0].id
  secret_string = jsonencode({
    security-pin = "CHANGE_ME_STRONG_RANDOM"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

data "aws_iam_policy_document" "pod_assume_role" {
  count = local.aws_enabled ? 1 : 0

  statement {
    effect = "Allow"
    actions = [
      "sts:AssumeRole",
      "sts:TagSession",
    ]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

    dynamic "condition" {
      for_each = var.aws_account_id != "" ? [1] : []
      content {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [var.aws_account_id]
      }
    }

    dynamic "condition" {
      for_each = var.aws_account_id != "" && var.kubernetes_cluster_name != "" ? [1] : []
      content {
        test     = "ArnLike"
        variable = "aws:SourceArn"
        values   = ["arn:aws:eks:${var.region}:${var.aws_account_id}:cluster/${var.kubernetes_cluster_name}"]
      }
    }
  }
}

resource "aws_iam_role" "pod_identity" {
  count = local.aws_enabled ? 1 : 0

  name               = "inji-${var.issuer_id}-pod-role"
  assume_role_policy = data.aws_iam_policy_document.pod_assume_role[0].json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "pod_permissions" {
  count = local.aws_enabled ? 1 : 0

  statement {
    sid    = "ReadOwnSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = compact([
      try(aws_secretsmanager_secret.db[0].arn, ""),
      try(aws_secretsmanager_secret.data_api[0].arn, ""),
      try(aws_secretsmanager_secret.softhsm[0].arn, ""),
    ])
  }

  dynamic "statement" {
    for_each = var.mimoto_config_store.bucket != "" ? [1] : []
    content {
      sid    = "MimotoConfig"
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:PutObject",
      ]
      resources = ["arn:aws:s3:::${var.mimoto_config_store.bucket}/*"]
    }
  }

  statement {
    sid    = "ECRPull"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "pod_identity" {
  count = local.aws_enabled ? 1 : 0

  name   = "inji-${var.issuer_id}-policy"
  role   = aws_iam_role.pod_identity[0].id
  policy = data.aws_iam_policy_document.pod_permissions[0].json
}

data "aws_route53_zone" "selected" {
  count = local.aws_enabled && local.route53_zone_name != "" ? 1 : 0

  name         = local.route53_zone_name
  private_zone = false
}

resource "aws_acm_certificate" "issuer" {
  count = local.create_new_cert ? 1 : 0

  domain_name               = "*.${var.base_domain}"
  subject_alternative_names = [var.base_domain]
  validation_method         = "DNS"

  tags = local.common_tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "issuer_alias" {
  count = local.create_alias_record ? 1 : 0

  zone_id = data.aws_route53_zone.selected[0].zone_id
  name    = local.dns_record_name
  type    = "A"

  alias {
    name                   = local.dns_target_name
    zone_id                = local.dns_target_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "issuer_cname" {
  count = local.create_cname_record ? 1 : 0

  zone_id = data.aws_route53_zone.selected[0].zone_id
  name    = local.dns_record_name
  type    = "CNAME"
  ttl     = 60
  records = [local.dns_target_name]
}

resource "aws_route53_record" "acm_validation" {
  for_each = local.create_new_cert && local.route53_zone_name != "" ? {
    for dvo in aws_acm_certificate.issuer[0].domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      value = dvo.resource_record_value
      type  = dvo.resource_record_type
    }
  } : {}

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.selected[0].zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.value]
}

resource "aws_acm_certificate_validation" "issuer" {
  count = local.create_new_cert && local.route53_zone_name != "" ? 1 : 0

  certificate_arn         = aws_acm_certificate.issuer[0].arn
  validation_record_fqdns = [for record in aws_route53_record.acm_validation : record.fqdn]
}
