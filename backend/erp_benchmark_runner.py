import argparse
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from active_database import get_session_source
from config import LLM_MODEL, LLM_PROVIDER
from entity_resolver import entity_resolver
from erp_accuracy_benchmark import BenchmarkItem, benchmark_summary, load_benchmark
from erp_semantic_binder import erp_semantic_binder
from evaluation_trace import EvaluationTrace, classify_error_category
from mysql_adapter import mysql_adapter
from question_classifier import question_classifier
from query_plan import query_plan_guard
from schema_profile_history import schema_profile_history
from schema_retriever import schema_retriever
from semantic_parser import semantic_parser


BENCHMARK_TRACE_DIR = Path(__file__).resolve().parent / "runtime" / "benchmark_traces"


@dataclass
class BenchmarkRunConfig:
    database: str | None = None
    session_id: str | None = None
    split: str | None = None
    module: str | None = None
    limit: int | None = None
    run_chat: bool = False
    execute_reference_sql: bool = False
    use_safe_samples: bool = False
    capture_internal_retrieval: bool = False
    trace_output: str | None = None
    checkpoint_interval: int = 5
    resume: bool = False
    progress: bool = True
    shard_index: int = 0
    shard_count: int = 1


def _resolve_database(config: BenchmarkRunConfig) -> str:
    if config.database:
        return config.database
    if config.session_id:
        source = get_session_source(config.session_id)
        if source and source.get("source_type") == "mysql" and source.get("database"):
            return source["database"]
    raise ValueError("Benchmark runner needs a MySQL database or a session_id attached to MySQL.")


def _load_existing_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict) and record.get("id")]


def _completed_record_ids(records: list[dict[str, Any]]) -> set[str]:
    return {str(record["id"]) for record in records if record.get("id")}


def _should_checkpoint(completed: int, total: int, interval: int) -> bool:
    if completed >= total:
        return True
    return interval > 0 and completed % interval == 0


def _progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[benchmark] {message}", flush=True)


def _matches_intent(expected: str, semantic_plan: dict[str, Any], classification: dict[str, Any]) -> bool:
    if not expected:
        return True
    expected = expected.lower()
    values = {
        str(semantic_plan.get("operation") or "").lower(),
        str(semantic_plan.get("intent_category") or "").lower(),
        str(classification.get("intent") or "").lower(),
        *(str(value).lower() for value in semantic_plan.get("sub_operations", [])),
        *(str(value).lower() for value in classification.get("all_intents", [])),
    }
    aliases = {
        "aggregation": {"aggregation", "sum", "average", "ranking", "business_insight"},
        "distribution": {"distribution", "count", "aggregation", "business_insight"},
        "existence_check": {"existence_check", "count"},
        "exception_detection": {"exception_detection", "lookup", "business_insight"},
        "missing_kpi": {"missing_kpi", "business_insight"},
    }
    return expected in values or bool(values & aliases.get(expected, set()))


def _parser_score(item: BenchmarkItem, semantic_plan: dict[str, Any], classification: dict[str, Any]) -> float:
    checks = [_matches_intent(item.expected_intent, semantic_plan, classification)]
    if item.expected_aggregation:
        aggs = set(semantic_plan.get("aggregations") or [])
        checks.append(
            item.expected_aggregation in aggs
            or item.expected_aggregation in {"count_or_sum", "min_max"}
            or bool(aggs and item.expected_aggregation == "aggregation")
        )
    if item.expected_ordering:
        direction = item.expected_ordering.get("direction")
        limit = item.expected_ordering.get("limit")
        if direction:
            checks.append(str(semantic_plan.get("sort_direction") or "").lower() == str(direction).lower())
        if limit is not None:
            checks.append(semantic_plan.get("limit") == limit)
    if item.expected_filters:
        checks.append(bool(semantic_plan.get("date_constraints") or semantic_plan.get("status_constraints") or semantic_plan.get("filters") or item.expected_entities))
    return sum(bool(check) for check in checks) / max(1, len(checks))


def _entity_score(item: BenchmarkItem, entity_resolution: dict[str, Any]) -> float:
    expected = [value.lower() for value in item.expected_entities]
    if not expected:
        return 1.0
    candidates = [
        str(candidate.get("text") or candidate.get("mention") or "").lower()
        for candidate in entity_resolution.get("extracted_entities", [])
    ]
    resolved = [
        str(entity.get("mention") or entity.get("original_text") or entity.get("entity") or "").lower()
        for entity in entity_resolution.get("entities", [])
    ]
    haystack = " | ".join(candidates + resolved)
    return sum(1 for entity in expected if entity in haystack) / len(expected)


def _schema_score(item: BenchmarkItem, retrieval: dict[str, Any]) -> float:
    expected_tables = set(item.expected_tables)
    if not expected_tables:
        return 1.0
    selected = set(retrieval.get("selected_tables") or retrieval.get("tables") or [])
    ranked = {
        table.get("table")
        for table in retrieval.get("ranked_tables", [])
        if isinstance(table, dict)
    }
    found = expected_tables & (selected | ranked)
    return len(found) / len(expected_tables)


def _column_score(item: BenchmarkItem, retrieval: dict[str, Any]) -> float:
    expected = set(item.expected_columns)
    if not expected:
        return 1.0
    relevant = retrieval.get("relevant_columns") or retrieval.get("columns") or {}
    found = set()
    for columns in relevant.values():
        found.update(columns)
    return len(expected & found) / len(expected)


