from erp_semantic_binder import erp_semantic_binder
from query_plan import query_plan_guard
from semantic_parser import semantic_parser
from verified_semantic_catalog import load_verified_semantic_catalog


ERP_SCHEMA_FINGERPRINT = "998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5"


def _catalog():
    verified = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    return {
        "verified_semantic_catalog": verified,
        "catalog_validation": verified["validation"],
        "catalog_authority": verified["authority"],
        "catalog_version": verified["catalog_version"],
        "erp_schema_version": verified["erp_schema_version"],
        "metrics": verified["metrics"],
        "dimensions": verified["dimensions"],
        "date_fields": verified["date_fields"],
        "status_fields": verified["status_fields"],
        "join_paths": verified["join_paths"],
        "business_formulas": verified["business_formulas"],
    }


def _bind(question):
    semantic_plan = semantic_parser.parse(
        question,
        allow_llm_fallback=False,
    ).to_dict()
    binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=_catalog(),
    ).to_dict()
    return semantic_plan, binding


def test_binds_project_value_ranking_to_verified_erp_mapping():
    semantic_plan, binding = _bind("Show top 5 projects by project value.")

    assert semantic_plan["metrics"] == ["project_value"]
    assert binding["authoritative"] is True
    assert binding["clarification_required"] is False
    assert binding["bound_metrics"][0]["metric"] == "project_value"
    assert binding["bound_metrics"][0]["table"] == "project_mst"
    assert binding["bound_metrics"][0]["column"] == "project_value"
    assert binding["aggregation_grain"] == ["project"]


def test_binds_vendor_purchase_value_without_treating_phrase_as_entity():
    semantic_plan, binding = _bind("Which vendor has the highest purchase value?")

    assert semantic_plan["candidate_entity_mentions"] == []
    assert binding["bound_metrics"][0]["metric"] == "purchase_value"
    assert binding["bound_metrics"][0]["table"] == "po_form"
    assert binding["bound_metrics"][0]["column"] == "total_value"
    assert binding["bound_dimensions"][0]["dimension"] == "vendor"
    assert (
        binding["bound_business_definitions"][0]["concept_id"]
        == "vendor_purchase_value"
    )


def test_binds_manager_kpi_aliases_to_verified_metrics():
    cases = [
        (
            "Which supplier has the highest procurement spend?",
            "purchase_value",
            "po_form",
            "total_value",
        ),
        (
            "Show revenue booked for RDSS-VIZAG.",
            "billing",
            "client_bill_raising",
            "gross_bill_value",
        ),
        (
            "Which project has the biggest contract amount?",
            "project_value",
            "project_mst",
            "project_value",
        ),
        (
            "Show site consumption for RDSS-VIZAG.",
            "material_consumption",
            "store_material_usage_quantity",
            "issued_quantity",
        ),
        (
            "What is the stock on hand?",
            "current_stock",
            "inventory_master",
            "available_quantity",
        ),
        (
            "How much issued material value was used?",
            "material_issue_value",
            "store_material_usage_quantity",
            "base_charge",
        ),
    ]

    for question, metric, table, column in cases:
        semantic_plan, binding = _bind(question)
        assert binding["clarification_required"] is False
        assert binding["bound_metrics"][0]["metric"] == metric
        assert binding["bound_metrics"][0]["table"] == table
        assert binding["bound_metrics"][0]["column"] == column
        assert all(
            item not in semantic_plan["candidate_entity_mentions"]
            for item in [
                "highest procurement spend",
                "revenue booked",
                "contract amount",
                "site consumption",
                "stock on hand",
                "issued material value",
            ]
        )


