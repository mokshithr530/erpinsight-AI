import json

from erp_accuracy_benchmark import benchmark_summary, load_benchmark
import erp_benchmark_merge
from erp_benchmark_runner import (
    _aggregate_column_score,
    _aggregate,
    _clarification_accuracy,
    _completed_record_ids,
    _load_existing_records,
    _relationship_selection_score,
    _result_property_score,
    _should_checkpoint,
    _verified_result_score,
)
from types import SimpleNamespace


def test_erp_benchmark_has_reviewable_size_split_and_required_fields():
    items = load_benchmark()
    summary = benchmark_summary(items)

    assert summary["total"] >= 200
    assert 0.70 <= summary["by_split"]["development"] / summary["total"] <= 0.80
    assert 0.20 <= summary["by_split"]["holdout"] / summary["total"] <= 0.30
    assert summary["safe_failure_expected"] >= 15

    required_categories = {
        "counts",
        "totals",
        "averages",
        "minimum_maximum",
        "top_bottom_n",
        "grouping",
        "comparisons",
        "entity_specific_filters",
        "multiple_entities",
        "date_ranges",
        "calendar_years",
        "financial_years",
        "status_filters",
        "multiple_filters",
        "trends",
        "zero_result_cases",
        "cross_module_questions",
        "missing_data_questions",
        "ambiguous_questions",
        "unsupported_business_definitions",
    }
    assert required_categories <= set(summary["by_category"])

    required_modules = {
        "projects",
        "clients",
        "billing",
        "invoices",
        "collections",
        "procurement",
        "purchase_orders",
        "vendors",
        "grn",
        "materials",
        "inventory",
        "stores",
        "boq",
        "employees",
        "departments",
        "attendance",
        "timesheets",
        "payroll",
    }
    assert required_modules <= set(summary["by_module"])

    for item in items:
        payload = item.to_dict()
        for field in (
            "question",
            "category",
            "module",
            "complexity",
            "answerable",
            "expected_intent",
            "expected_entities",
            "expected_tables",
            "expected_columns",
            "expected_joins",
            "expected_filters",
            "expected_aggregation",
            "expected_ordering",
            "reference_sql",
            "expected_result",
            "expected_interpretation",
            "verified_query_plan",
            "expected_answer",
            "ground_truth_status",
        ):
            assert field in payload


def test_verified_result_score_accepts_equivalent_mysql_numeric_types():
    item = SimpleNamespace(
        answerable=True,
        expected_answer={
            "columns": ["total_bill_amount"],
            "records": [
                {"total_bill_amount": "23105372347.897873"},
            ],
            "row_count": 1,
            "result_sha256": "different-representation",
        },
    )
    response = {
        "success": True,
        "result": {
            "columns": ["total_bill_amount"],
            "records": [
                {"total_bill_amount": 23105372347.897873},
            ],
            "rows": 1,
        },
    }

    assert _verified_result_score(item, response) == 1.0


def test_verified_result_score_accepts_equivalent_scalar_alias():
    item = SimpleNamespace(
        answerable=True,
        expected_answer={
            "columns": ["average_total_value"],
            "records": [
                {"average_total_value": "15187338.16058963"},
            ],
            "row_count": 1,
            "result_sha256": "different-alias",
        },
    )
    response = {
        "success": True,
        "result": {
            "columns": ["average_po_total_value"],
            "records": [
                {"average_po_total_value": 15187338.16058963},
            ],
            "rows": 1,
        },
    }

    assert _verified_result_score(item, response) == 1.0


def test_verified_result_score_preserves_large_currency_precision():
    item = SimpleNamespace(
        answerable=True,
        expected_answer={
            "columns": ["total_gross_billing"],
            "records": [
                {"total_gross_billing": "25156404601.670006"},
            ],
            "row_count": 1,
            "result_sha256": "different-float-representation",
        },
    )
    response = {
        "success": True,
        "result": {
            "columns": ["SUM(gross_bill_value)"],
            "records": [
                {"SUM(gross_bill_value)": 25156404601.670006},
            ],
            "rows": 1,
        },
    }

    assert _verified_result_score(item, response) == 1.0


def test_frozen_benchmark_has_complete_verified_ground_truth():
    items = load_benchmark()

    assert len(items) == 223
    assert all(item.reference_sql for item in items if item.answerable)
    assert all(
        item.expected_answer.get("type") == "verified_query_result"
        for item in items
        if item.answerable
    )
    assert all(
        item.expected_answer.get("type") in {"clarification", "safe_failure"}
        for item in items
        if not item.answerable
    )
    assert all(item.ground_truth_status != "unreviewed" for item in items)


