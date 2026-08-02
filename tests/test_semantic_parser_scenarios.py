import pytest

from semantic_parser import ParsedQuestion, SemanticParser, semantic_parser


def parsed(question):
    return semantic_parser.parse(question, allow_llm_fallback=False).to_dict()


@pytest.mark.parametrize(
    ("question", "operation", "metrics", "dimensions", "statuses"),
    [
        ("How many active projects are there?", "count", set(), {"project"}, {"active"}),
        ("Which projects are delayed?", "status_summary", set(), {"project"}, {"delayed"}),
        ("Show the top 10 projects by approved value.", "ranking", {"approved_value"}, {"project"}, set()),
        ("Which site has the highest material consumption this month?", "ranking", {"material_consumption"}, {"site", "material"}, set()),
        ("Which purchase orders are still pending?", "status_summary", set(), {"purchase_order"}, {"pending"}),
        ("Show the top 5 suppliers by total purchase value.", "ranking", {"purchase_value"}, {"vendor"}, set()),
        ("Which vendors delivered late most often?", "ranking", set(), {"vendor"}, {"late"}),
        ("What is the current stock by material?", "breakdown", {"stock"}, {"material"}, set()),
        ("Which materials have negative stock?", "exception_detection", {"stock"}, {"material"}, set()),
        ("Show damaged stock by store.", "breakdown", {"stock"}, {"store"}, {"damaged"}),
        ("What is the total billed amount this month?", "sum", {"billing"}, set(), set()),
        ("Which clients have overdue payments?", "overdue", {"payment"}, {"client"}, {"overdue"}),
        ("Show monthly revenue trend for the last year.", "trend", {"revenue"}, {"date"}, set()),
        ("How many employees are in each department?", "count", set(), {"employee", "department"}, set()),
        ("Which departments have the highest overtime?", "ranking", {"overtime"}, {"department"}, set()),
        ("Show employees with missing attendance.", "exception_detection", {"attendance"}, {"employee"}, {"missing"}),
        ("Which designation has the highest average salary?", "ranking", {"salary"}, {"category"}, set()),
        ("Which machines have the most downtime?", "ranking", {"duration"}, {"machine"}, set()),
        ("Show quality failures by production order.", "breakdown", {"quality_failures"}, {"quality", "production_order"}, set()),
        ("Which customers have the largest overdue balances?", "ranking", {"outstanding_amount"}, {"client"}, {"overdue"}),
        ("What approvals are pending with managers?", "status_summary", set(), {"approval", "manager"}, {"pending"}),
        ("What is this database about?", "metadata", set(), {"database"}, set()),
        ("Which tables contain customer information?", "schema_question", set(), {"client", "database"}, set()),
        ("Where is approval status stored?", "schema_question", set(), {"approval"}, set()),
    ],
)
def test_cross_domain_semantic_roles(question, operation, metrics, dimensions, statuses):
    plan = parsed(question)

    assert plan["operation"] == operation
    assert metrics <= set(plan["metrics"])
    assert dimensions <= set(plan["dimensions"])
    actual_statuses = {value for item in plan["status_constraints"] for value in item["values"]}
    assert statuses <= actual_statuses
    assert plan["candidate_entity_mentions"] == []


@pytest.mark.parametrize(
    "question",
    [
        "Which supplier has the highest purchase value?",
        "Show pending orders.",
        "Show monthly revenue.",
        "Explain project performance.",
        "Show total stock.",
        "Show purchase quantity.",
        "Show completion percentage.",
        "Show average salary.",
        "Show department-wise employee count.",
        "Show the top 5 clients.",
        "Show records from the last 30 days.",
    ],
)
def test_business_phrases_are_never_entity_values(question):
    assert parsed(question)["candidate_entity_mentions"] == []


def test_multi_operation_question_preserves_every_operation_and_constraint():
    plan = parsed("Compare the top five sites by material consumption during the last quarter.")

    assert plan["operation"] == "comparison"
    assert {"ranking", "aggregation", "grouping", "date_filtering"} <= set(plan["sub_operations"])
    assert plan["metrics"] == ["material_consumption"]
    assert "site" in plan["dimensions"]
    assert plan["limit"] == 5
    assert plan["sort_direction"] == "desc"
    assert plan["grouping_hints"] == ["site"]
    assert plan["date_constraints"][0]["type"] == "last_quarter"
    assert plan["candidate_entity_mentions"] == []


def test_question_contract_preserves_rank_status_location_metric_and_financial_year():
    plan = parsed("Show the top 5 delayed projects in Karnataka by approved value for the current financial year.")
    contract = plan["question_contract"]

    assert plan["candidate_entity_mentions"] == ["Karnataka"]
    assert plan["status_constraints"] == [{"type": "status", "values": ["delayed"]}]
    assert contract["required_limit"] == 5
    assert contract["required_statuses"] == ["delayed"]
    assert contract["required_entity_mentions"] == ["Karnataka"]
    assert contract["required_metrics"] == ["approved_value"]
    assert contract["required_ordering"] == ["approved_value desc"]
    assert contract["required_date_constraints"] == ["current_financial_year: specified"]