def test_cross_module_project_entity_prunes_detail_dimensions():
    semantic_plan = semantic_parser.parse(
        "for RDSS-VIZAG show billing PO value and material issued",
        allow_llm_fallback=False,
    ).to_dict()
    binding = erp_semantic_binder.bind(
        question="for RDSS-VIZAG show billing PO value and material issued",
        semantic_plan=semantic_plan,
        catalog=_catalog(),
        entity_resolution={
            "entities": [{
                "entity_type": "project",
                "resolved_type": "project",
                "table": "project_mst",
                "column": "project_name",
                "primary_key_column": "project_id",
                "primary_key_value": 2,
                "matched_value": "RDSS-VIZAG",
            }],
            "matches": [],
        },
    ).to_dict()

    metrics = {item["metric"] for item in binding["bound_metrics"]}
    dimensions = {item["dimension"] for item in binding["bound_dimensions"]}

    assert {"billing", "purchase_value", "material_consumption"} <= metrics
    assert "purchase_order" not in dimensions
    assert "material" not in dimensions
    assert "project_mst" in binding["required_tables"]
    assert "store_usage_quantity_master" in binding["required_tables"]


def test_cross_module_manager_aliases_bind_verified_metrics():
    semantic_plan = semantic_parser.parse(
        "project dashboard with billed amount procurement spend and material spent by project",
        allow_llm_fallback=False,
    ).to_dict()
    binding = erp_semantic_binder.bind(
        question="project dashboard with billed amount procurement spend and material spent by project",
        semantic_plan=semantic_plan,
        catalog=_catalog(),
        entity_resolution={"entities": [], "matches": []},
    ).to_dict()

    metrics = {item["metric"] for item in binding["bound_metrics"]}

    assert {
        "project_value",
        "billing",
        "purchase_value",
        "material_issue_value",
    } <= metrics
    assert binding["clarification_required"] is False


def test_binds_vendor_master_lookup_and_count_to_verified_vendor_module():
    _, count_binding = _bind("How many vendors are there?")
    _, details_binding = _bind("Show vendor details for KEI Industries.")
    _, state_binding = _bind("Show vendors by state.")
    _, category_binding = _bind("Show vendors by category.")

    assert count_binding["bound_dimensions"][0]["table"] == "vendor_mst"
    assert count_binding["bound_business_definitions"][0]["concept_id"] == "vendor_count"
    assert details_binding["bound_dimensions"][0]["table"] == "vendor_mst"
    assert state_binding["bound_dimensions"][-1]["table"] == "vendor_branch_details"
    assert category_binding["bound_dimensions"][-1]["table"] == "vendor_types"


def test_unverified_vendor_analytics_require_clarification():
    for question, concept in (
        ("Which vendor performs best?", "vendor_performance"),
        ("Show vendor payment history.", "vendor_payment_history"),
        ("Give vendor credit analysis.", "vendor_credit_analysis"),
        ("Show vendor delivery KPI.", "vendor_delivery_kpi"),
    ):
        _, binding = _bind(question)

        assert binding["clarification_required"] is True
        assert any(
            item["concept"] == concept
            for item in binding["unresolved_concepts"]
        )


def test_binds_billing_period_and_approved_project_relationship():
    _, binding = _bind("Compare billing across projects in 2025.")

    assert binding["bound_metrics"][0]["column"] == "gross_bill_value"
    assert binding["bound_date_fields"][0]["table"] == "client_bill_raising"
    assert binding["bound_date_fields"][0]["column"] == "bill_date"
    assert any(
        relationship["left_table"] == "client_bill_raising"
        and relationship["right_table"] == "project_mst"
        for relationship in binding["approved_relationships"]
    )


def test_binds_exact_bill_amount_without_treating_bill_as_invoice_output():
    semantic_plan, binding = _bind("Show total bill amount by month for 2025.")

    assert "invoice" not in semantic_plan["dimensions"]
    assert binding["bound_metrics"][0]["column"] == "bill_amount"
    assert binding["bound_date_fields"][0]["column"] == "bill_date"
    assert binding["clarification_required"] is False
    assert (
        binding["bound_business_definitions"][0]["concept_id"]
        == "bill_amount"
    )


