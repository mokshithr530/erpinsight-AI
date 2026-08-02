from erp_accuracy_benchmark import _case, BenchmarkItem
from erp_ground_truth_review import _review_item
from schema_profile_fixtures import column, table


def item_from_case(**kwargs):
    data = _case(**kwargs)
    data.pop("suffix")
    return BenchmarkItem(id="review-001", split="development", **data)


def test_ground_truth_review_marks_answerable_verified_without_risky_join():
    schema = {
        "projects": table(column("project_id", "int", primary_key=True), column("project_name"), row_count=10)
    }
    item = item_from_case(
        suffix="projects",
        question="How many projects?",
        category="counts",
        module="projects",
        complexity="easy",
        answerable=True,
        expected_intent="count",
        expected_tables=["projects"],
        expected_columns=["project_id"],
        expected_aggregation="count",
    )

    reviewed = _review_item(
        "demo",
        schema,
        {"projects": 10},
        item,
        execute_reference_sql=False,
        max_join_checks=0,
    )

    assert reviewed.review_status == "answerable_verified"
    assert reviewed.data_exists is True
    assert reviewed.missing_columns == []


def test_ground_truth_review_keeps_missing_definition_in_safe_failure_set():
    schema = {
        "projects": table(column("project_id", "int", primary_key=True), column("project_value", "decimal"), row_count=10)
    }
    item = item_from_case(
        suffix="profit",
        question="What is profit by project?",
        category="unsupported_business_definitions",
        module="projects",
        complexity="hard",
        answerable=False,
        expected_intent="missing_kpi",
        expected_tables=["projects"],
        expected_columns=["project_value"],
        expected_aggregation=None,
        expected_result={"type": "safe_failure"},
        safe_failure="profit_requires_cost_definition",
    )

    reviewed = _review_item(
        "demo",
        schema,
        {"projects": 10},
        item,
        execute_reference_sql=False,
        max_join_checks=0,
    )

    assert reviewed.review_status == "missing_business_definition"


def test_ground_truth_review_reports_unavailable_schema_objects():
    schema = {
        "projects": table(column("project_id", "int", primary_key=True), row_count=10)
    }
    item = item_from_case(
        suffix="bad",
        question="Total missing amount",
        category="totals",
        module="projects",
        complexity="easy",
        answerable=True,
        expected_intent="aggregation",
        expected_tables=["projects"],
        expected_columns=["missing_amount"],
        expected_aggregation="sum",
    )

    reviewed = _review_item(
        "demo",
        schema,
        {"projects": 10},
        item,
        execute_reference_sql=False,
        max_join_checks=0,
    )

    assert reviewed.review_status == "data_unavailable"
    assert reviewed.missing_columns == ["missing_amount"]

