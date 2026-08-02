import time

import pytest

from business_catalog import BusinessCatalog
from schema_retriever import SchemaRetriever
from semantic_parser import semantic_parser
from schema_profile_fixtures import (
    column,
    construction_schema,
    finance_schema,
    hr_schema,
    inventory_schema,
    large_schema,
    legacy_schema,
    manufacturing_schema,
    minimal_fk_schema,
    obfuscated_schema,
    sales_schema,
    table,
)


def retrieve(tmp_path, question, schema, *, name="case", entity_resolution=None, candidate_limit=8):
    row_counts = {table_name: info.get("row_count") for table_name, info in schema.items()}
    fingerprint = f"retrieval-{name}"
    catalog = BusinessCatalog().build(
        schema,
        source_type="synthetic",
        row_counts=row_counts,
        fingerprint=fingerprint,
    )
    retriever = SchemaRetriever(profile_dir=tmp_path)
    plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    result = retriever.retrieve(
        question=question,
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint=fingerprint,
        semantic_plan=plan,
        entity_resolution=entity_resolution or {},
        candidate_limit=candidate_limit,
    )
    profiles = retriever.profiles_for(
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint=fingerprint,
    )
    return retriever, plan, result, retriever.compact_prompt(result, profiles)


@pytest.mark.parametrize(
    ("name", "schema_factory", "question", "expected_top"),
    [
        ("erp", construction_schema, "Top 5 projects by approved value", "ProjectMaster"),
        ("hr", hr_schema, "Average overtime hours by department", "TimeSheetEntries"),
        ("finance", finance_schema, "Outstanding balance by customer in 2025", "sales_invoices"),
        ("inventory", inventory_schema, "Returned quantity by item last month", "inventory_movements"),
        ("manufacturing", manufacturing_schema, "Average downtime hours by machine", "machine_downtime_events"),
        ("legacy", legacy_schema, "Top 3 projects by approved amount", "t_prj_hdr"),
        ("obfuscated", obfuscated_schema, "Top customers by invoice amount in 2025", "z77"),
        ("minimal_fk", minimal_fk_schema, "Total hours by team", "work_log"),
    ],
)
def test_cross_domain_top_table_accuracy(tmp_path, name, schema_factory, question, expected_top):
    _retriever, _plan, result, _prompt = retrieve(tmp_path / name, question, schema_factory(), name=name)

    assert result.ranked_tables[0].table == expected_top
    assert expected_top in result.selected_tables
    assert result.ranked_tables[0].reasons
    assert result.ranked_tables[0].signal_scores


def test_question_contract_and_all_explicit_constraints_are_preserved(tmp_path):
    question = "Top 5 delayed projects in 2025 by approved value"
    _retriever, plan, result, prompt = retrieve(tmp_path, question, construction_schema(), name="contract")

    assert result.required_constraints["limit"] == 5
    assert result.required_constraints["sort_direction"] == "desc"
    assert result.required_constraints["date_constraints"] == plan["date_constraints"]
    assert result.required_constraints["status_constraints"] == plan["status_constraints"]
    assert result.required_constraints["question_contract"] == plan["question_contract"]
    assert result.constraint_coverage["metric:approved value"]["mapped"] is True
    assert result.constraint_coverage["status:delayed"]["column"] == "ProjectStatus"
    assert "Preserve every constraint" in prompt


def test_metrics_and_dimensions_are_ranked_as_different_column_roles(tmp_path):
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Outstanding balance by customer",
        finance_schema(),
        name="roles",
    )

    metric = result.constraint_coverage["metric:outstanding amount"]
    dimension = result.constraint_coverage["dimension:client"]
    assert metric["column"] == "outstanding_balance"
    assert dimension["column"] == "customer_name"
    assert metric["column"] != dimension["column"]


