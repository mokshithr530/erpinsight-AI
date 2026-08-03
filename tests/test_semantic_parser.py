from semantic_parser import SemanticQueryPlanInput, semantic_parser


def test_semantic_parser_ranking_purchase_value_excludes_entity_phrase():
    plan = semantic_parser.parse("Which vendor has the highest purchase value?").to_dict()

    assert plan["operation"] == "ranking"
    assert "purchase_value" in plan["metrics"]
    assert "vendor" in plan["dimensions"]
    assert "sum" in plan["aggregations"]
    assert plan["sort_direction"] == "desc"
    assert plan["limit"] == 1
    assert plan["candidate_entity_mentions"] == []


def test_semantic_parser_treats_plural_purchases_as_metric():
    plan = semantic_parser.parse("what purchases happened for Mission Bhagiratha").to_dict()

    assert "purchase_value" in plan["metrics"]
    assert plan["candidate_entity_mentions"] == ["Mission Bhagiratha"]


def test_semantic_parser_top_clients_by_billing_has_no_entity_mentions():
    plan = semantic_parser.parse("give me top clients by billing").to_dict()

    assert plan["operation"] == "ranking"
    assert plan["metrics"] == ["billing"]
    assert plan["dimensions"] == ["client"]
    assert plan["candidate_entity_mentions"] == []


def test_semantic_parser_relationship_lookup_preserves_entity_anchor():
    plan = semantic_parser.parse("Who are the vendors for RDSS-VIZAG?").to_dict()

    assert plan["operation"] == "relationship_lookup"
    assert plan["dimensions"] == ["vendor"]
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG"]
    assert plan["requested_output"] == ["vendor"]


def test_semantic_parser_comparison_preserves_mention_order_and_year():
    plan = semantic_parser.parse("Compare RDSS-VIZAG and APDRP billing in 2025.").to_dict()

    assert plan["operation"] == "comparison"
    assert "billing" in plan["metrics"]
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG", "APDRP"]
    assert plan["date_constraints"] == [{
        "type": "year",
        "value": 2025,
        "start": "2025-01-01",
        "end": "2025-12-31",
    }]


def test_semantic_parser_extracts_limits_status_and_relative_dates():
    plan = semantic_parser.parse("Show top 5 pending invoices for ABC Industries in the last 30 days").to_dict()

    assert plan["limit"] == 5
    assert plan["status_constraints"] == [{"type": "status", "values": ["pending"]}]
    assert {"type": "last_days", "value": 30, "days": 30} in plan["date_constraints"]
    assert plan["candidate_entity_mentions"] == ["ABC Industries"]


def test_semantic_parser_available_count_is_not_status_filter():
    plan = semantic_parser.parse("How many purchase orders are available?").to_dict()

    assert plan["operation"] == "count"
    assert "purchase_order" in plan["dimensions"]
    assert plan["status_constraints"] == []
    assert plan["filters"] == []


def test_semantic_parser_preserves_true_status_filter():
    plan = semantic_parser.parse("Show pending purchase orders").to_dict()

    assert plan["status_constraints"] == [{"type": "status", "values": ["pending"]}]
    assert "pending" in plan["question_contract"]["required_statuses"]


def test_semantic_parser_by_state_is_grouping_dimension():
    plan = semantic_parser.parse("How many projects are there by state?").to_dict()

    assert plan["operation"] == "count"
    assert "project" in plan["dimensions"]
    assert "location" in plan["dimensions"]
    assert "location" in plan["grouping_hints"]
    assert "location" in plan["question_contract"]["required_grouping"]


def test_semantic_parser_generic_value_metric_is_preserved():
    plan = semantic_parser.parse("List the top 10 purchase orders by total value.").to_dict()

    assert plan["operation"] == "ranking"
    assert "purchase_order" in plan["dimensions"]
    assert "purchase_value" in plan["metrics"]
    assert plan["limit"] == 10
    assert plan["sort_direction"] == "desc"


def test_semantic_parser_existence_questions_preserve_count():
    plan = semantic_parser.parse("Are there any purchase orders in the database?").to_dict()

    assert plan["operation"] == "existence_check"
    assert "count" in plan["aggregations"]
    assert "purchase_order" in plan["dimensions"]


def test_semantic_parser_show_by_status_is_distribution_count():
    plan = semantic_parser.parse("Show projects by status.").to_dict()

    assert plan["operation"] == "distribution"
    assert "count" in plan["aggregations"]
    assert "project" in plan["dimensions"]
    assert "status" in plan["dimensions"]
    assert "status" in plan["grouping_hints"]
    assert plan["sort_direction"] == "desc"
    assert plan["limit"] == 20