def test_erp_benchmark_uses_stable_holdout_ids():
    holdout = [item.id for item in load_benchmark() if item.split == "holdout"]

    assert holdout[:5] == [
        "erp-004-project-specific-details",
        "erp-008-client-top-billing",
        "erp-012-grn-total-value",
        "erp-016-department-headcount",
        "erp-020-profit-loss-missing",
    ]


def test_benchmark_aggregate_reports_required_accuracy_metrics():
    records = [
        {
            "module": "billing",
            "category": "totals",
            "complexity": "easy",
            "end_to_end_accuracy": 1.0,
            "parser_accuracy": 1.0,
            "entity_resolution_accuracy": 1.0,
            "schema_retrieval_accuracy": 1.0,
            "query_plan_accuracy": 1.0,
            "sql_execution_success": 1.0,
            "result_accuracy": 1.0,
            "safe_failure_accuracy": 1.0,
        },
        {
            "module": "billing",
            "category": "totals",
            "complexity": "hard",
            "end_to_end_accuracy": 0.5,
            "parser_accuracy": 1.0,
            "entity_resolution_accuracy": 1.0,
            "schema_retrieval_accuracy": 0.5,
            "query_plan_accuracy": 0.5,
            "sql_execution_success": 0.0,
            "result_accuracy": 0.0,
            "safe_failure_accuracy": 1.0,
        },
    ]

    report = _aggregate(records)

    assert report["summary"]["end_to_end_accuracy"] == 0.75
    assert report["summary"]["parser_accuracy"] == 1.0
    assert report["summary"]["entity_resolution_accuracy"] == 1.0
    assert report["summary"]["schema_retrieval_accuracy"] == 0.75
    assert report["summary"]["query_plan_accuracy"] == 0.75
    assert report["summary"]["sql_execution_success"] == 0.5
    assert report["summary"]["result_accuracy"] == 0.5
    assert report["summary"]["safe_failure_accuracy"] == 1.0
    assert report["by_module"]["billing"]["count"] == 2


def test_benchmark_runner_loads_existing_checkpoint_records(tmp_path):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(
        '{"records": [{"id": "erp-001", "passed": true}, {"id": "erp-002", "passed": false}]}',
        encoding="utf-8",
    )

    records = _load_existing_records(trace_path)

    assert [record["id"] for record in records] == ["erp-001", "erp-002"]
    assert _completed_record_ids(records) == {"erp-001", "erp-002"}


def test_benchmark_runner_ignores_missing_or_invalid_checkpoint(tmp_path):
    assert _load_existing_records(tmp_path / "missing.json") == []

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not json", encoding="utf-8")

    assert _load_existing_records(invalid_path) == []


def test_benchmark_runner_checkpoint_interval_logic():
    assert _should_checkpoint(completed=5, total=152, interval=5)
    assert not _should_checkpoint(completed=4, total=152, interval=5)
    assert not _should_checkpoint(completed=5, total=152, interval=0)
    assert _should_checkpoint(completed=152, total=152, interval=0)


def test_reference_sql_result_scores_without_chat_response():
    item = next(item for item in load_benchmark() if item.id == "erp-002-project-top-value")
    execution_result = {
        "success": True,
        "columns": ["project_name", "project_value"],
        "rows": [{"project_name": "Demo", "project_value": 10}],
        "row_count": 1,
    }

    assert _result_property_score(item, response=None, execution_result=execution_result) == 1.0


def test_reference_sql_result_without_required_columns_scores_complete_without_chat_response():
    item = next(item for item in load_benchmark() if item.id == "erp-001-project-count")
    execution_result = {
        "success": True,
        "columns": ["project_count"],
        "rows": [{"project_count": 19}],
        "row_count": 1,
    }

    assert _result_property_score(item, response=None, execution_result=execution_result) == 1.0


def test_count_star_satisfies_count_metric_column_without_naming_identifier():
    item = next(item for item in load_benchmark() if item.id == "erp-001-project-count")
    retrieval = {
        "selected_tables": ["project_mst"],
        "relevant_columns": {"project_mst": []},
    }

    assert _aggregate_column_score(
        item,
        retrieval,
        "SELECT COUNT(*) AS project_count FROM project_mst",
    ) == 1.0


