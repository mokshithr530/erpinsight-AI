import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from erp_accuracy_benchmark import BenchmarkItem, benchmark_summary, load_benchmark
from mysql_adapter import mysql_adapter


GROUND_TRUTH_REVIEW_VERSION = "erp-ground-truth-review:v1"
REVIEW_DIR = Path(__file__).resolve().parent / "runtime" / "ground_truth_reviews"


@dataclass
class ReviewedQuestion:
    id: str
    split: str
    question: str
    module: str
    category: str
    complexity: str
    original_answerable: bool
    review_status: str
    data_exists: bool
    verified_tables: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    verified_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    verified_date_columns: list[str] = field(default_factory=list)
    verified_status_columns: list[dict[str, Any]] = field(default_factory=list)
    verified_joins: list[dict[str, Any]] = field(default_factory=list)
    risky_joins: list[dict[str, Any]] = field(default_factory=list)
    relationship_cardinality: list[dict[str, Any]] = field(default_factory=list)
    aggregation_grain: dict[str, Any] | None = None
    reference_sql_status: str = "not_provided"
    expected_result: dict[str, Any] | None = None
    corrections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _columns(schema, table: str) -> set[str]:
    return {column["column_name"] for column in schema.get(table, {}).get("columns", [])}


def _row_count(row_counts: dict[str, int], table: str) -> int:
    return int(row_counts.get(table) or 0)


def _is_date_column(column: str) -> bool:
    text = column.lower()
    return any(marker in text for marker in ("date", "dt", "time", "created", "approved", "start", "end"))


def _is_status_column(column: str) -> bool:
    text = column.lower()
    return any(marker in text for marker in ("status", "state", "type", "approval", "cancel", "verified"))


def _is_metric_column(column: str) -> bool:
    text = column.lower()
    return any(marker in text for marker in ("amount", "value", "price", "qty", "quantity", "rate", "total", "shortage", "damage", "excess", "ctc"))


def _status_values(database: str, table: str, column: str) -> dict[str, Any]:
    try:
        with mysql_adapter.engine(database).connect() as connection:
            rows = connection.exec_driver_sql(
                f"""
                SELECT `{column}` AS value, COUNT(*) AS count
                FROM `{table}`
                WHERE `{column}` IS NOT NULL AND TRIM(CAST(`{column}` AS CHAR)) <> ''
                GROUP BY `{column}`
                ORDER BY count DESC
                LIMIT 12
                """
            ).mappings().all()
    except Exception as error:
        return {"table": table, "column": column, "error": str(error), "values": []}
    return {
        "table": table,
        "column": column,
        "values": [{"value": row["value"], "count": int(row["count"] or 0)} for row in rows],
    }