def test_binds_verified_project_state_and_purchase_order_identity():
    _, state_binding = _bind("How many projects are there by state?")
    _, po_binding = _bind("How many purchase orders are available?")

    assert state_binding["bound_dimensions"][-1]["column"] == "project_state"
    assert state_binding["clarification_required"] is False
    assert po_binding["bound_dimensions"][0]["column"] == "po_form_id"
    assert po_binding["clarification_required"] is False


def test_binds_verified_grn_value_without_entity_resolution():
    semantic_plan, binding = _bind("What is the total GRN value?")

    assert semantic_plan["candidate_entity_mentions"] == []
    assert binding["bound_metrics"][0]["metric"] == "grn_value"
    assert binding["bound_metrics"][0]["table"] == (
        "goods_receipt_cum_insp_note"
    )
    assert binding["bound_metrics"][0]["column"] == "value"
    assert binding["clarification_required"] is False


def test_binds_current_stock_to_verified_inventory_mapping():
    semantic_plan, binding = _bind("Show top 5 materials by current stock.")

    assert semantic_plan["candidate_entity_mentions"] == []
    assert binding["authoritative"] is True
    assert binding["clarification_required"] is False
    assert binding["bound_metrics"][0]["metric"] == "current_stock"
    assert binding["bound_metrics"][0]["table"] == "inventory_master"
    assert binding["bound_metrics"][0]["column"] == "available_quantity"
    assert binding["bound_dimensions"][0]["table"] == "sub_material"
    assert any(
        relationship["left_table"] == "inventory_master"
        and relationship["right_table"] == "sub_material"
        for relationship in binding["approved_relationships"]
    )


def test_binds_available_inventory_quantity_without_status_clarification():
    semantic_plan, binding = _bind("What is the total available inventory quantity?")

    assert semantic_plan["status_constraints"] == []
    assert binding["clarification_required"] is False
    assert binding["bound_metrics"][0]["metric"] == "current_stock"
    assert binding["bound_metrics"][0]["table"] == "inventory_master"
    assert binding["bound_metrics"][0]["column"] == "available_quantity"


def test_unverified_inventory_value_and_project_stock_require_clarification():
    _, value_binding = _bind("What is the inventory stock value?")
    _, project_binding = _bind("Show stock by project.")

    assert value_binding["clarification_required"] is True
    assert any(
        item["concept"] == "stock_value"
        for item in value_binding["unresolved_concepts"]
    )
    assert project_binding["clarification_required"] is True
    assert any(
        item["concept"] == "project_stock"
        for item in project_binding["unresolved_concepts"]
    )


def test_count_business_lines_uses_record_identity_not_amount_metric():
    semantic_plan, binding = _bind(
        "How many BOQ billing lines are available?"
    )

    assert semantic_plan["metrics"] == []
    assert binding["bound_metrics"] == []
    assert binding["bound_dimensions"][0]["dimension"] == (
        "boq_billing_line"
    )
    assert binding["bound_dimensions"][0]["column"] == (
        "client_bills_boq_wisedetails_id"
    )


def test_binds_workorder_after_tax_total_without_fake_tax_metric():
    semantic_plan, binding = _bind(
        "What is the average workorder after-tax total?"
    )

    assert semantic_plan["metrics"] == ["workorder_value"]
    assert binding["bound_metrics"][0]["column"] == (
        "after_taxes_grandtotal"
    )
    assert binding["clarification_required"] is False


def test_binds_employee_count_by_department_with_verified_join():
    _, binding = _bind("Show employee count by department.")

    assert binding["bound_metrics"][0]["metric"] == "employee_count"
    assert binding["bound_metrics"][0]["default_aggregation"] == "distinct_count"
    assert binding["bound_dimensions"][-1]["dimension"] == "department"
    assert any(
        relationship["left_table"] == "employee_table"
        and relationship["right_table"] == "department"
        for relationship in binding["approved_relationships"]
    )