def test_successful_scalar_chat_result_scores_complete():
    item = next(item for item in load_benchmark() if item.id == "erp-001-project-count")
    response = {
        "success": True,
        "result": {
            "columns": ["project_count"],
            "records": [{"project_count": 19}],
            "rows": 1,
        },
    }

    assert _result_property_score(item, response=response, execution_result=None) == 1.0
    assert _verified_result_score(item, response) == 1.0


def test_wrong_scalar_result_fails_verified_answer_scoring():
    item = next(item for item in load_benchmark() if item.id == "erp-001-project-count")
    response = {
        "success": True,
        "result": {
            "columns": ["project_count"],
            "records": [{"project_count": 10}],
            "rows": 1,
        },
    }

    assert _verified_result_score(item, response) == 0.0


def test_clarification_accuracy_requires_no_sql_and_clarifying_language():
    item = next(
        item
        for item in load_benchmark()
        if item.expected_clarification
    )
    response = {
        "success": True,
        "sql": None,
        "answer": "I need clarification: which verified status value should I use?",
    }

    assert _clarification_accuracy(item, response) == 1.0


def test_relationship_selection_scores_verified_join_from_generated_sql():
    item = next(
        item
        for item in load_benchmark()
        if item.id == "erp-006-billing-by-project"
    )
    sql = """
        SELECT p.project_name, SUM(b.gross_bill_value)
        FROM client_bill_raising b
        JOIN project_mst p ON p.project_id = b.project_id
        GROUP BY p.project_id, p.project_name
    """

    assert _relationship_selection_score(item, {}, sql) == 1.0


def test_aggregate_reports_latency_percentiles_and_new_pipeline_metrics():
    records = [
        {
            "module": "projects",
            "category": "counts",
            "complexity": "easy",
            "end_to_end_accuracy": 1.0,
            "semantic_binding_accuracy": 1.0,
            "column_selection_accuracy": 1.0,
            "metric_selection_accuracy": 1.0,
            "relationship_selection_accuracy": 1.0,
            "sql_generation_accuracy": 1.0,
            "final_answer_accuracy": 1.0,
            "clarification_accuracy": 1.0,
            "latency_ms": 100,
        },
        {
            "module": "projects",
            "category": "counts",
            "complexity": "easy",
            "end_to_end_accuracy": 0.0,
            "semantic_binding_accuracy": 0.0,
            "column_selection_accuracy": 0.0,
            "metric_selection_accuracy": 0.0,
            "relationship_selection_accuracy": 0.0,
            "sql_generation_accuracy": 0.0,
            "final_answer_accuracy": 0.0,
            "clarification_accuracy": 0.0,
            "latency_ms": 300,
        },
    ]

    report = _aggregate(records)

    assert report["summary"]["semantic_binding_accuracy"] == 0.5
    assert report["summary"]["metric_selection_accuracy"] == 0.5
    assert report["summary"]["relationship_selection_accuracy"] == 0.5
    assert report["summary"]["final_answer_accuracy"] == 0.5
    assert report["latency"] == {
        "count": 2,
        "mean_ms": 200.0,
        "p50_ms": 100.0,
        "p95_ms": 300.0,
        "max_ms": 300.0,
    }


def test_benchmark_shard_merge_requires_complete_non_overlapping_coverage(
    tmp_path,
    monkeypatch,
):
    items = [
        item
        for item in load_benchmark()
        if item.split == "development"
    ][:3]
    monkeypatch.setattr(erp_benchmark_merge, "load_benchmark", lambda: items)
    common = {
        "database": "erp",
        "schema_fingerprint": "schema",
        "production_model": {"provider": "test", "model": "test"},
        "status": "complete",
    }
    paths = []
    def record(item):
        return {
            "id": item.id,
            "module": item.module,
            "category": item.category,
            "complexity": item.complexity,
            "end_to_end_accuracy": 1.0,
            "latency_ms": 10,
        }

    for index, records in enumerate(
        (
            [record(items[0]), record(items[2])],
            [record(items[1])],
        )
    ):
        path = tmp_path / f"shard-{index}.json"
        path.write_text(
            json.dumps({**common, "records": records}),
            encoding="utf-8",
        )
        paths.append(str(path))

    output = tmp_path / "merged.json"
    report = erp_benchmark_merge.merge_benchmark_reports(
        paths,
        str(output),
        expected_split="development",
    )

    assert report["completed"] == 3
    assert [record["id"] for record in report["records"]] == sorted(
        item.id for item in items
    )
    assert output.exists()