def test_semantic_parser_show_by_transaction_type_is_distribution_count():
    plan = semantic_parser.parse("Show store issues by transaction type.").to_dict()

    assert "count" in plan["aggregations"]
    assert "type" in plan["dimensions"]
    assert "type" in plan["grouping_hints"]


def test_semantic_parser_top_metric_question_keeps_metric_ranking():
    plan = semantic_parser.parse("Show top 5 vendors by purchase value.").to_dict()

    assert plan["operation"] == "ranking"
    assert "purchase_value" in plan["metrics"]
    assert "sum" in plan["aggregations"]
    assert plan["limit"] == 5


def test_manager_kpi_aliases_are_metrics_not_entities():
    cases = [
        (
            "Which supplier has the highest procurement spend?",
            "purchase_value",
            "vendor",
        ),
        (
            "Show revenue booked for RDSS-VIZAG.",
            "billing",
            None,
        ),
        (
            "Which project has the biggest contract amount?",
            "project_value",
            "project",
        ),
        (
            "Show site consumption for RDSS-VIZAG.",
            "material_consumption",
            None,
        ),
        (
            "What is the stock on hand?",
            "stock",
            None,
        ),
        (
            "How much issued material value was used?",
            "material_issue_value",
            None,
        ),
    ]

    for question, metric, dimension in cases:
        plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
        assert metric in plan["metrics"]
        if dimension:
            assert dimension in plan["dimensions"]
        assert not any(
            alias in plan["candidate_entity_mentions"]
            for alias in [
                "highest procurement spend",
                "revenue booked",
                "contract amount",
                "site consumption",
                "stock on hand",
                "issued material value",
            ]
        )


def test_semantic_parser_preserves_plain_list_limits():
    cases = [
        ("show 5 projects", 5, "project"),
        ("name any 10 material", 10, "material"),
        ("list any five vendors", 5, "vendor"),
        ("give me 20 purchase orders", 20, "purchase_order"),
        ("show latest 5 bills", 5, "invoice"),
    ]

    for question, limit, dimension in cases:
        plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
        assert plan["limit"] == limit
        assert dimension in plan["dimensions"]
        assert plan["operation"] in {"lookup", "relationship_lookup"}


