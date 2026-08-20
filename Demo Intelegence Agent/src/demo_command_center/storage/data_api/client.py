"""Aurora Serverless Data API — the only SQL execution path.

The Data API rather than a driver because it is what lets the orchestrator
Lambda live *outside* the VPC and still reach Postgres, which in turn means it
can call Meta, Google, Cashfree and OpenAI without a NAT Gateway. That single
choice removes a fixed monthly cost and a whole class of networking incidents.

Two rules this module enforces and nothing else needs to remember:

* **Every runtime value is a named parameter.** `execute()` takes `sql` and a
  `dict`; there is no interface that accepts an interpolated string. The only
  f-string interpolation anywhere in the SQL is the schema name, fixed at
  construction from configuration and validated as an identifier.
* **There is no `execute_raw`.** An LLM-reachable SQL tool cannot exist because
  no such function exists to reach.

`tests/security/test_sql_parameterisation.py` walks the AST of every module
under `storage/` and fails if a literal SQL string contains an f-string
placeholder other than the validated schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderError, ProviderTimeout, ProviderUnavailable
from demo_command_center.observability.logging import get_logger

logger = get_logger("storage.data_api")

PROVIDER: Final = "aurora_data_api"

#: A schema name is an identifier, never user input. Validated anyway, because
#: this is the one value that reaches SQL by interpolation.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Errors worth retrying. Aurora Serverless v2 pauses and resumes; the first
#: query against a resuming cluster fails with a distinctive code.
_RETRYABLE: frozenset[str] = frozenset(
    {
        "StatementTimeoutException",
        "ServiceUnavailableError",
        "InternalServerErrorException",
        "ThrottlingException",
        "DatabaseResumingException",
    }
)


class SchemaNameInvalid(ValueError):
    pass


def validate_schema(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise SchemaNameInvalid(f"not a valid postgres identifier: {name!r}")
    return name


@dataclass(frozen=True, slots=True)
class DataApiConfig:
    cluster_arn: str
    secret_arn: str
    database: str
    schema: str
    region: str = "ap-south-1"
    statement_timeout_ms: int = 5_000
    max_retries: int = 2


class DataApiClient:
    """Thin, typed wrapper over `rds-data`. Parameterised statements only."""

    def __init__(self, config: DataApiConfig, *, client: Any = None) -> None:
        self._config = config
        self._schema = validate_schema(config.schema)
        self._client = client

    @property
    def schema(self) -> str:
        return self._schema

    def _rds(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("rds-data", region_name=self._config.region)
        return self._client

    async def execute(
        self, sql: str, params: dict[str, Any] | None = None, *, transaction_id: str = ""
    ) -> list[dict[str, Any]]:
        """Run one statement. Returns rows as dicts; empty for writes."""
        request: dict[str, Any] = {
            "resourceArn": self._config.cluster_arn,
            "secretArn": self._config.secret_arn,
            "database": self._config.database,
            "sql": sql,
            "parameters": _to_parameters(params or {}),
            # Without this every result comes back as a positional list and the
            # repositories would index by column order, which breaks silently
            # the first time a migration adds a column in the middle.
            "formatRecordsAs": "JSON",
        }
        if transaction_id:
            request["transactionId"] = transaction_id

        client = self._rds()
        last: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = client.execute_statement(**request)
            except Exception as exc:
                last = exc
                name = type(exc).__name__
                if name not in _RETRYABLE or attempt >= self._config.max_retries:
                    raise _classify(exc) from exc
                logger.info(
                    "data api retry", extra={"dcc_error": name, "dcc_attempt": str(attempt)}
                )
                continue
            return _decode(response)
        raise _classify(last) if last else ProviderUnavailable(PROVIDER, "exhausted retries")

    async def begin(self) -> str:
        client = self._rds()
        response = client.begin_transaction(
            resourceArn=self._config.cluster_arn,
            secretArn=self._config.secret_arn,
            database=self._config.database,
        )
        return str(response["transactionId"])

    async def commit(self, transaction_id: str) -> None:
        self._rds().commit_transaction(
            resourceArn=self._config.cluster_arn,
            secretArn=self._config.secret_arn,
            transactionId=transaction_id,
        )

    async def rollback(self, transaction_id: str) -> None:
        try:
            self._rds().rollback_transaction(
                resourceArn=self._config.cluster_arn,
                secretArn=self._config.secret_arn,
                transactionId=transaction_id,
            )
        except Exception:
            logger.warning("rollback failed; transaction will time out server-side")


def _to_parameters(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Python values → Data API parameter structs.

    Timestamps go over as ISO strings with an explicit `TIMESTAMP` type hint;
    without the hint Postgres receives text and the comparison against a
    `timestamptz` column silently does nothing useful.
    """
    out: list[dict[str, Any]] = []
    for name, value in params.items():
        entry: dict[str, Any] = {"name": name}
        if value is None:
            entry["value"] = {"isNull": True}
        elif isinstance(value, bool):
            entry["value"] = {"booleanValue": value}
        elif isinstance(value, int):
            entry["value"] = {"longValue": value}
        elif isinstance(value, float):
            entry["value"] = {"doubleValue": value}
        elif isinstance(value, Decimal):
            entry["value"] = {"stringValue": str(value)}
            entry["typeHint"] = "DECIMAL"
        elif isinstance(value, datetime):
            entry["value"] = {"stringValue": value.isoformat(sep=" ", timespec="milliseconds")}
            entry["typeHint"] = "TIMESTAMP"
        elif isinstance(value, date):
            entry["value"] = {"stringValue": value.isoformat()}
            entry["typeHint"] = "DATE"
        elif isinstance(value, dict | list):
            import json

            entry["value"] = {"stringValue": json.dumps(value, separators=(",", ":"), default=str)}
            entry["typeHint"] = "JSON"
        else:
            entry["value"] = {"stringValue": str(value)}
        out.append(entry)
    return out


def _decode(response: dict[str, Any]) -> list[dict[str, Any]]:
    import json

    records = response.get("formattedRecords")
    if not records:
        return []
    decoded = json.loads(records)
    return decoded if isinstance(decoded, list) else []


def _classify(exc: Exception | None) -> ProviderError:
    name = type(exc).__name__ if exc else "Unknown"
    if "Timeout" in name:
        return ProviderTimeout(PROVIDER, 0.0)
    if name in _RETRYABLE:
        return ProviderUnavailable(PROVIDER, name)
    return ProviderUnavailable(PROVIDER, name)
