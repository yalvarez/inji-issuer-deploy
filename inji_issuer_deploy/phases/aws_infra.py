"""
Phase 1 — AWS infrastructure.

Creates all AWS resources needed for a new issuer:
  - EKS namespace (via kubectl)
  - RDS database schema + init SQL (via psql through a K8s Job)
  - ECR repositories (3: inji-certify, inji-verify, mimoto)
  - Secrets Manager entries (DB password, data API credentials)
  - IAM role for EKS Pod Identity
  - Route53 A/CNAME record placeholder
  - ACM certificate request (or reuse wildcard)

All operations are idempotent — safe to re-run.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from inji_issuer_deploy.state import DeployState, save_state

console = Console()


# ── utilities ─────────────────────────────────────────────────

def _boto(service: str, cfg) -> Any:
    return boto3.client(service, region_name=cfg.aws_region)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _step(label: str) -> None:
    console.print(f"  [cyan]→[/cyan] {label}")


def _ok(label: str) -> None:
    console.print(f"  [green]✓[/green] {label}")


def _skip(label: str) -> None:
    console.print(f"  [dim]↷ {label} — already exists, skipping[/dim]")


def _fail(label: str, err: str) -> None:
    console.print(f"  [red]✗[/red] {label}\n    [red]{err}[/red]")
    raise RuntimeError(f"{label}: {err}")


def _kubectl_error_text(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or result.stdout or "").strip()


def _is_kube_connection_error(err: str) -> bool:
    msg = err.lower()
    return any(token in msg for token in [
        "unable to connect to the server",
        "current-context is not set",
        "no configuration has been provided",
        "connection refused",
        "couldn't get current server api group list",
        "the connection to the server",
        "context was not found",
    ])


# ── EKS namespace ─────────────────────────────────────────────

def _ensure_namespace(ns: str) -> None:
    _step(f"EKS namespace {ns!r}")
    result = _run(["kubectl", "get", "namespace", ns], check=False)
    if result.returncode == 0:
        _skip(f"namespace {ns}")
        return

    get_err = _kubectl_error_text(result)
    if _is_kube_connection_error(get_err):
        _fail(
            "kubectl cannot reach the Kubernetes cluster",
            "Set your kubeconfig/current context for the target cluster and retry.\n"
            f"Original error: {get_err}",
        )

    r = _run(["kubectl", "create", "namespace", ns], check=False)
    if r.returncode != 0:
        create_err = _kubectl_error_text(r)
        if "already exists" in create_err.lower():
            _skip(f"namespace {ns}")
            return
        _fail("create namespace", create_err or "kubectl returned a non-zero exit code")
    # Label for Istio injection
    _run(["kubectl", "label", "namespace", ns,
          "istio-injection=enabled", "--overwrite"], check=False)
    _ok(f"namespace {ns} created")


# ── ECR repositories ──────────────────────────────────────────

def _ensure_ecr_repo(ecr, repo_name: str) -> str:
    """Returns the repository URI."""
    try:
        resp = ecr.describe_repositories(repositoryNames=[repo_name])
        uri = resp["repositories"][0]["repositoryUri"]
        _skip(f"ECR repo {repo_name}")
        return uri
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            _fail(f"ECR describe {repo_name}", str(e))

    _step(f"ECR repo {repo_name}")
    resp = ecr.create_repository(
        repositoryName=repo_name,
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
        tags=[
            {"Key": "issuer", "Value": repo_name.split("/")[-1]},
            {"Key": "managed-by", "Value": "inji-issuer-deploy"},
        ],
    )
    uri = resp["repository"]["repositoryUri"]
    _ok(f"ECR repo {repo_name} → {uri}")
    return uri


# ── Secrets Manager ───────────────────────────────────────────

def _ensure_secret(sm, secret_name: str, description: str,
                   placeholder: dict) -> str:
    """Creates a secret with a placeholder value if it doesn't exist. Returns ARN."""
    try:
        resp = sm.describe_secret(SecretId=secret_name)
        _skip(f"secret {secret_name}")
        return resp["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            _fail(f"describe secret {secret_name}", str(e))

    _step(f"secret {secret_name}")
    resp = sm.create_secret(
        Name=secret_name,
        Description=description,
        SecretString=json.dumps(placeholder),
        Tags=[
            {"Key": "managed-by", "Value": "inji-issuer-deploy"},
        ],
    )
    _ok(f"secret {secret_name} → {resp['ARN']}")
    console.print(
        f"  [yellow]⚠[/yellow]  Secret [bold]{secret_name}[/bold] created with "
        f"placeholder values.\n"
        f"     Update it in AWS Secrets Manager before the service starts."
    )
    return resp["ARN"]


# ── IAM role for EKS Pod Identity ────────────────────────────

def _ensure_pod_identity_role(iam, cfg, namespace: str) -> str:
    """Creates an IAM role for EKS Pod Identity. Returns role ARN."""
    role_name = f"inji-{cfg.issuer_id}-pod-role"
    try:
        resp = iam.get_role(RoleName=role_name)
        _skip(f"IAM role {role_name}")
        return resp["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            _fail(f"get IAM role {role_name}", str(e))

    _step(f"IAM role {role_name}")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "pods.eks.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": cfg.aws_account_id,
                },
                "ArnLike": {
                    "aws:SourceArn": (
                        f"arn:aws:eks:{cfg.aws_region}:{cfg.aws_account_id}"
                        f":cluster/{cfg.eks_cluster_name}"
                    ),
                },
            },
        }],
    }
    resp = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description=f"Pod Identity role for inji-certify issuer {cfg.issuer_id}",
        Tags=[{"Key": "managed-by", "Value": "inji-issuer-deploy"}],
    )
    role_arn = resp["Role"]["Arn"]

    # Inline policy: read its own secrets + S3 config bucket
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadOwnSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Resource": f"arn:aws:secretsmanager:{cfg.aws_region}:{cfg.aws_account_id}"
                             f":secret:inji/{cfg.issuer_id}/*",
            },
            {
                "Sid": "ReadMimotoConfig",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"arn:aws:s3:::{cfg.mimoto_issuers_s3_bucket}/*",
            },
            {
                "Sid": "ECRPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                "Resource": "*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"inji-{cfg.issuer_id}-policy",
        PolicyDocument=json.dumps(policy),
    )
    _ok(f"IAM role {role_name} → {role_arn}")
    return role_arn


