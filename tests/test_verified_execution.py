from types import SimpleNamespace

import pandas as pd

import chat_pipeline as chat_pipeline_module
from chat_pipeline import ChatPipeline
from query_plan import QueryPlan
from verified_execution import (
    VERIFIED_EXECUTION_VERSION,
    build_verified_execution_receipt,
    verified_fact_payload,
)


def _plan():
    relationship = {
        "left_table": "client_bill_raising",
        "left_column": "project_id",
        "right_table": "project_mst",
        "right_column": "project_id",
        "relationship_type": "many_to_one",
        "cardinality": "many bills to one project",
    }
    return QueryPlan(
        original_question="Show billing by project.",
        operation="aggregation",
        intent_category="data_query",
        metrics=["billing"],
        aggregation=["sum"],
        dimensions=["project"],
        grouping=["project"],
        selected_tables=["client_bill_raising", "project_mst"],
        selected_columns={
            "client_bill_raising": ["project_id", "total_amount"],
            "project_mst": ["project_id", "project_name"],
        },
        generation_tables=["client_bill_raising", "project_mst"],
        generation_columns={
            "client_bill_raising": ["project_id", "total_amount"],
            "project_mst": ["project_id", "project_name"],
        },
        catalog_authoritative=True,
        catalog_version="verified-erp:test",
        erp_schema_version="erp:test",
        approved_relationships=[relationship],
    )


def test_verified_execution_receipt_is_deterministic_and_fact_scoped():
    result = {
        "rows": 2,
        "columns": ["project_name", "total_billing"],
        "records": [
            {"project_name": "Alpha", "total_billing": 1250},
            {"project_name": "Beta", "total_billing": 900},
        ],
    }
    kwargs = {
        "sql": (
            "SELECT p.project_name, SUM(c.total_amount) total_billing "
            "FROM client_bill_raising c JOIN project_mst p "
            "ON c.project_id=p.project_id GROUP BY p.project_name"
        ),
        "result": result,
        "query_plan": _plan(),
        "validation": {"valid": True},
        "database_fingerprint": "db-fingerprint",
        "schema_fingerprint": "schema-fingerprint",
    }

    first = build_verified_execution_receipt(**kwargs)
    second = build_verified_execution_receipt(**kwargs)

    assert first.to_dict() == second.to_dict()
    assert first.version == VERIFIED_EXECUTION_VERSION
    assert first.validation_passed is True
    assert first.execution_succeeded is True
    assert first.row_count == 2
    assert first.selected_tables == [
        "client_bill_raising",
        "project_mst",
    ]
    assert "password" not in str(first.to_dict()).lower()
    assert verified_fact_payload(result) == {
        "row_count": 2,
        "columns": ["project_name", "total_billing"],
        "rows": result["records"],
    }


def test_verified_answer_prompt_contains_facts_but_not_conversation(monkeypatch):
    captured = {}

    def generate(prompt, call_type):
        captured["prompt"] = prompt
        captured["call_type"] = call_type
        return "Alpha has the highest verified billing."

    monkeypatch.setattr(chat_pipeline_module.llm, "_generate", generate)
    monkeypatch.setattr(
        chat_pipeline_module.llm,
        "_conversation_history",
        lambda: (_ for _ in ()).throw(
            AssertionError("verified answer must not read conversation history")
        ),
    )

    answer = chat_pipeline_module.llm.generate_verified_answer(
        "Which project has the highest billing?",
        {
            "row_count": 1,
            "columns": ["project_name", "total_billing"],
            "rows": [
                {"project_name": "Alpha", "total_billing": 1250}
            ],
        },
        {
            "authoritative": True,
            "validation_passed": True,
            "execution_succeeded": True,
        },
    )

    assert answer == "Alpha has the highest verified billing."
    assert captured["call_type"] == "generate_verified_answer"
    assert '"total_billing": 1250' in captured["prompt"]
    assert "Recent conversation" not in captured["prompt"]
    assert "Never add a number" in captured["prompt"]


def test_verified_cross_module_answer_is_short_and_human():
    pipeline = ChatPipeline()
    dataframe = pd.DataFrame([
        {
            "project_name": "RDSS-VIZAG",
            "gross_bill_value": 124000000,
            "purchase_value": 187000000,
            "issued_value": 42000000,
        },
        {
            "project_name": "RDSS-UGCABLE",
            "gross_bill_value": 81000000,
            "purchase_value": 65000000,
            "issued_value": 29000000,
        },
    ])
    plan = QueryPlan(
        original_question="Compare billing and PO value by project.",
        operation="comparison",
        intent_category="data_query",
        metrics=["billing", "purchase_value", "material_issue_value"],
        aggregation=["sum"],
        dimensions=["project"],
        generation_tables=["project_mst", "client_bill_raising", "po_form", "po_items"],
        catalog_authoritative=True,
    )

    answer = pipeline._verified_cross_module_answer(
        "Compare billing and PO value by project.",
        dataframe,
        plan,
    )

    assert answer.startswith("RDSS-VIZAG has the higher")
    assert "| Project Name | Gross Bill Value | Purchase Value | Issued Value |" in answer
    assert "For RDSS-VIZAG, the recorded PO value is INR 63,000,000.00 higher" in answer
    assert "For RDSS-UGCABLE, the recorded billing value is INR 16,000,000.00 higher" in answer
    assert "Limitations" not in answer
    assert "Keep in mind" not in answer