def test_semantic_parser_does_not_treat_any_n_business_objects_as_entities():
    plan = semantic_parser.parse(
        "show stock available for any 5 materials",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["limit"] == 5
    assert "stock" in plan["metrics"]
    assert "material" in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == []


def test_semantic_parser_ignores_clarification_wording_as_entity_or_status():
    plan = semantic_parser.parse(
        "tell me materials used in RDSS-VIZAG last 30 days "
        "latest 30 days available in the database material issued quantity",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["status_constraints"] == []
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG"]


def test_semantic_parser_between_dates_and_financial_year():
    between = semantic_parser.parse("Revenue between 2025-01-01 and 2025-01-31").to_dict()
    fy = semantic_parser.parse("billing for current financial year").to_dict()

    assert {"type": "between", "start": "2025-01-01", "end": "2025-01-31"} in between["date_constraints"]
    assert {"type": "current_financial_year"} in fy["date_constraints"]


def test_semantic_parser_explicit_financial_year_has_april_to_march_bounds():
    plan = semantic_parser.parse(
        "Show billing in 2025 for financial year 2025-26."
    ).to_dict()

    assert plan["date_constraints"] == [
        {
            "type": "financial_year",
            "value": "2025-26",
            "start": "2025-04-01",
            "end": "2026-03-31",
        }
    ]


def test_business_acronyms_are_not_entity_values():
    for question in (
        "What is the total GRN value?",
        "How many BOQ billing lines are available?",
        "Show materials by UOM.",
        "What is the total employee CTC?",
    ):
        assert (
            semantic_parser.parse(question).candidate_entity_mentions
            == []
        )


def test_manager_wording_between_projects_preserves_clean_entities():
    plan = semantic_parser.parse(
        "Between RDSS-VIZAG and RDSS-UGCABLE, which one has more billing?",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["operation"] == "comparison"
    assert "billing" in plan["metrics"]
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG", "RDSS-UGCABLE"]


def test_manager_wording_biggest_pos_by_amount_maps_to_purchase_value():
    plan = semantic_parser.parse(
        "Show the biggest five POs by amount.",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["operation"] == "ranking"
    assert plan["limit"] == 5
    assert plan["sort_direction"] == "desc"
    assert "purchase_value" in plan["metrics"]
    assert "purchase_order" in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == []


def test_recent_day_window_is_date_not_material_entity():
    plan = semantic_parser.parse(
        "In the recent 30 day window, which materials were issued most?",
        allow_llm_fallback=False,
    ).to_dict()

    assert {"type": "last_days", "value": 30, "days": 30} in plan["date_constraints"]
    assert "material_issues" in plan["metrics"]
    assert "material" in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == []


def test_top_materials_used_from_stores_maps_to_issued_quantity():
    plan = semantic_parser.parse(
        "Give top materials used from stores.",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["operation"] == "ranking"
    assert plan["limit"] == 10
    assert "material_consumption" in plan["metrics"]
    assert "material" in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == []


def test_po_spend_alias_maps_to_purchase_value():
    plan = semantic_parser.parse(
        "Compare PO spend for RDSS-VIZAG versus RDSS-UGCABLE.",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["operation"] == "comparison"
    assert "purchase_value" in plan["metrics"]
    assert "project" in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG", "RDSS-UGCABLE"]


def test_cross_module_project_comparison_preserves_project_dimension():
    plan = semantic_parser.parse(
        "Compare billing and PO value for RDSS-VIZAG and RDSS-UGCABLE.",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["operation"] == "comparison"
    assert {"billing", "purchase_value"} <= set(plan["metrics"])
    assert "project" in plan["dimensions"]
    assert "purchase_order" not in plan["dimensions"]
    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG", "RDSS-UGCABLE"]


def test_semantic_parser_strips_leading_preposition_from_project_mentions():
    plan = semantic_parser.parse("what is going on in NH-930 GOWRELLY-VALIGONDA").to_dict()

    assert "NH-930 GOWRELLY-VALIGONDA" in plan["candidate_entity_mentions"]
    assert "in NH-930 GOWRELLY-VALIGONDA" not in plan["candidate_entity_mentions"]


def test_semantic_parser_removes_short_mentions_contained_in_longer_names():
    plan = semantic_parser.parse("compare billing between KHAMMAM ROADWORKS and TSIIC").to_dict()

    assert plan["candidate_entity_mentions"] == ["KHAMMAM ROADWORKS", "TSIIC"]


def test_semantic_parser_does_not_extract_manager_pronouns_or_generic_phrases():
    generic_questions = [
        "Give me a quick executive summary of what this ERP database is about and what a project manager can analyze from it.",
        "How many projects do we have overall, and can you show a few examples so I know the count is coming from the right place?",
        "Which departments have the most employees, and what should a project manager understand from that?",
        "Who are the vendors in this database? Give me a useful list, not technical table names.",
    ]

    for question in generic_questions:
        plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
        assert plan["candidate_entity_mentions"] == []


def test_semantic_parser_trims_project_mention_before_requested_outputs():
    plan = semantic_parser.parse(
        "Give me details about RDSS-VIZAG project including value, client, dates, state, and anything useful you can safely find.",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG"]


def test_semantic_parser_keeps_cross_module_project_entity_clean():
    plan = semantic_parser.parse(
        "for RDSS-VIZAG show billing PO value and material issued",
        allow_llm_fallback=False,
    ).to_dict()

    assert plan["candidate_entity_mentions"] == ["RDSS-VIZAG"]
    assert "purchase_value" in plan["metrics"]
    assert "material_issues" in plan["metrics"]


def test_semantic_parser_issue_metric_does_not_force_count_grouping():
    plan = semantic_parser.parse(
        "show PO value and material issue value by project",
        allow_llm_fallback=False,
    ).to_dict()

    assert "purchase_value" in plan["metrics"]
    assert "material_issue_value" in plan["metrics"]
    assert "project" in plan["dimensions"]
    assert "count" not in [str(item).lower() for item in plan["aggregations"]]


def test_semantic_parser_ignores_manager_presentation_phrases():
    cases = [
        "Please list 5 projects with their project value for demo.",
        "For management review, show project wise billing and purchase value.",
        "Show PO value and material issue value by project and explain what a manager should notice.",
    ]

    for question in cases:
        plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
        assert plan["candidate_entity_mentions"] == []
        assert "manager" not in plan["dimensions"]
        assert "manager" not in plan["requested_output"]


def test_semantic_parser_keeps_real_manager_dimension():
    plan = semantic_parser.parse(
        "What approvals are pending with managers?",
        allow_llm_fallback=False,
    ).to_dict()

    assert "manager" in plan["dimensions"]
    assert {"type": "status", "values": ["pending"]} in plan["status_constraints"]


def test_semantic_query_plan_input_is_structured():
    plan = semantic_parser.parse("List first 10 vendors")

    assert isinstance(plan, SemanticQueryPlanInput)
    payload = plan.to_dict()
    assert payload["version"]
    assert payload["operation"] in {
        "lookup",
        "relationship_lookup",
        "count",
        "aggregation",
        "ranking",
        "comparison",
        "trend",
        "distribution",
        "existence_check",
        "metadata",
        "business_insight",
        "missing_kpi",
        "unknown",
    }
