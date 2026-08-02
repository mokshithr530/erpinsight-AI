from dataclasses import asdict, dataclass
from typing import Any

from database_fingerprint import sha256_text, stable_json
from query_plan import QUERY_PLAN_VERSION
from verified_sql_validator import VERIFIED_SQL_VALIDATOR_VERSION


VERIFIED_EXECUTION_VERSION = "verified-execution:v1"


@dataclass
class VerifiedExecutionReceipt:
    authoritative: bool
    database_fingerprint: str
    schema_fingerprint: str
    catalog_version: str | None
    erp_schema_version: str | None
    query_plan_version: str
    sql_validator_version: str
    sql_hash: str
    result_signature: str
    selected_tables: list[str]
    selected_columns: dict[str, list[str]]
    approved_relationships: list[dict[str, Any]]
    row_count: int
    column_names: list[str]
    validation_passed: bool
    execution_succeeded: bool
    version: str = VERIFIED_EXECUTION_VERSION

    def to_dict(self):
        return asdict(self)


def build_verified_execution_receipt(
    *,
    sql: str,
    result: dict[str, Any],
    query_plan,
    validation: dict[str, Any],
    database_fingerprint: str,
    schema_fingerprint: str,
) -> VerifiedExecutionReceipt:
    records = result.get("records") or []
    result_identity = {
        "columns": result.get("columns") or [],
        "rows": result.get("rows"),
        "records": records,
    }
    return VerifiedExecutionReceipt(
        authoritative=bool(query_plan.catalog_authoritative),
        database_fingerprint=database_fingerprint,
        schema_fingerprint=schema_fingerprint,
        catalog_version=query_plan.catalog_version,
        erp_schema_version=query_plan.erp_schema_version,
        query_plan_version=QUERY_PLAN_VERSION,
        sql_validator_version=VERIFIED_SQL_VALIDATOR_VERSION,
        sql_hash=sha256_text(" ".join(str(sql).split())),
        result_signature=sha256_text(stable_json(result_identity)),
        selected_tables=list(
            query_plan.generation_tables or query_plan.selected_tables
        ),
        selected_columns=dict(
            query_plan.generation_columns or query_plan.selected_columns
        ),
        approved_relationships=[
            {
                key: relationship.get(key)
                for key in (
                    "left_table",
                    "left_column",
                    "right_table",
                    "right_column",
                    "relationship_type",
                    "cardinality",
                )
                if relationship.get(key) not in (None, "")
            }
            for relationship in query_plan.approved_relationships
        ],
        row_count=int(result.get("rows") or 0),
        column_names=list(result.get("columns") or []),
        validation_passed=bool(validation.get("valid")),
        execution_succeeded=True,
    )


def verified_fact_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_count": int(result.get("rows") or 0),
        "columns": list(result.get("columns") or []),
        "rows": list(result.get("records") or []),
    }