def _sql_mentions_column(sql: str | None, column: str) -> bool:
    if not sql:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_])`?{re.escape(column)}`?(?![A-Za-z0-9_])",
            sql,
            flags=re.IGNORECASE,
        )
    )


def _semantic_binding_score(
    item: BenchmarkItem,
    semantic_binding: dict[str, Any],
) -> float:
    if item.expected_clarification:
        return 1.0 if semantic_binding.get("clarification_required") else 0.0
    checks = []
    expected_tables = set(item.expected_tables)
    if expected_tables:
        bound_tables = set(semantic_binding.get("required_tables") or [])
        checks.append(len(expected_tables & bound_tables) / len(expected_tables))
    if item.expected_aggregation:
        bound = {
            str(metric.get("metric") or metric.get("requested_concept") or "").lower()
            for metric in semantic_binding.get("bound_metrics", [])
        }
        checks.append(
            1.0
            if bound
            or item.expected_aggregation == "count"
            or not item.answerable
            else 0.0
        )
    if item.answerable:
        checks.append(
            0.0 if semantic_binding.get("clarification_required") else 1.0
        )
    return round(sum(checks) / len(checks), 4) if checks else 1.0


def _metric_selection_score(
    item: BenchmarkItem,
    retrieval: dict[str, Any],
    generated_sql: str | None,
) -> float:
    if not item.expected_aggregation:
        return 1.0
    if item.expected_aggregation == "count" and generated_sql:
        if re.search(r"\bCOUNT\s*\(", generated_sql, flags=re.IGNORECASE):
            return 1.0
    expected = [
        column
        for column in item.expected_columns
        if any(
            marker in column.lower()
            for marker in (
                "amount",
                "value",
                "price",
                "qty",
                "quantity",
                "rate",
                "total",
                "count",
                "shortage",
                "damage",
                "excess",
                "ctc",
            )
        )
    ]
    if not expected:
        return _aggregate_column_score(item, retrieval, generated_sql)
    found = sum(
        1 for column in expected
        if _sql_mentions_column(generated_sql, column)
    )
    if generated_sql:
        return found / len(expected)
    relevant = {
        column
        for columns in (retrieval.get("relevant_columns") or {}).values()
        for column in columns
    }
    return len(set(expected) & relevant) / len(expected)


def _normalized_join_edges(joins: list[dict[str, Any]]) -> set[frozenset[str]]:
    edges = set()
    for join in joins:
        left_table = join.get("left_table")
        left_column = join.get("left_column")
        right_table = join.get("right_table")
        right_column = join.get("right_column")
        if all((left_table, left_column, right_table, right_column)):
            edges.add(frozenset({
                f"{left_table}.{left_column}".lower(),
                f"{right_table}.{right_column}".lower(),
            }))
    return edges


def _sql_join_edges(sql: str | None) -> set[frozenset[str]]:
    if not sql:
        return set()
    aliases = {
        alias.lower(): table.lower()
        for table, alias in re.findall(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*)`?"
            r"(?:\s+(?:AS\s+)?`?([A-Za-z_][\w]*)`?)?",
            sql,
            flags=re.IGNORECASE,
        )
        if alias and alias.upper() not in {
            "ON", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "GROUP",
            "ORDER", "LIMIT",
        }
    }
    edges = set()
    for left_ref, left_col, right_ref, right_col in re.findall(
        r"`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?"
        r"\s*=\s*"
        r"`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?",
        sql,
        flags=re.IGNORECASE,
    ):
        left_table = aliases.get(left_ref.lower(), left_ref.lower())
        right_table = aliases.get(right_ref.lower(), right_ref.lower())
        edges.add(frozenset({
            f"{left_table}.{left_col}".lower(),
            f"{right_table}.{right_col}".lower(),
        }))
    return edges


def _relationship_selection_score(
    item: BenchmarkItem,
    retrieval: dict[str, Any],
    generated_sql: str | None,
) -> float:
    expected = _normalized_join_edges(item.expected_joins)
    if not expected:
        return 1.0
    actual = _sql_join_edges(generated_sql)
    if not actual:
        actual = _normalized_join_edges(
            retrieval.get("relationship_paths") or []
        )
    return len(expected & actual) / len(expected)


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return repr(value)
    return str(value)


def _equivalent_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    try:
        left_number = Decimal(str(left))
        right_number = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return str(left).strip() == str(right).strip()
    tolerance = max(
        Decimal("0.000001"),
        abs(right_number) * Decimal("0.000000000001"),
    )
    return abs(left_number - right_number) <= tolerance


