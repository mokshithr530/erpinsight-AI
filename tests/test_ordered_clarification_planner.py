from clarification_planner import ordered_clarification_planner
from semantic_parser import semantic_parser


def _plan(question, entity_resolution=None):
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    return ordered_clarification_planner.plan(
        question=question,
        semantic_plan=semantic_plan,
        entity_resolution=entity_resolution or {},
        semantic_binding={},
    )


def test_unknown_entity_type_is_clarified_before_metric():
    plan = _plan("Show purchase orders for ABC.")

    assert plan.required
    assert plan.module == "purchase_orders"
    assert plan.next_slot == "entity_type"
    assert "ABC" in plan.question
    assert "project" in plan.question
    assert "vendor" in plan.question


def test_missing_comparison_entities_before_metric():
    plan = _plan("Compare purchase orders.")

    assert plan.required
    assert plan.next_slot == "entity_pair"
    assert "projects or vendors" in plan.question
    assert "RDSS-VIZAG and RDSS-UGCABLE" in plan.options


def test_compare_vendors_does_not_ask_for_projects():
    plan = _plan("Compare vendors.")

    assert plan.required
    assert plan.module == "vendors"
    assert plan.next_slot == "entity_pair"
    assert "vendors" in plan.question.lower()
    assert "project" not in plan.question.lower()
    assert "POLYCAB INDIA LIMITED and KEI Industries" in plan.options


def test_inventory_abc_distinguishes_material_project_store():
    plan = _plan("Show inventory for ABC.")

    assert plan.required
    assert plan.next_slot == "entity_type"
    assert "material" in plan.question
    assert "project" in plan.question
    assert "store" in plan.question


def test_store_material_abc_distinguishes_project_material():
    plan = _plan("Show material issued for ABC.")

    assert plan.required
    assert plan.next_slot == "entity_type"
    assert "project" in plan.question
    assert "material" in plan.question


def test_billing_abc_distinguishes_project_client():
    plan = _plan("Show billing for ABC.")

    assert plan.required
    assert plan.next_slot == "entity_type"
    assert "project" in plan.question
    assert "client" in plan.question


def test_show_project_details_asks_for_project():
    plan = _plan("Show project details.")

    assert plan.required
    assert plan.next_slot == "entity"
    assert plan.question == "Which project should I show?"
    assert "RDSS-VIZAG" in plan.options


def test_top_vendors_asks_metric_only():
    plan = _plan("Show top vendors.")

    assert plan.required
    assert plan.next_slot == "metric"
    assert "PO total value" in plan.options
    assert "PO count" in plan.options


def test_latest_bills_asks_limit_when_no_default_exists():
    plan = _plan("Show latest bills.")

    assert plan.required
    assert plan.next_slot == "limit"
    assert "How many recent bills" in plan.question


def test_unsupported_vendor_performance_safe_fails_before_entities():
    plan = _plan("Compare vendor performance.")

    assert plan.required
    assert plan.safe_unsupported
    assert "not currently defined" in plan.question
    assert "PO total value" in plan.question


def test_complete_query_bypasses_clarification():
    plan = _plan(
        "Show purchase order total value by project for RDSS-VIZAG.",
        entity_resolution={"matches": [{"entity": "RDSS-VIZAG"}]},
    )

    assert not plan.required


def test_entity_pair_mentions_fill_compare_slot_then_metric_is_next():
    plan = _plan("Compare purchase orders for RDSS-VIZAG and RDSS-UGCABLE.")

    assert plan.required
    assert plan.next_slot == "metric"


def test_material_movement_project_scope_does_not_repeat_scope_question():
    plan = _plan("Show material movement for project.")

    assert not (plan.required and plan.next_slot == "entity_scope")


def test_low_stock_threshold_does_not_repeat_threshold_question():
    plan = _plan("Show low stock items below 10.")

    assert not (plan.required and plan.next_slot == "threshold")


def test_month_without_year_requires_date_range():
    plan = _plan("Show purchase orders from March.")

    assert plan.required
    assert plan.next_slot == "date_range"
    assert "year or date range" in plan.question


def test_candidate_ambiguity_is_asked_as_entity_type_first():
    plan = _plan(
        "Show billing for ABC.",
        entity_resolution={"clarification_required": True, "ambiguities": [{"mention": "ABC"}]},
    )

    assert plan.next_slot == "entity_type"


def test_optional_total_billing_does_not_block():
    plan = _plan("What is the total billing?")

    assert not plan.required


def test_active_projects_asks_status_meaning_only():
    plan = _plan("Show active projects.")

    assert plan.required
    assert plan.next_slot == "project_status"
    assert "status" in plan.question.lower()