def test_question_contract_preserves_two_metrics_two_entities_month_grouping_and_range():
    plan = parsed("Compare monthly material issues and returns for Site A and Site B from January to June 2026.")
    contract = plan["question_contract"]

    assert plan["operation"] == "comparison"
    assert {"material_issues", "material_returns"} <= set(plan["metrics"])
    assert plan["candidate_entity_mentions"] == ["Site A", "Site B"]
    assert plan["comparison_entities"] == ["Site A", "Site B"]
    assert plan["grouping_hints"] == ["month"]
    assert plan["date_constraints"][0]["start"] == "2026-01-01"
    assert plan["date_constraints"][0]["end"] == "2026-06-30"
    assert contract["comparison_cardinality"] == 2
    assert contract["required_grouping"] == ["month"]


def test_supplier_status_phrase_is_split_from_entity_value():
    plan = parsed("Show pending orders for supplier ABC Engineering.")

    assert plan["candidate_entity_mentions"] == ["ABC Engineering"]
    assert plan["status_constraints"] == [{"type": "status", "values": ["pending"]}]
    assert {"vendor", "purchase_order"} <= set(plan["dimensions"])


def test_comparison_preserves_identifier_order_and_original_spelling():
    plan = parsed("Compare PO-2025-001 and EMP/0042 for Site-A.")

    assert plan["candidate_entity_mentions"] == ["PO-2025-001", "EMP/0042", "Site-A"]
    assert plan["comparison_entities"] == ["PO-2025-001", "EMP/0042", "Site-A"]
    assert plan["original_question"] == "Compare PO-2025-001 and EMP/0042 for Site-A."


def test_numeric_threshold_and_grouping_do_not_disappear():
    plan = parsed("Which managers have more than five active projects by department?")
    contract = plan["question_contract"]

    assert plan["numeric_constraints"][0]["operator"] == ">"
    assert plan["numeric_constraints"][0]["value"] == 5
    assert "department" in plan["grouping_hints"]
    assert "active" in contract["required_statuses"]
    assert any("project > 5" in value for value in contract["required_filters"])


def test_relative_date_is_not_misread_as_row_limit():
    plan = parsed("Who joined in the last 30 days?")

    assert plan["limit"] is None
    assert plan["date_constraints"] == [{"type": "last_days", "value": 30, "days": 30}]
    assert plan["candidate_entity_mentions"] == []


def test_last_six_months_is_preserved_and_not_an_entity():
    plan = parsed("List vendors with no orders in the last six months.")

    assert plan["date_constraints"] == [{"type": "last_months", "value": 6, "months": 6}]
    assert plan["candidate_entity_mentions"] == []
    assert any(item["operator"] == "missing" for item in plan["filters"])


def test_ageing_constraint_is_structured():
    plan = parsed("Show invoices older than 90 days.")

    assert plan["operation"] == "ageing"
    assert plan["numeric_constraints"] == [{
        "field_concept": "age",
        "operator": ">",
        "value": 90.0,
        "unit": "days",
        "confidence": 0.9,
    }]


def test_inventory_receipts_issues_and_stock_remain_separate_metrics():
    plan = parsed("Compare receipts, issues and closing stock by warehouse.")

    assert plan["operation"] == "comparison"
    assert {"material_receipts", "material_issues", "stock"} <= set(plan["metrics"])
    assert plan["grouping_hints"] == ["store"]
    assert plan["candidate_entity_mentions"] == []


def test_underspecified_performance_is_the_only_kind_that_requests_clarification():
    broad = parsed("Show performance.")
    answerable = parsed("Which department has the highest performance?")

    assert broad["clarification_required"] is True
    assert "underspecified_business_concept" in broad["ambiguity_flags"]
    assert answerable["clarification_required"] is False


def test_valid_llm_fallback_can_only_change_semantic_fields():
    parser = SemanticParser(lambda _question, _contract: {
        "operation": "business_insight",
        "sub_operations": ["comparison"],
        "confidence": 0.7,
    })

    plan = parser.parse("Tell me what matters").to_dict()

    assert plan["fallback_used"] is True
    assert plan["operation"] == "business_insight"
    assert plan["sub_operations"] == ["comparison"]


@pytest.mark.parametrize(
    "fallback_payload",
    [
        {"operation": "lookup", "sql": "SELECT * FROM users"},
        {"operation": "lookup", "tables": ["users"]},
        {"operation": "not_real"},
        "not json",
    ],
)
def test_invalid_or_schema_inventing_llm_fallback_is_ignored(fallback_payload):
    parser = SemanticParser(lambda _question, _contract: fallback_payload)

    plan = parser.parse("Tell me what matters").to_dict()

    assert plan["fallback_used"] is False
    assert plan["operation"] == "unknown"


def test_llm_fallback_failure_returns_deterministic_parse():
    def unavailable(_question, _contract):
        raise RuntimeError("provider unavailable")

    plan = SemanticParser(unavailable).parse("Tell me what matters").to_dict()

    assert plan["fallback_used"] is False
    assert plan["operation"] == "unknown"


def test_new_compiler_style_model_keeps_legacy_contract():
    plan = semantic_parser.parse("Top 3 customers by revenue")

    assert isinstance(plan, ParsedQuestion)
    payload = plan.to_dict()
    assert payload["aggregation_specs"]
    assert payload["ordering"][0]["direction"] == "desc"
    assert payload["sort_direction"] == "desc"
    assert payload["requested_output"] == ["client"]
    assert payload["question_contract"]["required_limit"] == 3