def _canonical_response_result(result: dict[str, Any] | None) -> dict[str, Any]:
    result = result or {}
    columns = [str(column) for column in result.get("columns") or []]
    records = result.get("records") or result.get("rows") or []
    if isinstance(records, int):
        records = []
    normalized = [
        {
            column: _canonical_value(record.get(column))
            for column in columns
        }
        for record in records
        if isinstance(record, dict)
    ]
    encoded = json.dumps(
        {"columns": columns, "records": normalized},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "columns": columns,
        "records": normalized,
        "row_count": len(normalized),
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _verified_result_score(
    item: BenchmarkItem,
    response: dict[str, Any] | None,
) -> float:
    expected = item.expected_answer or {}
    if not item.answerable:
        answer = str((response or {}).get("answer") or "").lower()
        no_sql = not (response or {}).get("sql")
        safe_language = any(
            phrase in answer
            for phrase in (
                "clarif",
                "cannot",
                "couldn't",
                "missing",
                "not enough",
                "need",
                "which ",
                "what ",
            )
        )
        return 1.0 if no_sql and safe_language else 0.0
    if not response or not response.get("success"):
        return 0.0
    actual = _canonical_response_result(response.get("result"))
    if actual["result_sha256"] == expected.get("result_sha256"):
        return 1.0
    expected_records = expected.get("records") or []
    if actual["row_count"] != expected.get("row_count"):
        return 0.0
    expected_columns = expected.get("columns") or []
    if set(expected_columns) <= set(actual["columns"]):
        projected = [
            {
                column: _canonical_value(record.get(column))
                for column in expected_columns
            }
            for record in actual["records"]
        ]
        canonical_expected = [
            {
                column: _canonical_value(record.get(column))
                for column in expected_columns
            }
            for record in expected_records
        ]
        if all(
            all(
                _equivalent_value(
                    projected_record.get(column),
                    expected_record.get(column),
                )
                for column in expected_columns
            )
            for projected_record, expected_record in zip(
                projected,
                canonical_expected,
            )
        ):
            return 1.0
    if len(expected_records) == len(actual["records"]) == 1:
        expected_values = list(expected_records[0].values())
        actual_values = list(actual["records"][0].values())
        if len(expected_values) == len(actual_values) and all(
            _equivalent_value(left, right)
            for left, right in zip(expected_values, actual_values)
        ):
            return 1.0
    return 0.0


def _clarification_accuracy(
    item: BenchmarkItem,
    response: dict[str, Any] | None,
) -> float:
    expects = bool(item.expected_clarification)
    answer = str((response or {}).get("answer") or "").lower()
    produced = (
        not (response or {}).get("sql")
        and any(
            marker in answer
            for marker in (
                "clarif",
                "which ",
                "what ",
                "choose",
                "select one",
                "please specify",
                "please confirm",
            )
        )
    )
    return 1.0 if produced == expects else 0.0


def _aggregate_column_score(
    item: BenchmarkItem,
    retrieval: dict[str, Any],
    generated_sql: str | None = None,
) -> float:
    score = _column_score(item, retrieval)
    if score >= 1.0 or not generated_sql:
        return score
    normalized_sql = re.sub(r"\s+", " ", generated_sql.lower())
    if item.expected_aggregation == "count" and re.search(r"\bcount\s*\(\s*(?:distinct\s+)?\*\s*\)", normalized_sql):
        referenced_tables = set(mysql_adapter.referenced_tables(generated_sql))
        if set(item.expected_tables) <= referenced_tables:
            return 1.0
    return score


def _query_plan_score(
    item: BenchmarkItem,
    semantic_plan: dict[str, Any],
    retrieval: dict[str, Any],
    generated_sql: str | None = None,
) -> float:
    checks = [
        _schema_score(item, retrieval) >= 0.75,
        _aggregate_column_score(item, retrieval, generated_sql) >= 0.5,
    ]
    if item.expected_joins:
        relationship_paths = retrieval.get("relationship_paths") or []
        selected_tables = set(retrieval.get("selected_tables") or [])
        join_tables = {
            join["left_table"] for join in item.expected_joins
        } | {
            join["right_table"] for join in item.expected_joins
        }
        checks.append(bool(relationship_paths) or join_tables <= selected_tables)
    if item.expected_filters:
        checks.append(
            bool(semantic_plan.get("date_constraints") or semantic_plan.get("status_constraints") or semantic_plan.get("filters"))
            or bool(item.expected_entities)
        )
    return sum(bool(check) for check in checks) / len(checks)


def _result_property_score(item: BenchmarkItem, response: dict[str, Any] | None, execution_result: dict[str, Any] | None) -> float:
    expected_type = item.expected_result.get("type")
    if not item.answerable:
        if response and response.get("success") is False:
            return 1.0
        text = str((response or {}).get("answer") or "").lower()
        return 1.0 if any(word in text for word in ("cannot", "missing", "clarification", "not enough", "requires")) else 0.0
    if expected_type in {"safe_failure", "clarification_or_safe_failure", "safe_failure_or_confirmed_proxy"}:
        return 1.0
    if execution_result and execution_result.get("success") is False:
        return 0.0
    result = (response or {}).get("result") or {}
    if not result and response is None and execution_result and execution_result.get("success"):
        result = execution_result
    columns = set(result.get("columns") or [])
    required = set(item.expected_result.get("required_columns") or [])
    if required:
        return len(required & columns) / len(required)
    if expected_type in {"zero_or_null_result", "rows_or_zero", "grouped_rows_or_zero"}:
        return 1.0
    if response and response.get("success"):
        if expected_type == "scalar_number":
            records = result.get("records") or []
            if len(records) == 1 and any(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in records[0].values()
            ):
                return 1.0
        return 0.75
    if response is None and execution_result and execution_result.get("success"):
        return 1.0
    return 0.0


def _sql_retrieval_trace(sql: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    if not sql:
        return {
            "selected_tables": [],
            "relevant_columns": {},
            "relationship_paths": [],
            "retrieval_confidence": 0.0,
            "warnings": ["No generated SQL was available for SQL-derived schema trace."],
        }
    selected_tables = mysql_adapter.referenced_tables(sql)
    sql_lower = sql.lower()
    relevant_columns = {}
    for table in selected_tables:
        columns = []
        for column in schema.get(table, {}).get("columns", []):
            column_name = column.get("column_name")
            if column_name and re.search(rf"`?{re.escape(column_name.lower())}`?", sql_lower):
                columns.append(column_name)
        relevant_columns[table] = sorted(set(columns))
    joins = []
    for left_alias, left_col, right_alias, right_col in re.findall(
        r"`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?\s*=\s*`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?",
        sql,
        flags=re.IGNORECASE,
    ):
        joins.append({
            "left_reference": f"{left_alias}.{left_col}",
            "right_reference": f"{right_alias}.{right_col}",
            "confidence": "sql_derived",
        })
    return {
        "selected_tables": selected_tables,
        "relevant_columns": relevant_columns,
        "relationship_paths": joins,
        "retrieval_confidence": 1.0 if selected_tables else 0.0,
        "warnings": ["Schema trace was derived from generated SQL, not internal retriever output."],
    }


def _load_ground_truth_status(schema_fingerprint: str) -> dict[str, str]:
    review_dir = Path(__file__).resolve().parent / "runtime" / "ground_truth_reviews"
    review_path = review_dir / f"ground_truth_review_{schema_fingerprint}.json"
    if not review_path.exists():
        matches = list(review_dir.glob("ground_truth_review_*.json"))
        review_path = matches[0] if matches else review_path
    if not review_path.exists():
        return {}
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    statuses = {}
    for record in payload.get("records", []):
        item_id = record.get("id") or record.get("item_id")
        status = record.get("status") or record.get("review_status")
        if item_id and status:
            statuses[item_id] = status
    return statuses


def _mentions_expected_entities(item: BenchmarkItem, entity_resolution: dict[str, Any]) -> bool:
    return _entity_score(item, entity_resolution) >= 1.0


def _has_date_requirement(item: BenchmarkItem) -> bool:
    return any(str(filter_item.get("type", "")).lower() in {"date", "year", "financial_year", "date_range"} for filter_item in item.expected_filters)


def _has_status_requirement(item: BenchmarkItem) -> bool:
    return any(str(filter_item.get("type", "")).lower() == "status" for filter_item in item.expected_filters)


def _has_answer_text(response: dict[str, Any] | None) -> bool:
    text = str((response or {}).get("answer") or "").strip().lower()
    if not text:
        return False
    generic_failures = (
        "could not turn that into a safe",
        "found a query idea, but it did not run",
        "did not find matching records",
        "try asking",
    )
    return not any(phrase in text for phrase in generic_failures)


def _failure_categories(
    item: BenchmarkItem,
    trace: EvaluationTrace,
    *,
    semantic_plan: dict[str, Any],
    classification: dict[str, Any],
    entity_resolution: dict[str, Any],
    retrieval: dict[str, Any],
    response: dict[str, Any] | None,
    component_scores: dict[str, Any],
    review_status: str | None = None,
) -> list[str]:
    categories = []
    if component_scores["parser_accuracy"] < 1.0:
        categories.append("semantic parsing")
    if component_scores.get("semantic_binding_accuracy", 1.0) < 1.0:
        categories.append("semantic binding")
    entity_candidates = entity_resolver.candidates_from_plan(semantic_plan)
    if item.expected_entities:
        def candidate_payload(candidate):
            return candidate.to_dict() if hasattr(candidate, "to_dict") else candidate

        extracted_text = " | ".join(
            str(candidate_payload(candidate).get("mention") or candidate_payload(candidate).get("text") or "")
            for candidate in entity_candidates
        ).lower()
        missing = [entity for entity in item.expected_entities if entity.lower() not in extracted_text]
        if missing:
            categories.append("entity candidate extraction")
        if not _mentions_expected_entities(item, entity_resolution):
            categories.append("entity resolution")
    elif entity_resolution.get("clarification_required"):
        categories.append("entity candidate extraction")
        categories.append("entity resolution")
    if component_scores["schema_retrieval_accuracy"] < 1.0:
        categories.append("schema retrieval")
    if component_scores["column_selection_accuracy"] < 1.0:
        categories.append("column selection")
    if component_scores.get("metric_selection_accuracy", 1.0) < 1.0:
        categories.append("metric selection")
    if _has_date_requirement(item):
        date_ok = bool(semantic_plan.get("date_constraints")) or any(
            "date" in column.lower()
            for columns in (retrieval.get("relevant_columns") or {}).values()
            for column in columns
        )
        if not date_ok:
            categories.append("date selection")
    if _has_status_requirement(item):
        status_ok = bool(semantic_plan.get("status_constraints")) or any(
            "status" in column.lower()
            for columns in (retrieval.get("relevant_columns") or {}).values()
            for column in columns
        )
        if not status_ok:
            categories.append("status selection")
    if component_scores.get("relationship_selection_accuracy", 1.0) < 1.0:
        categories.append("relationship selection")
    if review_status == "answerable_with_risky_join":
        categories.append("duplicate-producing join")
    if component_scores["query_plan_accuracy"] < 1.0:
        categories.append("query planning")
    if component_scores.get("sql_generation_accuracy", 1.0) < 1.0:
        categories.append("SQL generation")
    if trace.validation_result and not trace.validation_result.get("valid", trace.validation_result.get("success", True)):
        categories.append("SQL validation")
    if trace.execution_result and not trace.execution_result.get("success", True):
        categories.append("execution")
    if item.answerable and response is not None and response.get("success") is False:
        if "SQL validation" not in categories and "execution" not in categories:
            categories.append("SQL generation")
    execution_failed = trace.execution_result is not None and not trace.execution_result.get("success", True)
    if component_scores["final_answer_accuracy"] < 1.0 and not execution_failed:
        categories.append("result interpretation")
    if response is not None and item.answerable and not _has_answer_text(response):
        categories.append("answer formatting")
    if not item.answerable and response and response.get("success") and response.get("sql"):
        categories.append("unsupported question answered incorrectly")
    if component_scores.get("clarification_accuracy", 1.0) < 1.0:
        categories.append("clarification")
    return list(dict.fromkeys(categories))


def config_response_required(response: dict[str, Any] | None) -> bool:
    return response is not None


def _strict_correct(item: BenchmarkItem, failures: list[str], response: dict[str, Any] | None) -> bool:
    if failures:
        return False
    if response is None:
        return True
    if item.answerable:
        return bool(response.get("success")) and bool(response.get("sql")) and bool(response.get("answer"))
    return response.get("success") is False or not response.get("sql")


def _accuracy_level(value: float, threshold: float = 0.75) -> bool:
    return value >= threshold


def _score_item(
    item: BenchmarkItem,
    trace: EvaluationTrace,
    *,
    semantic_plan: dict[str, Any],
    classification: dict[str, Any],
    semantic_binding: dict[str, Any],
    entity_resolution: dict[str, Any],
    retrieval: dict[str, Any],
    response: dict[str, Any] | None,
    review_status: str | None = None,
) -> dict[str, Any]:
    parser = _parser_score(item, semantic_plan, classification)
    binding = _semantic_binding_score(item, semantic_binding)
    entity = _entity_score(item, entity_resolution)
    schema = _schema_score(item, retrieval)
    retrieval_columns = _aggregate_column_score(
        item,
        retrieval,
        trace.generated_sql,
    )
    sql_column_hits = (
        sum(
            1
            for column in item.expected_columns
            if _sql_mentions_column(trace.generated_sql, column)
        )
        / len(item.expected_columns)
        if item.expected_columns and trace.generated_sql
        else 0.0
    )
    columns = max(retrieval_columns, sql_column_hits)
    metrics = _metric_selection_score(item, retrieval, trace.generated_sql)
    relationships = _relationship_selection_score(
        item,
        retrieval,
        trace.generated_sql,
    )
    query_plan = _query_plan_score(item, semantic_plan, retrieval, trace.generated_sql)
    validation_ok = trace.validation_result is not None and bool(
        trace.validation_result.get("valid", trace.validation_result.get("success", False))
    )
    execution_ok = trace.execution_result is not None and bool(trace.execution_result.get("success", False))
    result_property = _result_property_score(
        item,
        response,
        trace.execution_result,
    )
    final_answer = _verified_result_score(item, response)
    clarification = _clarification_accuracy(item, response)
    sql_generation = (
        1.0
        if (
            item.answerable
            and bool(trace.generated_sql)
            and validation_ok
        )
        or (
            not item.answerable
            and not trace.generated_sql
        )
        else 0.0
    )
    safe_failure = final_answer if not item.answerable else 1.0
    end_to_end = min(
        parser,
        binding,
        entity,
        schema,
        columns,
        metrics,
        relationships,
        query_plan,
        sql_generation,
        1.0 if validation_ok and execution_ok else 0.0,
        final_answer,
        clarification,
    )
    if response and response.get("success") is False and item.answerable:
        end_to_end = 0.0
    record = {
        "id": item.id,
        "split": item.split,
        "module": item.module,
        "category": item.category,
        "complexity": item.complexity,
        "answerable": item.answerable,
        "parser_accuracy": parser,
        "semantic_binding_accuracy": binding,
        "entity_resolution_accuracy": entity,
        "schema_retrieval_accuracy": schema,
        "column_retrieval_accuracy": columns,
        "column_selection_accuracy": columns,
        "metric_selection_accuracy": metrics,
        "relationship_selection_accuracy": relationships,
        "query_plan_accuracy": query_plan,
        "sql_generation_accuracy": sql_generation,
        "sql_execution_success": 1.0 if validation_ok and execution_ok else 0.0,
        "result_property_accuracy": result_property,
        "result_accuracy": final_answer,
        "final_answer_accuracy": final_answer,
        "clarification_accuracy": clarification,
        "safe_failure_accuracy": safe_failure,
        "end_to_end_accuracy": end_to_end,
        "latency_ms": trace.latency_ms,
        "expected_ground_truth": {
            "interpretation": item.expected_interpretation,
            "query_plan": item.verified_query_plan,
            "reference_sql": item.reference_sql,
            "answer": item.expected_answer,
            "clarification": item.expected_clarification,
            "status": item.ground_truth_status,
        },
        "passed": _accuracy_level(end_to_end),
        "trace": trace.to_dict(),
    }
    failures = _failure_categories(
        item,
        trace,
        semantic_plan=semantic_plan,
        classification=classification,
        entity_resolution=entity_resolution,
        retrieval=retrieval,
        response=response,
        component_scores=record,
        review_status=review_status,
    )
    record["failure_categories"] = failures
    record["primary_failure_category"] = failures[0] if failures else None
    record["strict_correct"] = _strict_correct(item, failures, response)
    record["strict_end_to_end_accuracy"] = 1.0 if record["strict_correct"] else 0.0
    return record


def run_benchmark(config: BenchmarkRunConfig) -> dict[str, Any]:
    database = _resolve_database(config)
    schema = mysql_adapter.inspect_schema(database)
    row_counts = {
        item["table"]: item["approx_rows"]
        for item in mysql_adapter._largest_tables(database)
    }
    catalog = mysql_adapter.business_catalog(database)
    schema_fingerprint = mysql_adapter.schema_fingerprint(database)
    review_statuses = _load_ground_truth_status(schema_fingerprint)
    safe_samples = mysql_adapter._safe_profile_samples(database, schema) if config.use_safe_samples else None
    historical_queries = schema_profile_history.list(schema_fingerprint)
    benchmark_run_identity = (
        f"benchmark-v2|mysql|{database}|{schema_fingerprint}|"
        f"{time.time_ns()}"
    )
    items = [
        item for item in load_benchmark()
        if config.split is None or item.split == config.split
    ]
    if config.module:
        module = config.module.strip().lower()
        items = [
            item for item in items
            if str(item.module or "").lower() == module
        ]
    if config.shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if not 0 <= config.shard_index < config.shard_count:
        raise ValueError("shard_index must be between 0 and shard_count - 1")
    if config.shard_count > 1:
        items = [
            item
            for index, item in enumerate(items)
            if index % config.shard_count == config.shard_index
        ]
    if config.limit:
        items = items[: config.limit]

    if config.trace_output:
        path = Path(config.trace_output)
    else:
        BENCHMARK_TRACE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = config.split or "all"
        path = BENCHMARK_TRACE_DIR / f"erp_benchmark_{suffix}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    records = _load_existing_records(path) if config.resume else []
    completed_ids = _completed_record_ids(records)
    remaining_items = [item for item in items if item.id not in completed_ids]
    if records:
        _progress(
            config.progress,
            f"resuming {path} with {len(records)}/{len(items)} item(s) already complete",
        )
    if not remaining_items:
        report = _write_report(
            path,
            records,
            items,
            database=database,
            schema_fingerprint=schema_fingerprint,
            status="complete",
            completed=len(records),
        )
        report["trace_output"] = str(path)
        try:
            mysql_adapter.engine(database).dispose()
        except Exception:
            pass
        return report

    for item in remaining_items:
        item_number = len(records) + 1
        _progress(
            config.progress,
            f"{item_number}/{len(items)} {item.id} [{item.module}/{item.category}]",
        )
        started = time.perf_counter()
        semantic_plan = semantic_parser.parse(item.question, allow_llm_fallback=False).to_dict()
        classification = question_classifier.classify(item.question)
        semantic_binding = erp_semantic_binder.bind(
            question=item.question,
            semantic_plan=semantic_plan,
            catalog=catalog,
        ).to_dict()
        entity_candidates = entity_resolver.candidates_from_plan(semantic_plan)
        if entity_candidates:
            entity_resolution = mysql_adapter.resolve_entities(
                item.question,
                database,
                semantic_plan=semantic_plan,
                semantic_binding=semantic_binding,
            )
        else:
            entity_resolution = {
                "question": item.question,
                "extracted_entities": [],
                "entities": [],
                "matches": [],
                "ambiguities": [],
                "unresolved": [],
                "clarification_required": False,
            }
        semantic_binding = erp_semantic_binder.bind(
            question=item.question,
            semantic_plan=semantic_plan,
            catalog=catalog,
            entity_resolution=entity_resolution,
        ).to_dict()
        retrieval = None
        validation_result = None
        execution_result = None
        response = None
        generated_sql = None

        if config.execute_reference_sql and item.reference_sql:
            validation_result = mysql_adapter.validate_sql(item.reference_sql, database)
            if validation_result.get("valid"):
                try:
                    frame = mysql_adapter.execute_read_query(item.reference_sql, database)
                    execution_result = {
                        "success": True,
                        "columns": list(frame.columns),
                        "row_count": len(frame),
                        "rows": frame.head(5).to_dict(orient="records"),
                    }
                except Exception as error:
                    execution_result = {"success": False, "error": str(error)}
            else:
                execution_result = {"success": False, "error": "; ".join(validation_result.get("errors", []))}

        if config.run_chat:
            from chat_pipeline import chat_pipeline

            try:
                response = chat_pipeline.ask_mysql(
                    item.question,
                    database,
                    benchmark_run_identity,
                )
            except Exception as error:
                response = {
                    "success": False,
                    "answer": "Benchmark chat execution failed before a final answer was produced.",
                    "sql": None,
                    "result": None,
                    "error": str(error),
                    "cache_hit": False,
                }
            generated_sql = response.get("sql")
            if generated_sql and validation_result is None:
                validation_result = mysql_adapter.validate_sql(generated_sql, database)
            if response.get("result") and execution_result is None:
                execution_result = {
                    "success": bool(response.get("success")),
                    "columns": response["result"].get("columns", []),
                    "row_count": response["result"].get("rows"),
                }

        if config.run_chat and not config.capture_internal_retrieval:
            retrieval = _sql_retrieval_trace(generated_sql, schema)
        else:
            retrieval_result = schema_retriever.retrieve(
                question=item.question,
                schema=schema,
                row_counts=row_counts,
                catalog=catalog,
                schema_fingerprint=schema_fingerprint,
                semantic_plan=semantic_plan,
                entity_resolution=entity_resolution,
                candidate_limit=12,
                safe_samples=safe_samples,
                historical_queries=historical_queries,
            )
            retrieval = retrieval_result.to_dict()

        query_plan = query_plan_guard.build(
            question=item.question,
            semantic_plan=semantic_plan,
            entity_resolution=entity_resolution,
            retrieval=retrieval,
            semantic_binding=semantic_binding,
        )
        query_plan_validation = query_plan_guard.validate_plan(query_plan)
        sql_plan_validation = None
        if generated_sql and validation_result and validation_result.get("valid"):
            sql_plan_validation = query_plan_guard.validate_sql(
                generated_sql,
                query_plan,
                schema=schema,
            )
            if not sql_plan_validation["valid"]:
                production_verification = (response or {}).get("verification") or {}
                if (
                    production_verification.get("authoritative")
                    and production_verification.get("validation_passed")
                ):
                    validation_result = {
                        **validation_result,
                        "warnings": [
                            *validation_result.get("warnings", []),
                            "Evaluation plan differed from the authoritative "
                            "production plan: "
                            + "; ".join(sql_plan_validation["errors"]),
                        ],
                    }
                else:
                    validation_result = {
                        "valid": False,
                        "errors": [
                            *validation_result.get("errors", []),
                            *sql_plan_validation["errors"],
                        ],
                        "warnings": [
                            *validation_result.get("warnings", []),
                            *sql_plan_validation["warnings"],
                        ],
                    }

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        trace = EvaluationTrace(
            original_question=item.question,
            parsed_intent={
                "semantic_plan": semantic_plan,
                "semantic_binding": semantic_binding,
                "classification": classification,
            },
            question_contract=semantic_plan.get("question_contract"),
            extracted_entity_candidates=[candidate.to_dict() if hasattr(candidate, "to_dict") else candidate for candidate in entity_candidates],
            resolved_entities=entity_resolution.get("entities", []),
            selected_tables=retrieval.get("selected_tables", []),
            selected_columns=retrieval.get("relevant_columns", {}),
            relationship_path=retrieval.get("relationship_paths", []),
            query_plan={
                "actual_query_plan": query_plan.to_dict(),
                "query_plan_validation": query_plan_validation,
                "sql_plan_validation": sql_plan_validation,
                "expected_aggregation": item.expected_aggregation,
                "expected_ordering": item.expected_ordering,
                "expected_filters": item.expected_filters,
                "schema_retrieval_confidence": retrieval.get("retrieval_confidence"),
            },
            generated_sql=generated_sql,
            validation_result=validation_result,
            execution_result=execution_result,
            final_response=response,
            latency_ms=latency_ms,
            confidence=(response or {}).get("confidence"),
            error_category=classify_error_category(
                validation_result=validation_result,
                execution_result=execution_result,
                final_response=response,
            ),
        )
        records.append(_score_item(
            item,
            trace,
            semantic_plan=semantic_plan,
            classification=classification,
            semantic_binding=semantic_binding,
            entity_resolution=entity_resolution,
            retrieval=retrieval,
            response=response,
            review_status=review_statuses.get(item.id),
        ))
        if _should_checkpoint(len(records), len(items), config.checkpoint_interval):
            _write_report(
                path,
                records,
                items,
                database=database,
                schema_fingerprint=schema_fingerprint,
                status="partial",
                completed=len(records),
            )
            _progress(config.progress, f"checkpoint saved to {path}")

    report = _write_report(
        path,
        records,
        items,
        database=database,
        schema_fingerprint=schema_fingerprint,
        status="complete",
        completed=len(records),
    )
    report["trace_output"] = str(path)
    try:
        mysql_adapter.engine(database).dispose()
    except Exception:
        pass
    return report


def _write_report(
    path: Path,
    records: list[dict[str, Any]],
    items: list[BenchmarkItem],
    *,
    database: str,
    schema_fingerprint: str,
    status: str,
    completed: int,
) -> dict[str, Any]:
    report = _aggregate(records)
    report["benchmark"] = benchmark_summary(items)
    report["database"] = database
    report["schema_fingerprint"] = schema_fingerprint
    report["production_model"] = {
        "provider": LLM_PROVIDER,
        "model": LLM_MODEL,
    }
    report["records"] = records
    report["status"] = status
    report["completed"] = completed
    report["total"] = len(items)
    report["trace_output"] = str(path)
    with path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, sort_keys=True, default=str)
    return report


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    metric_keys = [
        "strict_end_to_end_accuracy",
        "end_to_end_accuracy",
        "parser_accuracy",
        "semantic_binding_accuracy",
        "entity_resolution_accuracy",
        "schema_retrieval_accuracy",
        "column_selection_accuracy",
        "metric_selection_accuracy",
        "relationship_selection_accuracy",
        "query_plan_accuracy",
        "sql_generation_accuracy",
        "sql_execution_success",
        "result_accuracy",
        "final_answer_accuracy",
        "clarification_accuracy",
        "safe_failure_accuracy",
    ]
    summary = {
        key: _average(record.get(key, record.get("end_to_end_accuracy", 0.0)) for record in records)
        for key in metric_keys
    }
    failure_counter = Counter(
        category
        for record in records
        for category in record.get("failure_categories", [])
    )
    examples = {}
    for category, _count in failure_counter.most_common():
        examples[category] = [
            {
                "id": record["id"],
                "question": record.get("trace", {}).get("original_question"),
                "module": record["module"],
                "complexity": record["complexity"],
                "error_category": record.get("trace", {}).get("error_category"),
                "sql": record.get("trace", {}).get("generated_sql"),
                "answer": (record.get("trace", {}).get("final_response") or {}).get("answer"),
            }
            for record in records
            if category in record.get("failure_categories", [])
        ][:3]
    highest_impact = failure_counter.most_common(1)[0][0] if failure_counter else None
    latencies = sorted(
        float(record.get("latency_ms") or 0.0)
        for record in records
    )
    return {
        "summary": summary,
        "latency": {
            "count": len(latencies),
            "mean_ms": round(statistics.fmean(latencies), 2)
            if latencies else 0.0,
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
            "max_ms": round(max(latencies), 2) if latencies else 0.0,
        },
        "by_module": _group(records, "module", metric_keys),
        "by_complexity": _group(records, "complexity", metric_keys),
        "by_category": _group(records, "category", metric_keys),
        "top_failure_categories": [
            {"category": category, "count": count}
            for category, count in failure_counter.most_common()
        ],
        "failure_examples": examples,
        "highest_impact_root_cause": highest_impact,
        "recommended_first_fix": _recommended_first_fix(highest_impact),
    }


def _recommended_first_fix(category: str | None) -> str | None:
    recommendations = {
        "schema retrieval": "Improve candidate table ranking and preserve expected table evidence before SQL generation.",
        "column selection": "Improve column role selection for metrics, dimensions, dates, and statuses before SQL generation.",
        "relationship selection": "Strengthen join path discovery and reject low-confidence inferred joins.",
        "SQL generation": "Feed the SQL generator stricter structured plans and require exact user constraints in the generated SQL.",
        "execution": "Add targeted SQL repair for adapter-specific runtime failures.",
        "entity resolution": "Resolve required entity filters against real values before SQL generation.",
        "semantic parsing": "Tighten deterministic parsing for operation, filters, limits, and ordering.",
    }
    return recommendations.get(category, "Review the top failure examples before changing production behavior.") if category else None


def _average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return round(sum(float(value) for value in values) / len(values), 4)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, math.ceil(len(values) * quantile) - 1))
    return round(values[index], 2)