def test_undefined_department_performance_requires_metric_clarification():
    _, binding = _bind("Which department performs best?")

    assert binding["clarification_required"] is True
    question = next(
        item
        for item in binding["clarification_questions"]
        if item["concept"] == "department_performance"
    )
    assert question["options"] == [
        "employee count",
        "attendance count",
    ]


def test_missing_profit_loss_formula_is_not_guessed():
    _, binding = _bind("What is the company loss?")

    assert binding["clarification_required"] is True
    assert any(
        item["concept"] == "profit_loss"
        for item in binding["unresolved_concepts"]
    )
    assert binding["confidence_score"] <= 55


def test_context_free_status_and_date_require_clarification():
    _, binding = _bind("Show pending records from the last 30 days.")

    assert binding["bound_status_fields"] == []
    assert binding["bound_date_fields"] == []
    assert {
        item["concept"]
        for item in binding["clarification_questions"]
    } == {"status", "date"}


def test_unverified_numeric_status_labels_require_clarification():
    _, binding = _bind("Show active employees.")

    assert binding["bound_status_fields"] == []
    assert binding["clarification_required"] is True
    assert any(
        item["concept"] == "status"
        for item in binding["clarification_questions"]
    )


def test_verified_text_purchase_order_status_binds_without_guessing():
    _, binding = _bind("Show open purchase orders.")

    assert binding["bound_status_fields"][0]["column"] == "is_po_item_status"
    assert binding["bound_status_fields"][0]["requested_values"] == ["open"]
    assert binding["clarification_required"] is False


def test_unsupported_management_kpis_require_clarification():
    for question, concept in (
        ("What is the profit margin?", "margin"),
        ("Which project has the highest risk score?", "risk_score"),
        ("Which project performs best?", "performance"),
        ("Which project cost the most?", "project_cost"),
    ):
        _, binding = _bind(question)

        assert binding["clarification_required"] is True
        assert any(
            item["concept"] == concept
            for item in binding["unresolved_concepts"]
        )


def test_missing_authoritative_catalog_uses_fallback_without_fake_bindings():
    semantic_plan = semantic_parser.parse(
        "Show top 5 projects by project value.",
        allow_llm_fallback=False,
    ).to_dict()

    binding = erp_semantic_binder.bind(
        question="Show top 5 projects by project value.",
        semantic_plan=semantic_plan,
        catalog={},
    ).to_dict()

    assert binding["authoritative"] is False
    assert binding["fallback_required"] is True
    assert binding["bound_metrics"] == []


def test_typed_query_plan_preserves_authoritative_bindings():
    semantic_plan, binding = _bind(
        "Which vendor has the highest purchase value?"
    )
    plan = query_plan_guard.build(
        question="Which vendor has the highest purchase value?",
        semantic_plan=semantic_plan,
        entity_resolution={"entities": [], "matches": [], "unresolved": []},
        retrieval={
            "selected_tables": ["po_form"],
            "relevant_columns": {"po_form": ["vendor_name", "total_value"]},
            "relationship_paths": [],
        },
        semantic_binding=binding,
    )

    assert plan.catalog_authoritative is True
    assert plan.bound_metrics[0]["column"] == "total_value"
    assert set(plan.selected_columns["po_form"]) == {"vendor_name", "total_value"}
    assert plan.aggregation_grain == ["purchase_order_header", "vendor"]
    assert query_plan_guard.validate_plan(plan)["valid"] is True


def test_binder_never_uses_relationship_without_runtime_approval():
    question = "Compare billing across projects."
    semantic_plan = semantic_parser.parse(
        question,
        allow_llm_fallback=False,
    ).to_dict()
    catalog = _catalog()
    relationship = next(
        item
        for item in catalog["join_paths"]
        if item["left_table"] == "client_bill_raising"
        and item["right_table"] == "project_mst"
    )
    relationship["runtime_approved"] = False

    binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=catalog,
    ).to_dict()

    assert binding["approved_relationships"] == []
    assert binding["clarification_required"] is True
    assert any(
        item["role"] == "relationship"
        for item in binding["unresolved_concepts"]
    )