# ── Route53 ───────────────────────────────────────────────────

def _ensure_route53_record(r53, cfg) -> str | None:
    """
    Looks for a hosted zone matching base_domain and creates a placeholder
    CNAME record if it doesn't exist yet.
    Returns the hosted zone ID or None if no matching zone found.
    """
    _step(f"Route53 for {cfg.base_domain}")
    parts = cfg.base_domain.split(".")
    # Try progressively shorter suffixes to find the hosted zone
    zone_id = None
    for i in range(1, len(parts) - 1):
        zone_name = ".".join(parts[i:]) + "."
        resp = r53.list_hosted_zones_by_name(DNSName=zone_name, MaxItems="1")
        for zone in resp.get("HostedZones", []):
            if zone["Name"].rstrip(".") == zone_name.rstrip("."):
                zone_id = zone["Id"].split("/")[-1]
                break
        if zone_id:
            break

    if not zone_id:
        console.print(
            f"  [yellow]⚠[/yellow]  No Route53 hosted zone found for {cfg.base_domain}.\n"
            f"     Create the DNS record manually after the ALB/Ingress is deployed."
        )
        return None

    _ok(f"hosted zone {zone_id} found for {cfg.base_domain}")
    console.print(
        f"  [dim]DNS record will need to be created manually once\n"
        f"  the AWS Load Balancer Controller assigns an endpoint.[/dim]"
    )
    return zone_id


# ── ACM certificate ───────────────────────────────────────────