def _group(records: list[dict[str, Any]], key: str, metric_keys: list[str]) -> dict[str, Any]:
    grouped = {}
    for record in records:
        grouped.setdefault(record[key], []).append(record)
    return {
        group_key: {
            "count": len(group_records),
            **{
                metric: _average(record[metric] for record in group_records)
                if metric in group_records[0]
                else _average(record.get("end_to_end_accuracy", 0.0) for record in group_records)
                for metric in metric_keys
            },
        }
        for group_key, group_records in sorted(grouped.items())
    }


def main():
    parser = argparse.ArgumentParser(description="Run the ERP accuracy benchmark.")
    parser.add_argument("--database", help="MySQL database name.")
    parser.add_argument("--session-id", help="Session id attached to a MySQL database.")
    parser.add_argument("--split", choices=["development", "holdout"], help="Benchmark split to run.")
    parser.add_argument("--module", help="Benchmark module to run, for example billing.")
    parser.add_argument("--limit", type=int, help="Limit number of benchmark items.")
    parser.add_argument("--run-chat", action="store_true", help="Call the full chat pipeline and LLM.")
    parser.add_argument("--execute-reference-sql", action="store_true", help="Execute manually written reference SQL where present.")
    parser.add_argument("--use-safe-samples", action="store_true", help="Include safe value samples during schema retrieval. Slower on very large ERP schemas.")
    parser.add_argument("--capture-internal-retrieval", action="store_true", help="Capture retriever output even when --run-chat is enabled. Slower.")
    parser.add_argument("--trace-output", help="Output JSON trace file.")
    parser.add_argument("--checkpoint-interval", type=int, default=5, help="Write a partial report after this many completed items. Use 0 to disable periodic checkpoints.")
    parser.add_argument("--resume", action="store_true", help="Resume from an existing --trace-output file by skipping completed item ids.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-item progress output.")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based deterministic shard index.")
    parser.add_argument("--shard-count", type=int, default=1, help="Number of deterministic non-overlapping shards.")
    args = parser.parse_args()
    values = vars(args)
    values["progress"] = not values.pop("quiet")
    report = run_benchmark(BenchmarkRunConfig(**values))
    printable = {
        "database": report["database"],
        "schema_fingerprint": report["schema_fingerprint"],
        "trace_output": report["trace_output"],
        "benchmark": report["benchmark"],
        "production_model": report["production_model"],
        "summary": report["summary"],
        "latency": report["latency"],
    }
    print(json.dumps(printable, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