def _execute_reference_sql(database: str, sql: str | None) -> tuple[str, dict[str, Any] | None]:
    if not sql:
        return "not_provided", None
    validation = mysql_adapter.validate_sql(sql, database)
    if not validation.get("valid"):
        return "validation_failed", {"validation": validation}
    try:
        started = time.perf_counter()
        frame = mysql_adapter.execute_read_query(sql, database)
    except Exception as error:
        return "execution_failed", {"error": str(error)}
    return "executed", {
        "columns": list(frame.columns),
        "row_count": len(frame),
        "sample_rows": frame.head(10).to_dict(orient="records"),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _join_cardinality(database: str, row_counts: dict[str, int], join: dict[str, Any]) -> dict[str, Any]:
    left = join["left_table"]
    left_column = join["left_column"]
    right = join["right_table"]
    right_column = join["right_column"]
    result = {
        "join": join,
        "left_rows": _row_count(row_counts, left),
        "right_rows": _row_count(row_counts, right),
        "left_distinct": None,
        "right_distinct": None,
        "joined_rows": None,
        "cardinality": "unknown",
        "can_multiply_rows": None,
        "notes": [],
    }
    try:
        with mysql_adapter.engine(database).connect() as connection:
            left_distinct = connection.exec_driver_sql(
                f"SELECT COUNT(DISTINCT `{left_column}`) FROM `{left}` WHERE `{left_column}` IS NOT NULL"
            ).scalar()
            right_distinct = connection.exec_driver_sql(
                f"SELECT COUNT(DISTINCT `{right_column}`) FROM `{right}` WHERE `{right_column}` IS NOT NULL"
            ).scalar()
            joined_rows = connection.exec_driver_sql(
                f"""
                SELECT COUNT(*)
                FROM `{left}` l
                JOIN `{right}` r
                  ON l.`{left_column}` = r.`{right_column}`
                """
            ).scalar()
    except Exception as error:
        result["notes"].append(f"cardinality_check_failed: {error}")
        return result

    result["left_distinct"] = int(left_distinct or 0)
    result["right_distinct"] = int(right_distinct or 0)
    result["joined_rows"] = int(joined_rows or 0)
    left_rows = max(1, int(result["left_rows"]))
    right_rows = max(1, int(result["right_rows"]))
    if result["left_distinct"] >= result["left_rows"] * 0.95 and result["right_distinct"] < result["right_rows"] * 0.95:
        result["cardinality"] = "one_to_many_or_dimension_to_fact"
    elif result["right_distinct"] >= result["right_rows"] * 0.95 and result["left_distinct"] < result["left_rows"] * 0.95:
        result["cardinality"] = "many_to_one_or_fact_to_dimension"
    elif result["left_distinct"] >= result["left_rows"] * 0.95 and result["right_distinct"] >= result["right_rows"] * 0.95:
        result["cardinality"] = "one_to_one_like"
    else:
        result["cardinality"] = "many_to_many_risk"
    result["can_multiply_rows"] = result["joined_rows"] > max(result["left_rows"], result["right_rows"])
    if result["can_multiply_rows"]:
        result["notes"].append("join can multiply rows; aggregate at the correct grain before summing")
    if result["joined_rows"] == 0:
        result["notes"].append("join currently returns zero rows")
    return result


def _review_item(
    database: str,
    schema: dict,
    row_counts: dict[str, int],
    item: BenchmarkItem,
    *,
    execute_reference_sql: bool,
    max_join_checks: int,
) -> ReviewedQuestion:
    verified_tables = [table for table in item.expected_tables if table in schema]
    missing_tables = [table for table in item.expected_tables if table not in schema]
    union_columns = set()
    for table in verified_tables:
        union_columns.update(_columns(schema, table))
    verified_columns = [column for column in item.expected_columns if column in union_columns]
    missing_columns = [column for column in item.expected_columns if column not in union_columns]
    data_exists = any(_row_count(row_counts, table) > 0 for table in verified_tables)
    corrections = []
    notes = []

    verified_date_columns = [
        column for column in item.expected_columns
        if column in union_columns and _is_date_column(column)
    ]
    verified_status_columns = []
    for table in verified_tables:
        table_columns = _columns(schema, table)
        for column in item.expected_columns:
            if column in table_columns and _is_status_column(column):
                verified_status_columns.append(_status_values(database, table, column))

    verified_joins = []
    risky_joins = []
    cardinality = []
    for index, join in enumerate(item.expected_joins):
        left_ok = join["left_table"] in schema and join["left_column"] in _columns(schema, join["left_table"])
        right_ok = join["right_table"] in schema and join["right_column"] in _columns(schema, join["right_table"])
        if left_ok and right_ok:
            verified_joins.append(join)
            if index < max_join_checks:
                card = _join_cardinality(database, row_counts, join)
                cardinality.append(card)
                if card["cardinality"] == "many_to_many_risk" or card["can_multiply_rows"]:
                    risky_joins.append(card)
        else:
            corrections.append(f"invalid join metadata: {join}")

    metric_columns = [column for column in item.expected_columns if _is_metric_column(column)]
    if item.expected_aggregation and item.expected_aggregation not in {"count", "count_or_sum"} and not metric_columns:
        notes.append("aggregation has no obvious metric column in expected_columns")
    aggregation_grain = {
        "aggregation": item.expected_aggregation,
        "grouping_columns": [
            column for column in item.expected_columns
            if column not in metric_columns and column not in verified_date_columns
        ],
        "metric_columns": metric_columns,
        "join_multiplication_risk": bool(risky_joins),
    }

    reference_sql_status = "not_provided"
    expected_result = dict(item.expected_result)
    if execute_reference_sql and item.reference_sql:
        reference_sql_status, executed = _execute_reference_sql(database, item.reference_sql)
        expected_result = {**expected_result, "actual_reference_result": executed}

    if missing_tables or missing_columns:
        status = "data_unavailable"
    elif not item.answerable:
        status = "missing_business_definition" if item.safe_failure else "ambiguous"
    elif not data_exists:
        status = "data_unavailable"
    elif risky_joins:
        status = "answerable_with_risky_join"
    elif item.expected_clarification:
        status = "ambiguous"
    else:
        status = "answerable_verified"

    return ReviewedQuestion(
        id=item.id,
        split=item.split,
        question=item.question,
        module=item.module,
        category=item.category,
        complexity=item.complexity,
        original_answerable=item.answerable,
        review_status=status,
        data_exists=data_exists,
        verified_tables=verified_tables,
        missing_tables=missing_tables,
        verified_columns=verified_columns,
        missing_columns=missing_columns,
        verified_date_columns=verified_date_columns,
        verified_status_columns=verified_status_columns,
        verified_joins=verified_joins,
        risky_joins=risky_joins,
        relationship_cardinality=cardinality,
        aggregation_grain=aggregation_grain,
        reference_sql_status=reference_sql_status,
        expected_result=expected_result,
        corrections=corrections,
        notes=notes,
    )


def review_development_set(
    database: str,
    *,
    execute_reference_sql: bool = True,
    max_join_checks_per_item: int = 2,
    output: str | None = None,
) -> dict[str, Any]:
    schema = mysql_adapter.inspect_schema(database)
    row_counts = {
        table: int(info.get("row_count") or 0)
        for table, info in schema.items()
    }
    items = [item for item in load_benchmark() if item.split == "development"]
    reviewed = [
        _review_item(
            database,
            schema,
            row_counts,
            item,
            execute_reference_sql=execute_reference_sql,
            max_join_checks=max_join_checks_per_item,
        )
        for item in items
    ]
    records = [item.to_dict() for item in reviewed]
    by_status = {}
    for item in records:
        by_status[item["review_status"]] = by_status.get(item["review_status"], 0) + 1
    report = {
        "version": GROUND_TRUTH_REVIEW_VERSION,
        "database": database,
        "schema_fingerprint": mysql_adapter.schema_fingerprint(database),
        "benchmark": benchmark_summary(items),
        "summary": {
            "reviewed_questions": len(records),
            "by_status": dict(sorted(by_status.items())),
            "reference_sql_executed": sum(1 for item in records if item["reference_sql_status"] == "executed"),
            "reference_sql_failed": sum(1 for item in records if item["reference_sql_status"] in {"validation_failed", "execution_failed"}),
            "risky_join_questions": sum(1 for item in records if item["risky_joins"]),
            "uncertain_metric_questions": sum(1 for item in records if item["notes"]),
        },
        "verified_questions": [item for item in records if item["review_status"] == "answerable_verified"],
        "corrected_questions": [item for item in records if item["corrections"]],
        "removed_duplicate_questions": [],
        "unavailable_data_questions": [item for item in records if item["review_status"] == "data_unavailable"],
        "missing_definition_questions": [item for item in records if item["review_status"] == "missing_business_definition"],
        "ambiguous_questions": [item for item in records if item["review_status"] == "ambiguous"],
        "risky_joins": [item for item in records if item["risky_joins"]],
        "uncertain_metrics": [item for item in records if item["notes"]],
        "records": records,
    }
    path = Path(output) if output else REVIEW_DIR / f"ground_truth_review_{mysql_adapter.schema_fingerprint(database)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    report["output"] = str(path)
    try:
        mysql_adapter.engine(database).dispose()
    except Exception:
        pass
    return report


def main():
    parser = argparse.ArgumentParser(description="Review ERP benchmark ground truth.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--no-reference-sql", action="store_true")
    parser.add_argument("--max-join-checks-per-item", type=int, default=2)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = review_development_set(
        args.database,
        execute_reference_sql=not args.no_reference_sql,
        max_join_checks_per_item=args.max_join_checks_per_item,
        output=args.output,
    )
    printable = {
        "database": report["database"],
        "schema_fingerprint": report["schema_fingerprint"],
        "output": report["output"],
        "summary": report["summary"],
    }
    print(json.dumps(printable, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