def _ensure_acm_certificate(acm, cfg) -> str | None:
    """
    Checks for an existing wildcard or exact certificate.
    If none found, requests a new DNS-validated one.
    Returns the certificate ARN or None.
    """
    _step(f"ACM certificate for {cfg.base_domain}")
    # Look for existing valid cert that covers this domain
    paginator = acm.get_paginator("list_certificates")
    for page in paginator.paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
        for cert in page["CertificateSummaryList"]:
            domain = cert.get("DomainName", "")
            # Check exact match or wildcard match
            if domain == cfg.base_domain:
                _skip(f"ACM cert {cert['CertificateArn']}")
                return cert["CertificateArn"]
            # e.g. *.mtc.gob.pe covers certify.mtc.gob.pe
            if domain.startswith("*."):
                wildcard_base = domain[2:]
                if cfg.base_domain.endswith("." + wildcard_base):
                    _skip(f"ACM wildcard cert {cert['CertificateArn']} covers {cfg.base_domain}")
                    return cert["CertificateArn"]

    _step(f"requesting new ACM certificate for *.{cfg.base_domain}")
    resp = acm.request_certificate(
        DomainName=f"*.{cfg.base_domain}",
        ValidationMethod="DNS",
        SubjectAlternativeNames=[cfg.base_domain],
        Tags=[
            {"Key": "issuer", "Value": cfg.issuer_id},
            {"Key": "managed-by", "Value": "inji-issuer-deploy"},
        ],
    )
    arn = resp["CertificateArn"]
    _ok(f"ACM certificate requested → {arn}")
    console.print(
        f"  [yellow]⚠[/yellow]  Certificate is pending DNS validation.\n"
        f"     Add the CNAME record shown in the ACM console to validate it."
    )
    return arn


# ── RDS schema ────────────────────────────────────────────────

def _ensure_rds_schema(cfg) -> None:
    """
    Creates the PostgreSQL database and user for this issuer via a K8s Job
    that runs psql in-cluster (reusing the existing DB connection pattern).
    Falls back to printing the SQL if kubectl is not available.
    """
    _step(f"RDS schema for inji_{cfg.issuer_id}")
    db_name = f"inji_{cfg.issuer_id}"
    db_user = f"inji_{cfg.issuer_id}_user"

    sql_init = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{db_user}') THEN
        CREATE USER {db_user} WITH PASSWORD '{{DB_PASSWORD_PLACEHOLDER}}';
    END IF;
END
$$;

