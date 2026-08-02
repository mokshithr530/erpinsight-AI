import chat_pipeline as chat_pipeline_module
from chat_pipeline import ChatPipeline
from erp_sql_context import ERPSQLContextBuilder
from query_plan import QueryPlan


def _schema():
    return {
        "project_mst": {
            "comment": "Verified project master",
            "row_count": 19,
            "columns": [
                {
                    "column_name": "project_id",
                    "data_type": "int",
                    "column_type": "int unsigned",
                    "primary_key": True,
                    "not_null": True,
                },
                {
                    "column_name": "project_name",
                    "data_type": "varchar",
                    "column_type": "varchar(255)",
                    "not_null": True,
                    "comment": "Manager-facing project name",
                },
                {
                    "column_name": "project_value",
                    "data_type": "decimal",
                    "column_type": "decimal(18,2)",
                },
                {
                    "column_name": "internal_note",
                    "data_type": "text",
                    "column_type": "text",
                },
            ],
            "foreign_keys": [],
            "indexes": [
                {
                    "name": "PRIMARY",
                    "columns": ["project_id"],
                    "unique": True,
                }
            ],
        },
        "client_bill_raising": {
            "columns": [
                {
                    "column_name": "billing_id",
                    "data_type": "int",
                    "column_type": "int",
                    "primary_key": True,
                },
                {
                    "column_name": "project_id",
                    "data_type": "int",
                    "column_type": "int unsigned",
                },
                {
                    "column_name": "total_amount",
                    "data_type": "decimal",
                    "column_type": "decimal(18,2)",
                },
                {
                    "column_name": "bill_date",
                    "data_type": "date",
                    "column_type": "date",
                },
                {
                    "column_name": "billing_status",
                    "data_type": "varchar",
                    "column_type": "varchar(20)",
                },
                {
                    "column_name": "technical_payload",
                    "data_type": "longtext",
                    "column_type": "longtext",
                },
            ],
            "foreign_keys": [
                {
                    "name": "fk_bill_project",
                    "column": "project_id",
                    "table": "project_mst",
                    "references": "project_id",
                }
            ],
            "indexes": [],
        },
        "audit_log": {
            "columns": [
                {
                    "column_name": "audit_id",
                    "data_type": "bigint",
                    "column_type": "bigint",
                    "primary_key": True,
                },
                {
                    "column_name": "payload",
                    "data_type": "longtext",
                    "column_type": "longtext",
                },
            ],
            "foreign_keys": [],
            "indexes": [],
        },
    }


def _binding():
    relationship = {
        "left_table": "client_bill_raising",
        "left_column": "project_id",
        "right_table": "project_mst",
        "right_column": "project_id",
        "relationship_type": "many_to_one",
        "cardinality": "many bills to one project",
        "verification_status": "verified",
        "ambiguity_notes": "Aggregate billing headers before joining details.",
    }
    return {
        "authoritative": True,
        "clarification_required": False,
        "catalog_version": "verified-erp:test",
        "erp_schema_version": "erp:test",
        "required_tables": ["client_bill_raising", "project_mst"],
        "required_columns": {
            "client_bill_raising": [
                "total_amount",
                "project_id",
                "bill_date",
                "billing_status",
            ],
            "project_mst": ["project_name"],
        },
        "bound_metrics": [
            {
                "metric": "billing",
                "table": "client_bill_raising",
                "column": "total_amount",
                "default_aggregation": "sum",
                "aggregation_grain": "billing_header",
            }
        ],
        "bound_dimensions": [
            {
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
            }
        ],
        "bound_date_fields": [
            {
                "semantic": "billing_date",
                "table": "client_bill_raising",
                "column": "bill_date",
            }
        ],
        "bound_status_fields": [
            {
                "table": "client_bill_raising",
                "column": "billing_status",
                "semantics": ["pending", "approved", "completed"],
                "requested_values": ["approved"],
            }
        ],
        "bound_formulas": [
            {
                "formula": "billing",
                "definition": "SUM(client_bill_raising.total_amount)",
                "preferred_table": "client_bill_raising",
                "preferred_column": "total_amount",
            }
        ],
        "bound_business_definitions": [
            {
                "concept_id": "gross_billing",
                "metric_definition": "Gross billing at billing-header grain.",
                "formula": "SUM(client_bill_raising.total_amount)",
                "aggregation": "sum",
                "data_grain": "one amount per billing header",
                "date_field": "client_bill_raising.bill_date",
                "date_storage_format": "%d-%m-%Y",
                "status_field": "client_bill_raising.billing_status",
                "valid_status_values": [0, 2, 3],
                "status_value_meanings_verified": False,
                "null_handling": "NULL values contribute zero.",
                "approved_joins": [
                    "client_bill_raising.project_id = project_mst.project_id"
                ],
                "duplicate_risk_rules": [
                    "Aggregate billing headers before joining details."
                ],
                "expected_output": {
                    "columns": ["project_name", "gross_billing"],
                    "grain": "one row per project",
                },
                "reference_sql": (
                    "SELECT p.project_name, SUM(b.total_amount) AS "
                    "gross_billing FROM client_bill_raising b JOIN "
                    "project_mst p ON b.project_id = p.project_id "
                    "GROUP BY p.project_name"
                ),
                "ambiguity_notes": "",
            }
        ],
        "approved_relationships": [relationship],
    }


