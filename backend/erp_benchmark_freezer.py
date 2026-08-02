import argparse
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from erp_accuracy_benchmark import (
    ERP_BENCHMARK_VERSION,
    FROZEN_BENCHMARK_FILE,
    benchmark_summary,
    generated_benchmark,
)
from mysql_adapter import mysql_adapter


FREEZE_FORMAT_VERSION = "erp-benchmark-freeze:v1"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def canonical_result(columns: list[str], records: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [
        {column: _json_value(record.get(column)) for column in columns}
        for record in records
    ]
    encoded = json.dumps(
        {"columns": columns, "records": normalized},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "columns": columns,
        "row_count": len(normalized),
        "records": normalized,
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _verified_expected_answer(item, database: str) -> dict[str, Any]:
    if not item.answerable:
        return {
            "type": (
                "clarification"
                if item.expected_clarification
                else "safe_failure"
            ),
            "reason": item.expected_clarification or item.safe_failure,
        }
    if not item.reference_sql:
        raise ValueError(f"{item.id} is answerable but has no reference SQL")
    validation = mysql_adapter.validate_sql(item.reference_sql, database)
    if not validation.get("valid"):
        raise ValueError(
            f"{item.id} reference SQL failed validation: "
            f"{'; '.join(validation.get('errors', []))}"
        )
    frame = mysql_adapter.execute_read_query(item.reference_sql, database)
    result = canonical_result(
        list(frame.columns),
        frame.to_dict(orient="records"),
    )
    return {
        "type": "verified_query_result",
        **result,
        "sample_records": result["records"][:10],
    }


def freeze_benchmark(
    database: str,
    output: str | None = None,
) -> dict[str, Any]:
    items = generated_benchmark()
    frozen_items = []
    for item in items:
        expected_answer = _verified_expected_answer(item, database)
        status = (
            "answerable_verified"
            if item.answerable
            else (
                "clarification_verified"
                if item.expected_clarification
                else "safe_failure_verified"
            )
        )
        frozen_items.append(
            replace(
                item,
                expected_answer=expected_answer,
                ground_truth_status=status,
            )
        )

    serialized_items = [item.to_dict() for item in frozen_items]
    canonical_items = json.dumps(
        serialized_items,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    holdout_ids = [item.id for item in frozen_items if item.split == "holdout"]
    payload = {
        "freeze_format": FREEZE_FORMAT_VERSION,
        "version": ERP_BENCHMARK_VERSION,
        "database": database,
        "schema_fingerprint": mysql_adapter.schema_fingerprint(database),
        "benchmark_sha256": hashlib.sha256(
            canonical_items.encode("utf-8")
        ).hexdigest(),
        "holdout_ids_sha256": hashlib.sha256(
            "\n".join(holdout_ids).encode("utf-8")
        ).hexdigest(),
        "summary": benchmark_summary(frozen_items),
        "items": serialized_items,
    }
    path = Path(output) if output else FROZEN_BENCHMARK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return {
        "output": str(path),
        "benchmark_sha256": payload["benchmark_sha256"],
        "holdout_ids_sha256": payload["holdout_ids_sha256"],
        "summary": payload["summary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify and freeze the ERP management benchmark."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    print(
        json.dumps(
            freeze_benchmark(args.database, args.output),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