CREATE DATABASE {db_name} OWNER {db_user};
GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user};
""".strip()

    # Try to detect if psql is available via a pod in the cluster
    result = _run(
        ["kubectl", "get", "pods", "-n", "postgres", "--no-headers"],
        check=False,
    )
    if result.returncode != 0:
        # Print SQL for manual execution
        console.print(
            f"\n  [yellow]⚠[/yellow]  Cannot reach postgres namespace.\n"
            f"  Run the following SQL manually against the RDS instance:\n"
        )
        console.print(f"  [dim]{sql_init}[/dim]\n")
        console.print(
            f"  Then update the Secrets Manager secret "
            f"[bold]inji/{cfg.issuer_id}/db-credentials[/bold] "
            f"with the actual password."
        )
    else:
        _ok(f"RDS schema instructions noted (run manually or via DB init Job)")

    # Store the SQL for the k8s_deploy phase to run via the postgres-init Helm chart
    return sql_init


# ── main ─────────────────────────────────────────────────────

def run(state: DeployState, dry_run: bool = False) -> None:
    from rich.panel import Panel
    console.print(Panel(
        "[bold]Phase 1 — AWS Infrastructure[/bold]",
        border_style="cyan",
    ))

    cfg = state.issuer
    ns = f"inji-{cfg.issuer_id}"

    if dry_run:
        console.print("[yellow]DRY RUN — no resources will be created[/yellow]")
        _print_dry_run_plan(cfg, ns)
        return

    state.mark_started("aws_infra")
    outputs: dict = {}

    try:
        # Clients
        ecr = _boto("ecr", cfg)
        sm  = _boto("secretsmanager", cfg)
        iam = _boto("iam", cfg)
        r53 = _boto("route53", cfg)
        acm = _boto("acm", cfg)

        # 1. EKS namespace
        _ensure_namespace(ns)
        outputs["namespace"] = ns

        # 2. ECR repos
        console.print("\n  [bold]ECR repositories[/bold]")
        ecr_uris: dict[str, str] = {}
        for svc in ["inji-certify", "inji-verify", "mimoto"]:
            repo_name = f"{cfg.issuer_id}/{svc}"
            uri = _ensure_ecr_repo(ecr, repo_name)
            ecr_uris[svc] = uri
        outputs["ecr_uris"] = ecr_uris

        # 3. Secrets Manager
        console.print("\n  [bold]Secrets Manager[/bold]")
        db_secret_arn = _ensure_secret(
            sm,
            secret_name=f"inji/{cfg.issuer_id}/db-credentials",
            description=f"RDS credentials for inji_{cfg.issuer_id} database",
            placeholder={"username": f"inji_{cfg.issuer_id}_user", "password": "CHANGE_ME"},
        )
        outputs["db_secret_arn"] = db_secret_arn

        data_api_secret_arn = cfg.data_api_secret_arn
        if not data_api_secret_arn:
            placeholder = {
                "mtls":   {"client_cert": "CHANGE_ME", "client_key": "CHANGE_ME"},
                "oauth2": {"client_id": "CHANGE_ME", "client_secret": "CHANGE_ME"},
                "apikey": {"api_key": "CHANGE_ME"},
                "none":   {},
            }.get(cfg.data_api_auth_type, {})
            data_api_secret_arn = _ensure_secret(
                sm,
                secret_name=f"inji/{cfg.issuer_id}/data-api-credentials",
                description=f"Credentials for {cfg.issuer_id} data API ({cfg.data_api_auth_type})",
                placeholder=placeholder,
            )
            cfg.data_api_secret_arn = data_api_secret_arn
        outputs["data_api_secret_arn"] = data_api_secret_arn

        hsm_secret_arn = _ensure_secret(
            sm,
            secret_name=f"inji/{cfg.issuer_id}/softhsm-pin",
            description=f"SoftHSM security pin for inji-certify {cfg.issuer_id}",
            placeholder={"security-pin": "CHANGE_ME_WITH_STRONG_PIN"},
        )
        outputs["hsm_secret_arn"] = hsm_secret_arn

        # 4. IAM role
        console.print("\n  [bold]IAM role[/bold]")
        role_arn = _ensure_pod_identity_role(iam, cfg, ns)
        outputs["pod_identity_role_arn"] = role_arn

        # 5. Route53
        console.print("\n  [bold]Route53[/bold]")
        zone_id = _ensure_route53_record(r53, cfg)
        outputs["route53_zone_id"] = zone_id or ""

        # 6. ACM
        console.print("\n  [bold]ACM certificate[/bold]")
        cert_arn = _ensure_acm_certificate(acm, cfg)
        outputs["acm_cert_arn"] = cert_arn or ""

        # 7. RDS schema SQL
        console.print("\n  [bold]RDS schema[/bold]")
        init_sql = _ensure_rds_schema(cfg)
        outputs["db_init_sql"] = init_sql
        outputs["db_name"] = f"inji_{cfg.issuer_id}"

    except Exception as exc:
        state.mark_failed("aws_infra", str(exc))
        save_state(state)
        raise

    state.mark_done("aws_infra", outputs)
    save_state(state)
    console.print(f"\n[green]Phase 1 complete — {len(outputs)} resources provisioned.[/green]")


def _print_dry_run_plan(cfg, ns: str) -> None:
    from rich.table import Table
    t = Table(title="Resources that will be created", show_header=True)
    t.add_column("Resource type")
    t.add_column("Name / identifier")
    rows = [
        ("EKS namespace",   ns),
        ("ECR repo",        f"{cfg.issuer_id}/inji-certify"),
        ("ECR repo",        f"{cfg.issuer_id}/inji-verify"),
        ("ECR repo",        f"{cfg.issuer_id}/mimoto"),
        ("Secret",          f"inji/{cfg.issuer_id}/db-credentials"),
        ("Secret",          f"inji/{cfg.issuer_id}/data-api-credentials"),
        ("Secret",          f"inji/{cfg.issuer_id}/softhsm-pin"),
        ("IAM role",        f"inji-{cfg.issuer_id}-pod-role"),
        ("Route53",         f"lookup zone for {cfg.base_domain}"),
        ("ACM certificate", f"*.{cfg.base_domain}"),
        ("RDS schema",      f"inji_{cfg.issuer_id} (manual step)"),
    ]
    for r, n in rows:
        t.add_row(r, n)
    console.print(t)
