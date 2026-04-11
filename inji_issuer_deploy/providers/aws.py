"""
AWS provider — implements CloudProvider using boto3.

Credential resolution (in order, transparent to the operator):
  1. Named profile (aws_profile set in Phase 0)
  2. Environment variables: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
  3. ~/.aws/credentials default profile
  4. EC2 instance profile / ECS task role / EKS Pod Identity
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console

from inji_issuer_deploy.cloud import CloudProvider, CloudProviderConfig

console = Console()


class AWSProvider(CloudProvider):

    def __init__(self, provider_cfg: CloudProviderConfig, issuer_cfg):
        self._pcfg = provider_cfg
        self._icfg = issuer_cfg
        # Build a boto3 session — respects named profile if set
        if provider_cfg.aws_profile:
            self._session = boto3.Session(
                profile_name=provider_cfg.aws_profile,
                region_name=issuer_cfg.aws_region,
            )
        else:
            self._session = boto3.Session(region_name=issuer_cfg.aws_region)

    def _client(self, service: str):
        return self._session.client(service)

    def name(self) -> str:
        return "aws"

    def verify_credentials(self) -> tuple[bool, str]:
        from inji_issuer_deploy.cloud import _check_aws
        return _check_aws(self._pcfg)

    # ── Container registry (ECR) ──────────────────────────

    def ensure_registry_repo(self, repo_name: str) -> str:
        ecr = self._client("ecr")
        try:
            resp = ecr.describe_repositories(repositoryNames=[repo_name])
            uri = resp["repositories"][0]["repositoryUri"]
            console.print(f"  [dim]↷ ECR {repo_name} — already exists[/dim]")
            return uri
        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryNotFoundException":
                raise
        resp = ecr.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
            tags=[{"Key": "managed-by", "Value": "inji-issuer-deploy"}],
        )
        uri = resp["repository"]["repositoryUri"]
        console.print(f"  [green]✓[/green] ECR repo {repo_name} → {uri}")
        return uri

    # ── Secrets store (Secrets Manager) ──────────────────

    def ensure_secret(self, name: str, description: str,
                       placeholder: dict) -> str:
        sm = self._client("secretsmanager")
        try:
            resp = sm.describe_secret(SecretId=name)
            console.print(f"  [dim]↷ secret {name} — already exists[/dim]")
            return resp["ARN"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        resp = sm.create_secret(
            Name=name,
            Description=description,
            SecretString=json.dumps(placeholder),
            Tags=[{"Key": "managed-by", "Value": "inji-issuer-deploy"}],
        )
        console.print(f"  [green]✓[/green] secret {name}")
        console.print(f"  [yellow]⚠[/yellow]  Fill in real values at: AWS Secrets Manager → {name}")
        return resp["ARN"]

    def read_secret(self, reference: str) -> dict:
        sm = self._client("secretsmanager")
        val = sm.get_secret_value(SecretId=reference)["SecretString"]
        return json.loads(val)

    # ── Workload identity (IAM + EKS Pod Identity) ────────

    def ensure_workload_identity(self, issuer_id: str, namespace: str,
                                  cfg) -> str:
        iam = self._client("iam")
        role_name = f"inji-{issuer_id}-pod-role"
        try:
            resp = iam.get_role(RoleName=role_name)
            console.print(f"  [dim]↷ IAM role {role_name} — already exists[/dim]")
            return resp["Role"]["Arn"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise

        trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "pods.eks.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": cfg.aws_account_id},
                    "ArnLike": {"aws:SourceArn":
                        f"arn:aws:eks:{cfg.aws_region}:{cfg.aws_account_id}:cluster/{cfg.eks_cluster_name}"},
                },
            }],
        }
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"Pod Identity role for inji-certify issuer {issuer_id}",
            Tags=[{"Key": "managed-by", "Value": "inji-issuer-deploy"}],
        )
        role_arn = resp["Role"]["Arn"]
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "ReadOwnSecrets", "Effect": "Allow",
                 "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                 "Resource": f"arn:aws:secretsmanager:{cfg.aws_region}:{cfg.aws_account_id}:secret:inji/{issuer_id}/*"},
                {"Sid": "MimotoConfig", "Effect": "Allow",
                 "Action": ["s3:GetObject", "s3:PutObject"],
                 "Resource": f"arn:aws:s3:::{cfg.mimoto_issuers_s3_bucket}/*"},
                {"Sid": "ECRPull", "Effect": "Allow",
                 "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
                            "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer"],
                 "Resource": "*"},
            ],
        }
        iam.put_role_policy(RoleName=role_name, PolicyName=f"inji-{issuer_id}-policy",
                             PolicyDocument=json.dumps(policy))
        console.print(f"  [green]✓[/green] IAM role {role_name} → {role_arn}")
        return role_arn

    # ── DNS (Route53) ─────────────────────────────────────

    def find_dns_zone(self, domain: str) -> str | None:
        r53 = self._client("route53")
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            zone_name = ".".join(parts[i:]) + "."
            resp = r53.list_hosted_zones_by_name(DNSName=zone_name, MaxItems="1")
            for zone in resp.get("HostedZones", []):
                if zone["Name"].rstrip(".") == zone_name.rstrip("."):
                    zone_id = zone["Id"].split("/")[-1]
                    console.print(f"  [green]✓[/green] Route53 zone {zone_id} for {domain}")
                    return zone_id
        console.print(f"  [yellow]⚠[/yellow]  No Route53 zone found for {domain} — create DNS record manually")
        return None

    # ── TLS certificate (ACM) ─────────────────────────────

    def ensure_tls_certificate(self, domain: str) -> str | None:
        acm = self._client("acm")
        paginator = acm.get_paginator("list_certificates")
        for page in paginator.paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
            for cert in page["CertificateSummaryList"]:
                d = cert.get("DomainName", "")
                if d == domain or (d.startswith("*.") and domain.endswith("." + d[2:])):
                    console.print(f"  [dim]↷ ACM cert for {domain} — already exists[/dim]")
                    return cert["CertificateArn"]
        resp = acm.request_certificate(
            DomainName=f"*.{domain}",
            ValidationMethod="DNS",
            SubjectAlternativeNames=[domain],
            Tags=[{"Key": "managed-by", "Value": "inji-issuer-deploy"}],
        )
        arn = resp["CertificateArn"]
        console.print(f"  [green]✓[/green] ACM cert requested → {arn}")
        console.print(f"  [yellow]⚠[/yellow]  Complete DNS validation in the ACM console")
        return arn

    # ── Config file store (S3) ────────────────────────────

    def read_config_file(self, bucket: str, key: str) -> dict:
        s3 = self._client("s3")
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())

    def write_config_file(self, bucket: str, key: str, data: dict) -> None:
        s3 = self._client("s3")
        s3.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(data, indent=2),
                      ContentType="application/json")
        console.print(f"  [green]✓[/green] s3://{bucket}/{key} updated")

    # ── Dry-run plan ──────────────────────────────────────

    def dry_run_plan(self, issuer_id: str, cfg) -> list[tuple[str, str]]:
        return [
            ("EKS namespace",      f"inji-{issuer_id}"),
            ("ECR repo",           f"{issuer_id}/inji-certify"),
            ("ECR repo",           f"{issuer_id}/inji-verify"),
            ("ECR repo",           f"{issuer_id}/mimoto"),
            ("Secrets Manager",    f"inji/{issuer_id}/db-credentials"),
            ("Secrets Manager",    f"inji/{issuer_id}/data-api-credentials"),
            ("Secrets Manager",    f"inji/{issuer_id}/softhsm-pin"),
            ("IAM role",           f"inji-{issuer_id}-pod-role (EKS Pod Identity)"),
            ("Route53",            f"zone lookup for {cfg.base_domain}"),
            ("ACM certificate",    f"*.{cfg.base_domain}"),
            ("S3 patch",           f"s3://{cfg.mimoto_issuers_s3_bucket}/{cfg.mimoto_issuers_s3_key}"),
        ]