def test_authoritative_runtime_skips_heuristic_sql_and_keeps_zero_result(
    monkeypatch,
):
    pipeline = ChatPipeline()
    plan = _plan()
    sql = """
SELECT p.project_name, SUM(c.total_amount) AS total_billing
FROM client_bill_raising c
JOIN project_mst p ON c.project_id = p.project_id
GROUP BY p.project_name
ORDER BY total_billing DESC
LIMIT 10
""".strip()
    calls = {
        "generate": 0,
        "verified_answer": 0,
        "rewrite": 0,
        "tool": 0,
    }
    binding = {
        "authoritative": True,
        "clarification_required": False,
        "required_tables": ["client_bill_raising", "project_mst"],
        "required_columns": plan.generation_columns,
        "approved_relationships": plan.approved_relationships,
        "bound_metrics": [],
        "bound_dimensions": [],
        "unresolved_concepts": [],
    }

    def forbidden_discovery(*args, **kwargs):
        raise AssertionError(
            "authoritative runtime must not call generic discovery"
        )

    class Binding:
        def to_dict(self):
            return binding

    monkeypatch.setattr(
        chat_pipeline_module.erp_semantic_binder,
        "bind",
        lambda **kwargs: Binding(),
    )
    monkeypatch.setattr(
        chat_pipeline_module.question_classifier,
        "classify",
        lambda question: {
            "intent": "data_query",
            "normalized_question": question.lower(),
        },
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "business_catalog",
        lambda database: {"catalog_authority": "verified_erp_primary"},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "analyze_question",
        forbidden_discovery,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "resolve_verified_entities",
        lambda *args, **kwargs: {
            "entities": [],
            "matches": [],
            "unresolved": [],
            "clarification_required": False,
        },
    )
    monkeypatch.setattr(
        chat_pipeline_module.response_cache,
        "identity_for",
        lambda *args, **kwargs: {"key": "test", "metadata": {}},
    )
    monkeypatch.setattr(
        chat_pipeline_module.response_cache,
        "get_by_identity",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        chat_pipeline_module.response_cache,
        "set_by_identity",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "database_fingerprint",
        lambda *args, **kwargs: "db-fingerprint",
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "schema_fingerprint",
        lambda database: "schema-fingerprint",
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "source_identity",
        lambda database: f"mysql:{database}",
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "relevant_tables",
        forbidden_discovery,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "database_memory",
        forbidden_discovery,
    )
    monkeypatch.setattr(
        chat_pipeline_module.database_memory,
        "prompt_for_question",
        forbidden_discovery,
    )
    monkeypatch.setattr(
        chat_pipeline_module.entity_resolver,
        "format_multi_prompt",
        lambda resolution: "ENTITIES",
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "retrieve_schema_context",
        forbidden_discovery,
    )
    monkeypatch.setattr(
        chat_pipeline_module.query_plan_guard,
        "build",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        chat_pipeline_module.query_plan_guard,
        "validate_plan",
        lambda query_plan: {"valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "inspect_schema",
        lambda database: {},
    )
    monkeypatch.setattr(
        chat_pipeline_module.erp_sql_context_builder,
        "build",
        lambda **kwargs: SimpleNamespace(
            prompt="AUTHORITATIVE ERP SQL CONTEXT",
            selected_tables=plan.generation_tables,
            selected_columns=plan.generation_columns,
            to_dict=lambda: {
                "prompt": "AUTHORITATIVE ERP SQL CONTEXT"
            },
        ),
    )
    monkeypatch.setattr(
        chat_pipeline_module.verified_sql_compiler,
        "compile",
        lambda query_plan: sql,
    )
    monkeypatch.setattr(
        pipeline,
        "_mysql_validate_with_plan",
        lambda *args, **kwargs: {
            "valid": True,
            "errors": [],
            "warnings": [],
            "analysis": {},
        },
    )

    def tool(*args, **kwargs):
        calls["tool"] += 1
        return "SELECT 1"

    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "count_table_sql",
        tool,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "catalog_sql",
        tool,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "business_overview_sql",
        tool,
    )

    def generate(question, prompt):
        calls["generate"] += 1
        assert prompt == "AUTHORITATIVE ERP SQL CONTEXT"
        return sql

    monkeypatch.setattr(
        chat_pipeline_module.llm,
        "generate_mysql_sql",
        generate,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "execute_read_query",
        lambda sql, database: pd.DataFrame(
            columns=["project_name", "total_billing"]
        ),
    )

    def rewrite(**kwargs):
        calls["rewrite"] += 1
        raise AssertionError("verified zero rows must not trigger SQL rewrite")

    monkeypatch.setattr(
        chat_pipeline_module.llm,
        "rewrite_failed_mysql_sql",
        rewrite,
    )
    monkeypatch.setattr(
        chat_pipeline_module.llm,
        "generate_answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generic answer generator must not be used")
        ),
    )

    def verified_answer(question, facts, receipt):
        calls["verified_answer"] += 1
        assert facts["row_count"] == 0
        assert receipt["validation_passed"] is True
        return "No matching billing records were returned."

    monkeypatch.setattr(
        chat_pipeline_module.llm,
        "generate_verified_answer",
        verified_answer,
    )
    monkeypatch.setattr(
        chat_pipeline_module.schema_profile_history,
        "record_success",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "referenced_tables",
        lambda sql: ["client_bill_raising", "project_mst"],
    )

    response = pipeline.ask_mysql(
        "Show billing by project.",
        "erp",
        "mysql:erp",
    )

    assert response["success"] is True
    assert response["answer"] == "No matching billing records were returned."
    assert response["result"]["rows"] == 0
    assert response["verification"]["authoritative"] is True
    assert response["confidence"]["confidence_level"] == "medium"
    assert calls == {
        "generate": 0,
        "verified_answer": 1,
        "rewrite": 0,
        "tool": 0,
    }
