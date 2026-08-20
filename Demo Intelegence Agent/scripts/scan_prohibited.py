"""Fail the build if Demo introduces a prohibited resource.

The constraint list is not advisory. Every entry is something that either costs
money continuously (NAT Gateway, ElastiCache, an always-on container), breaks
the serverless model, or duplicates infrastructure that already exists.

This scans **only** `Demo Intelegence Agent/`. The Tutor stack legitimately uses
S3 and a VPC, and flagging those would be exactly the collateral damage the
protected-path rule exists to prevent.

    python scripts/scan_prohibited.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Terraform resource types Demo may never declare, and why.
PROHIBITED_RESOURCES: dict[str, str] = {
    "aws_nat_gateway": "a NAT Gateway is a fixed hourly cost; Demo has no VPC-attached function",
    "aws_instance": "EC2 is not serverless",
    "aws_ecs_cluster": "ECS is not serverless",
    "aws_ecs_service": "ECS is not serverless",
    "aws_ecs_task_definition": "ECS is not serverless",
    "aws_eks_cluster": "Kubernetes is not serverless",
    "aws_elasticache_cluster": "no Redis; the shared store is PostgreSQL",
    "aws_elasticache_replication_group": "no Redis",
    "aws_elasticache_serverless_cache": "no Redis",
    "aws_s3_bucket": "Demo may not create S3; the package is uploaded directly",
    "aws_rds_cluster": "Demo uses the EXISTING cluster and owns only the demo_agent schema",
    "aws_db_instance": "same",
    "aws_rds_cluster_instance": "same",
    "aws_autoscaling_group": "not serverless",
    "aws_lb": "an always-on load balancer is not serverless",
    "aws_alb": "same",
}

#: Argument patterns that indicate a prohibited approach even without a
#: prohibited resource type — an S3-sourced Lambda is the obvious one.
PROHIBITED_ARGUMENTS: dict[str, str] = {
    r"^\s*s3_bucket\s*=": "Lambda code must be uploaded directly, not from S3",
    r"^\s*s3_key\s*=": "same",
    r'^\s*backend\s+"s3"': (
        "no new S3 bucket for Terraform state; see docs/operations/terraform-state.md"
    ),
    r"^\s*vpc_config\s*\{": "no Demo function is VPC-attached; persistence is the Data API",
}

#: Python imports that would mean a prohibited dependency crept in.
PROHIBITED_IMPORTS: dict[str, str] = {
    "redis": "no Redis",
    "aioredis": "no Redis",
    "pymemcache": "no Memcached",
    "memcache": "no Memcached",
    "pymysql": "no direct MySQL; the website is reached through the gateway",
    "MySQLdb": "same",
    "mysql.connector": "same",
    "psycopg2": "use asyncpg; one driver, matching the Tutor service",
    # asyncpg is ALLOWED and deliberately absent from this list. The original
    # no-driver rule assumed a Data-API-only world; the shared NXTutors database
    # is a plain RDS instance with no Data API, so a driver is the only way to
    # reach it. It is an optional extra, excluded from the Lambda package unless
    # `persistence_mode=postgres_dsn` is used.
}


def scan_terraform() -> list[str]:
    findings: list[str] = []
    for path in (ROOT / "infra").rglob("*.tf"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)

        for line_number, line in enumerate(text.splitlines(), start=1):
            # A comment explaining why something is absent is not a declaration.
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue

            for resource, reason in PROHIBITED_RESOURCES.items():
                if re.search(rf'^\s*resource\s+"{re.escape(resource)}"', line):
                    findings.append(f"{relative}:{line_number}: {resource} — {reason}")

            for pattern, reason in PROHIBITED_ARGUMENTS.items():
                if re.search(pattern, line):
                    findings.append(f"{relative}:{line_number}: {stripped[:60]} — {reason}")
    return findings


def scan_python() -> list[str]:
    findings: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        relative = path.relative_to(ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for module, reason in PROHIBITED_IMPORTS.items():
                if re.match(rf"^\s*(import|from)\s+{re.escape(module)}\b", line):
                    findings.append(f"{relative}:{line_number}: imports {module} — {reason}")
    return findings


def scan_dependencies() -> list[str]:
    findings: list[str] = []
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for module, reason in PROHIBITED_IMPORTS.items():
        if re.search(rf'"\s*{re.escape(module)}\s*[=<>~]', pyproject):
            findings.append(f"pyproject.toml: declares {module} — {reason}")
    return findings


def main() -> int:
    findings = scan_terraform() + scan_python() + scan_dependencies()

    print(f"prohibited-resource scan over {ROOT.name}/")
    print(f"  terraform resource types checked : {len(PROHIBITED_RESOURCES)}")
    print(f"  terraform argument patterns      : {len(PROHIBITED_ARGUMENTS)}")
    print(f"  python modules                   : {len(PROHIBITED_IMPORTS)}")
    print()

    if findings:
        print(f"FAIL: {len(findings)} prohibited item(s):")
        for finding in findings:
            print(f"  ! {finding}")
        return 1

    print("OK: no prohibited resource, argument or dependency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