def _plan():
    relationship = _binding()["approved_relationships"][0]
    return QueryPlan(
        original_question="Compare approved billing across projects in 2025.",
        operation="comparison",
        intent_category="data_query",
        requested_outputs=["project"],
        base_entity="project",
        metrics=["billing"],
        aggregation=["sum"],
        dimensions=["project"],
        date_constraints=[
            {
                "type": "year",
                "value": 2025,
                "start": "2025-01-01",
                "end": "2025-12-31",
            }
        ],
        status_constraints=[
            {
                "type": "status",
                "values": ["approved"],
            }
        ],
        grouping=["project"],
        ordering=[],
        joins=[relationship],
        aggregation_grain=["billing_header", "project"],
        selected_tables=[
            "client_bill_raising",
            "project_mst",
            "audit_log",
        ],
        selected_columns={
            "client_bill_raising": [
                "billing_id",
                "project_id",
                "total_amount",
                "bill_date",
                "billing_status",
                "technical_payload",
            ],
            "project_mst": [
                "project_id",
                "project_name",
                "internal_note",
            ],
            "audit_log": ["audit_id", "payload"],
        },
        catalog_authoritative=True,
        catalog_version="verified-erp:test",
        erp_schema_version="erp:test",
        approved_relationships=[relationship],
    )


def _catalog():
    return {
        "verified_semantic_catalog": {
            "duplicate_risk_warnings": [
                {
                    "relationship": (
                        "client_bill_raising -> client_bills_boq_wisedetails"
                    ),
                    "warning": (
                        "Billing header totals can be duplicated when joined "
                        "to BOQ details before aggregation."
                    ),
                    "preferred_table": "client_bill_raising",
                    "preferred_column": "billing_id",
                }
            ]
        }
    }


def test_context_contains_only_plan_required_schema():
    context = ERPSQLContextBuilder().build(
        schema=_schema(),
        query_plan=_plan(),
        semantic_binding=_binding(),
        catalog=_catalog(),
        schema_fingerprint="schema-test",
    )

    assert context.selected_tables == [
        "client_bill_raising",
        "project_mst",
    ]
    assert "CREATE TABLE `client_bill_raising`" in context.canonical_ddl
    assert "CREATE TABLE `project_mst`" in context.canonical_ddl
    assert "audit_log" not in context.prompt
    assert "technical_payload" not in context.prompt
    assert "internal_note" not in context.prompt


def test_context_retains_keys_types_comments_and_foreign_keys():
    context = ERPSQLContextBuilder().build(
        schema=_schema(),
        query_plan=_plan(),
        semantic_binding=_binding(),
        catalog=_catalog(),
        schema_fingerprint="schema-test",
    )

    assert "`project_id` int unsigned NOT NULL" in context.canonical_ddl
    assert "`project_name` varchar(255) NOT NULL" in context.canonical_ddl
    assert "Manager-facing project name" in context.canonical_ddl
    assert "PRIMARY KEY (`project_id`)" in context.canonical_ddl
    assert "CONSTRAINT `fk_bill_project` FOREIGN KEY (`project_id`)" in (
        context.canonical_ddl
    )


def test_context_keeps_verified_semantics_and_safety_rules():
    context = ERPSQLContextBuilder().build(
        schema=_schema(),
        query_plan=_plan(),
        semantic_binding=_binding(),
        catalog=_catalog(),
        schema_fingerprint="schema-test",
    )

    assert "AUTHORITATIVE ERP SQL CONTEXT" in context.prompt
    assert '"semantics": [' in context.prompt
    assert '"approved"' in context.prompt
    assert "SUM(client_bill_raising.total_amount)" in context.prompt
    assert "many bills to one project" in context.prompt
    assert "Billing header totals can be duplicated" in context.prompt
    assert '"concept_id": "gross_billing"' in context.prompt
    assert '"null_handling": "NULL values contribute zero."' in context.prompt
    assert '"reference_sql": "SELECT p.project_name' in context.prompt
    assert "Never invent a join" in context.prompt


def test_context_is_deterministic_and_cached():
    builder = ERPSQLContextBuilder()
    kwargs = {
        "schema": _schema(),
        "query_plan": _plan(),
        "semantic_binding": _binding(),
        "catalog": _catalog(),
        "schema_fingerprint": "schema-test",
    }

    first = builder.build(**kwargs)
    second = builder.build(**kwargs)

    assert first is second
    assert first.cache_key == second.cache_key
    assert first.prompt == second.prompt
    assert first.character_count == len(first.prompt)


def test_context_refuses_non_authoritative_or_ambiguous_binding():
    builder = ERPSQLContextBuilder()
    binding = _binding()
    binding["authoritative"] = False

    assert builder.build(
        schema=_schema(),
        query_plan=_plan(),
        semantic_binding=binding,
        catalog=_catalog(),
        schema_fingerprint="schema-test",
    ) is None

    binding["authoritative"] = True
    binding["clarification_required"] = True
    assert builder.build(
        schema=_schema(),
        query_plan=_plan(),
        semantic_binding=binding,
        catalog=_catalog(),
        schema_fingerprint="schema-test",
    ) is None


def test_chat_prompt_prefers_authoritative_context(monkeypatch):
    pipeline = ChatPipeline()
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "schema_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generic schema prompt must not be read")
        ),
    )

    prompt = pipeline._mysql_schema_prompt(
        database="erp",
        relevant_tables=["project_mst"],
        entity_resolver_prompt="ENTITY",
        question_memory_prompt="MEMORY",
        erp_sql_context={"prompt": "AUTHORITATIVE ERP SQL CONTEXT\nDDL"},
    )

    assert prompt == "AUTHORITATIVE ERP SQL CONTEXT\nDDL"