def test_generic_value_metric_maps_to_price_like_columns(tmp_path):
    schema = {
        "po_header": table(
            column("po_id", "int", primary_key=True),
            column("po_number"),
            column("vendor_name"),
            column("final_price", "decimal"),
            row_count=1500,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "List the top 10 purchase orders by total value.",
        schema,
        name="generic_value",
    )

    assert result.constraint_coverage["metric:amount value"]["mapped"] is True
    assert result.constraint_coverage["metric:amount value"]["column"] == "final_price"
    assert "final_price" in result.relevant_columns["po_header"]


def test_location_grouping_maps_to_state_column(tmp_path):
    schema = {
        "projects": table(
            column("project_id", "int", primary_key=True),
            column("project_name"),
            column("state"),
            row_count=50,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "How many projects are there by state?",
        schema,
        name="location_grouping",
    )

    assert result.constraint_coverage["grouping:location"]["mapped"] is True
    assert result.constraint_coverage["grouping:location"]["column"] == "state"
    assert "state" in result.relevant_columns["projects"]


def test_selected_metric_table_keeps_supporting_identifier_reference_and_date_columns(tmp_path):
    schema = {
        "purchase_orders": table(
            column("purchase_order_id", "int", primary_key=True),
            column("po_no"),
            column("vendor_name"),
            column("order_date", "date"),
            column("total_value", "decimal"),
            row_count=1500,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "List the top 10 purchase orders by total value.",
        schema,
        name="supporting_columns",
    )

    columns = result.relevant_columns["purchase_orders"]
    assert "total_value" in columns
    assert "purchase_order_id" in columns
    assert "po_no" in columns
    assert "order_date" in columns


def test_supporting_columns_are_only_added_to_selected_tables(tmp_path):
    schema = {
        "purchase_orders": table(
            column("purchase_order_id", "int", primary_key=True),
            column("po_no"),
            column("total_value", "decimal"),
            row_count=100,
        ),
        "audit_log": table(
            column("audit_id", "int", primary_key=True),
            column("audit_ref"),
            column("created_at", "datetime"),
            row_count=900000,
        ),
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Top purchase orders by total value",
        schema,
        name="supporting_columns_selected_only",
    )

    assert "purchase_orders" in result.relevant_columns
    assert "audit_log" not in result.relevant_columns


def test_specific_date_term_selects_due_date_not_other_dates(tmp_path):
    schema = {
        "invoices": table(
            column("invoice_id", "int", primary_key=True),
            column("invoice_date", "date"),
            column("due_date", "date", comment="Date payment is due"),
            column("created_at", "datetime"),
            column("invoice_amount", "decimal"),
            row_count=1000,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Invoice amount due in 2025",
        schema,
        name="date",
    )

    date_mappings = [value for key, value in result.constraint_coverage.items() if key.startswith("date:")]
    assert date_mappings
    assert date_mappings[0]["column"] == "due_date"
    assert "invoice_date" not in [
        column.column
        for column in result.ranked_columns["invoices"]
        if "date" in column.roles
    ]


def test_specific_status_term_selects_matching_status_column(tmp_path):
    schema = {
        "invoices": table(
            column("invoice_id", "int", primary_key=True),
            column("payment_status"),
            column("approval_status"),
            column("invoice_amount", "decimal"),
            row_count=1000,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Show invoices with pending payment status",
        schema,
        name="status",
    )

    status_mappings = [value for key, value in result.constraint_coverage.items() if key.startswith("status:")]
    assert status_mappings
    assert status_mappings[0]["column"] == "payment_status"


def test_populated_table_wins_over_empty_similarly_named_table(tmp_path):
    schema = {
        "orders": table(column("order_id", "int", primary_key=True), column("order_total", "decimal"), row_count=0),
        "order_register": table(
            column("order_id", "int", primary_key=True),
            column("order_name"),
            column("order_total", "decimal"),
            row_count=9000,
        ),
    }
    _retriever, _plan, result, _prompt = retrieve(tmp_path, "Highest order total", schema, name="population")

    names = [item.table for item in result.ranked_tables]
    assert names.index("order_register") < names.index("orders")
    assert "empty table penalty" in next(item.reasons for item in result.ranked_tables if item.table == "orders")


def test_bridge_table_added_only_for_required_relationship_path(tmp_path):
    schema = {
        "customers": table(column("customer_id", "int", primary_key=True), column("customer_name"), row_count=100),
        "orders": table(
            column("order_id", "int", primary_key=True),
            column("customer_id", "int", indexed=True),
            foreign_keys=[{"column": "customer_id", "table": "customers", "references": "customer_id"}],
            row_count=1000,
        ),
        "order_lines": table(
            column("line_id", "int", primary_key=True),
            column("order_id", "int", indexed=True),
            column("quantity", "decimal"),
            foreign_keys=[{"column": "order_id", "table": "orders", "references": "order_id"}],
            row_count=5000,
        ),
        "audit_events": table(column("event_id", "int", primary_key=True), column("event_date", "date"), row_count=50000),
    }
    _retriever, _plan, result, _prompt = retrieve(tmp_path, "Total quantity by customer", schema, name="bridge")

    assert {"customers", "order_lines"}.issubset(result.selected_tables)
    assert result.bridge_tables == ["orders"]
    assert "audit_events" not in result.selected_tables
    assert result.relationship_paths[0].tables in (
        ["customers", "orders", "order_lines"],
        ["order_lines", "orders", "customers"],
    )


def test_single_table_question_does_not_expand_relationship_graph(tmp_path):
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Average machine downtime hours",
        manufacturing_schema(),
        name="no_bridge",
    )

    assert result.selected_tables == ["machine_downtime_events"]
    assert result.bridge_tables == []
    assert result.relationship_paths == []


def test_minimal_fk_schema_uses_explainable_inferred_relationship(tmp_path):
    _retriever, _plan, result, prompt = retrieve(
        tmp_path,
        "Total hours by team",
        minimal_fk_schema(),
        name="inferred_relationship",
    )

    assert {"work_log", "teams"}.issubset(result.selected_tables)
    assert result.bridge_tables == []
    assert result.relationship_paths
    assert result.relationship_paths[0].inferred_edge_count == 1
    assert result.relationship_paths[0].warnings
    assert "work_log.team_id" in prompt or "teams.team_id" in prompt


def test_ambiguous_status_columns_are_unresolved_instead_of_guessed(tmp_path):
    schema = {
        "invoices": table(
            column("invoice_id", "int", primary_key=True),
            column("payment_status"),
            column("approval_status"),
            column("invoice_amount", "decimal"),
            row_count=1000,
        )
    }
    _retriever, _plan, result, _prompt = retrieve(
        tmp_path,
        "Show pending invoices",
        schema,
        name="ambiguous_status",
    )

    assert any(key.startswith("status:") for key in result.unresolved_concepts)
    assert any("Ambiguous status mapping" in warning for warning in result.warnings)
    assert result.fallback_required is True


def test_missing_required_metric_is_unresolved_instead_of_substituted(tmp_path):
    _retriever, _plan, result, prompt = retrieve(
        tmp_path,
        "Profit by customer",
        sales_schema(),
        name="missing_metric",
    )

    assert any(item.startswith("metric:profit loss") for item in result.unresolved_concepts)
    assert result.fallback_required is True
    assert "do not substitute or guess" in prompt


def test_compact_context_is_deterministic_and_excludes_unselected_tables(tmp_path):
    schema = {**finance_schema(), **manufacturing_schema(), **inventory_schema()}
    first = retrieve(tmp_path, "Outstanding balance by customer in 2025", schema, name="deterministic")
    second = retrieve(tmp_path, "Outstanding balance by customer in 2025", schema, name="deterministic")
    first_result, first_prompt = first[2], first[3]
    second_result, second_prompt = second[2], second[3]

    assert first_prompt == second_prompt
    assert first_result.relevant_columns == second_result.relevant_columns
    assert "machine_downtime_events" not in first_prompt
    assert len(first_prompt) < 6000
    assert first_result.context_character_count == len(first_prompt)


def test_large_schema_retrieval_stays_bounded_and_never_expands_full_schema(tmp_path):
    schema = large_schema(1000)
    started = time.perf_counter()
    _retriever, _plan, result, prompt = retrieve(
        tmp_path,
        "Top 5 domain742 events by metric amount in 2025",
        schema,
        name="large",
        candidate_limit=10,
    )
    elapsed = time.perf_counter() - started

    assert result.ranked_tables[0].table == "domain742_events"
    assert len(result.selected_tables) <= 3
    assert len(result.relevant_columns["domain742_events"]) < len(schema["domain742_events"]["columns"])
    assert "domain12_events" not in prompt
    assert len(prompt) < 5000
    assert elapsed < 8


def test_table_ranking_weights_are_configurable_and_normalized(tmp_path):
    retriever = SchemaRetriever(
        profile_dir=tmp_path,
        weights={"semantic_similarity": 5, "historical_usage": 0},
    )

    assert sum(retriever.weights.values()) == pytest.approx(1.0)
    assert retriever.weights["semantic_similarity"] > retriever.weights["schema_metadata"]
    assert retriever.weights["historical_usage"] == 0
