import time

import chat_pipeline as chat_pipeline_module
import pandas as pd

from business_catalog import BusinessCatalog
from cache_identity import build_cache_identity
from cache_relevance import cache_relevance
from chat_pipeline import ChatPipeline
from database_memory import DatabaseMemory
from database_fingerprint import database_fingerprint, schema_fingerprint
from entity_resolver import entity_resolver
from mysql_adapter import MySQLAdapter
from question_classifier import question_classifier
from relationship_discovery import relationship_discovery
from response_cache import ResponseCache
from query_plan import query_plan_guard
from schema_retriever import SchemaRetriever
from semantic_parser import semantic_parser


class InMemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        item = self.values.get(key)
        if not item:
            return None
        value, expires_at = item
        if expires_at <= time.time():
            self.values.pop(key, None)
            return None
        return value

    def setex(self, key, ttl, value):
        self.values[key] = (value, time.time() + ttl)


def commerce_schema():
    return {
        "orders": {
            "columns": [
                {"name": "order_id", "type": "int", "primary_key": True},
                {"name": "customer_id", "type": "int", "indexed": True},
                {"name": "order_total", "type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "customers": {
            "columns": [
                {"name": "customer_id", "type": "int", "primary_key": True},
                {"name": "customer_name", "type": "text"},
            ],
            "foreign_keys": [],
        },
    }


def erp_schema():
    return {
        "invoice_header": {
            "columns": [
                {"name": "invoice_id", "type": "int", "primary_key": True},
                {"name": "project_id", "type": "int", "indexed": True},
                {"name": "invoice_total", "type": "decimal"},
            ],
            "foreign_keys": [{"column": "project_id", "table": "project_master", "references": "project_id"}],
        },
        "project_master": {
            "columns": [
                {"name": "project_id", "type": "int", "primary_key": True},
                {"name": "department_id", "type": "int", "indexed": True},
                {"name": "project_name", "type": "text"},
            ],
            "foreign_keys": [{"column": "department_id", "table": "department", "references": "department_id"}],
        },
        "department": {
            "columns": [
                {"name": "department_id", "type": "int", "primary_key": True},
                {"name": "department_name", "type": "text"},
            ],
            "foreign_keys": [],
        },
    }


def warehouse_schema():
    return {
        "project": {
            "columns": [
                {"column_name": "id", "data_type": "int", "primary_key": True},
                {"column_name": "name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "client_master": {
            "columns": [
                {"column_name": "client_id", "data_type": "int", "primary_key": True},
                {"column_name": "client_name", "data_type": "varchar"},
                {"column_name": "status", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "project_master": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
                {"column_name": "project_value", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "vendor_mst": {
            "columns": [
                {"column_name": "vendor_id", "data_type": "int", "primary_key": True},
                {"column_name": "vendor_name", "data_type": "varchar"},
                {"column_name": "vendor_active_status", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "service_vendors": {
            "columns": [
                {"column_name": "service_vendors_id", "data_type": "int", "primary_key": True},
                {"column_name": "servicevendor_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
    }


def test_schema_fingerprint_changes_when_columns_change():
    first = commerce_schema()
    second = commerce_schema()
    second["orders"]["columns"].append({"name": "updated_at", "type": "datetime"})

    assert schema_fingerprint(first) != schema_fingerprint(second)


def test_database_fingerprint_avoids_secrets_and_uses_safe_identity():
    fingerprint = database_fingerprint(
        source_type="mysql",
        dialect="mysql",
        database_name="erp",
        connection_id="mysql:erp:safe",
        host_identity="hosthash",
    )

    assert len(fingerprint) == 64
    assert "erp" not in fingerprint


def test_business_catalog_exposes_metrics_dimensions_and_ambiguity():
    catalog = BusinessCatalog().build(commerce_schema(), source_type="sqlite")

    assert catalog["catalog_version"]
    assert any(metric["metric"] == "revenue" for metric in catalog["metrics"])
    assert any(dimension["dimension"] == "client" for dimension in catalog["dimensions"])
    assert catalog["relationship_graph"]["edges"]


def test_relationship_discovery_scores_multi_hop_path():
    paths = relationship_discovery.find_paths(
        erp_schema(),
        ["invoice_header"],
        ["department"],
    )

    assert paths
    assert paths[0]["tables"] == ["invoice_header", "project_master", "department"]
    assert paths[0]["confidence_level"] in {"high", "medium"}


def test_question_classifier_detects_comparison_metric_and_dimension():
    classification = question_classifier.classify("Compare billing between clients")

    assert classification["intent"] == "comparison"
    assert "billing" in classification["metrics"]
    assert "client" in classification["dimensions"]


def test_cache_identity_changes_when_schema_changes():
    first = build_cache_identity(
        question="total revenue",
        database_fingerprint="db",
        schema_fingerprint="schema-a",
        dialect="sqlite",
        source_identity="sqlite:file",
        provider="openrouter",
        model="deepseek",
    )
    second = build_cache_identity(
        question="total revenue",
        database_fingerprint="db",
        schema_fingerprint="schema-b",
        dialect="sqlite",
        source_identity="sqlite:file",
        provider="openrouter",
        model="deepseek",
    )

    assert first["key"] != second["key"]


def test_cache_relevance_rejects_schema_mismatch():
    identity = build_cache_identity(
        question="total revenue",
        database_fingerprint="db",
        schema_fingerprint="schema-a",
        dialect="sqlite",
        source_identity="sqlite:file",
        provider="openrouter",
        model="deepseek",
    )
    envelope = {
        "metadata": identity["metadata"],
        "response": {"success": True, "answer": "old"},
        "created_at": time.time(),
    }
    expected = dict(identity["metadata"])
    expected["schema_fingerprint"] = "schema-b"

    result = cache_relevance.validate(envelope, expected)

    assert result["valid"] is False
    assert result["reason"] == "schema_fingerprint_mismatch"


def test_response_cache_returns_only_relevant_enveloped_answer():
    cache = ResponseCache()
    cache.client = InMemoryRedis()
    identity = build_cache_identity(
        question="total revenue",
        database_fingerprint="db",
        schema_fingerprint="schema-a",
        dialect="sqlite",
        source_identity="sqlite:file",
        provider="openrouter",
        model="deepseek",
    )
    response = {"success": True, "answer": "fresh", "confidence": {"confidence_level": "high"}}

    cache.set_by_identity(
        "total revenue",
        "legacy-db",
        response,
        provider="openrouter",
        model="deepseek",
        cache_identity=identity,
    )

    assert cache.get_by_identity(
        "total revenue",
        "legacy-db",
        provider="openrouter",
        model="deepseek",
        cache_identity=identity,
    ) == response

    stale = build_cache_identity(
        question="total revenue",
        database_fingerprint="db",
        schema_fingerprint="schema-b",
        dialect="sqlite",
        source_identity="sqlite:file",
        provider="openrouter",
        model="deepseek",
    )
    assert cache.get_by_identity(
        "total revenue",
        "legacy-db",
        provider="openrouter",
        model="deepseek",
        cache_identity=stale,
    ) is None


def test_database_memory_persists_and_selects_relevant_tables(tmp_path):
    memory_store = DatabaseMemory(tmp_path / "memory")
    fingerprint = schema_fingerprint(erp_schema())

    memory = memory_store.get_or_build(
        source_type="mysql",
        database_name="erp",
        schema=erp_schema(),
        row_counts={"invoice_header": 100, "project_master": 10, "department": 4},
        fingerprint=fingerprint,
    )
    loaded = memory_store.get_or_build(
        source_type="mysql",
        database_name="erp",
        schema=erp_schema(),
        row_counts={},
        fingerprint=fingerprint,
    )

    assert loaded["schema_fingerprint"] == fingerprint
    assert (tmp_path / "memory" / f"{fingerprint}.json").exists()
    relevant = memory_store.relevant_tables("compare billing by project", memory)
    assert "invoice_header" in relevant
    assert "project_master" in relevant


def test_database_memory_prompt_uses_cached_schema_map(tmp_path):
    memory_store = DatabaseMemory(tmp_path / "memory")
    fingerprint = schema_fingerprint(erp_schema())
    memory = memory_store.get_or_build(
        source_type="mysql",
        database_name="erp",
        schema=erp_schema(),
        row_counts={"invoice_header": 100},
        fingerprint=fingerprint,
    )

    prompt = memory_store.prompt_for_question("how many projects are there", memory)

    assert "Cached database memory" in prompt
    assert "project_master" in prompt
    assert "project_name" in prompt


def test_mysql_generic_count_routes_to_best_entity_table():
    adapter = MySQLAdapter()
    adapter.inspect_schema = lambda database: warehouse_schema()
    adapter._largest_tables = lambda database: [
        {"table": "project_master", "approx_rows": 15},
        {"table": "project", "approx_rows": 0},
    ]

    sql = adapter.count_table_sql("how many prjects are listed just the number", "demo")

    assert "project_master" in sql
    assert "project_count" in sql
    assert "project_name" in sql


def test_mysql_generic_count_works_for_non_project_entities():
    adapter = MySQLAdapter()
    adapter.inspect_schema = lambda database: warehouse_schema()
    adapter._largest_tables = lambda database: [
        {"table": "client_master", "approx_rows": 4},
        {"table": "project_master", "approx_rows": 15},
    ]

    sql = adapter.count_table_sql("how many clients are there", "demo")

    assert "client_master" in sql
    assert "client_count" in sql
    assert "client_name" in sql


def test_mysql_generic_list_uses_requested_entity_and_limit():
    adapter = MySQLAdapter()
    sql = adapter._generic_entity_list_sql("list 5 clients", warehouse_schema())

    assert "client_master" in sql
    assert "client_name" in sql
    assert "LIMIT 5" in sql


def test_mysql_generic_list_understands_who_all_vendor_question():
    adapter = MySQLAdapter()
    sql = adapter._generic_entity_list_sql(
        "who all are the vendors",
        warehouse_schema(),
        {"vendor_mst": 256, "service_vendors": 0},
    )

    assert "vendor_mst" in sql
    assert "vendor_name" in sql
    assert "service_vendors" not in sql


def test_mysql_generic_rank_uses_entity_value_and_limit():
    adapter = MySQLAdapter()
    sql = adapter._generic_entity_rank_sql(
        "tell me top 3 projects based on the project value",
        warehouse_schema(),
        {"project_master": 15, "project": 0},
    )

    assert "project_master" in sql
    assert "project_value" in sql
    assert "metric_value" in sql
    assert "CAST(p.`project_value` AS DECIMAL" in sql
    assert "ORDER BY metric_value DESC" in sql
    assert "LIMIT 3" in sql


def test_mysql_empty_count_result_is_not_trusted_for_business_answer():
    pipeline = ChatPipeline()

    assert pipeline._looks_like_empty_mysql_answer(
        "how many projects are there",
        pd.DataFrame([{"total_projects": 0}]),
    )
    assert not pipeline._looks_like_empty_mysql_answer(
        "how many projects are there",
        pd.DataFrame([{"project_count": 15}]),
    )


def test_mysql_schema_prompt_can_include_full_compact_schema_with_rows():
    adapter = MySQLAdapter()
    adapter.inspect_schema = lambda database: warehouse_schema()
    adapter._largest_tables = lambda database: [
        {"table": "project_master", "approx_rows": 15},
        {"table": "project", "approx_rows": 0},
        {"table": "client_master", "approx_rows": 4},
    ]

    prompt = adapter.schema_prompt("demo", max_columns=10, include_row_counts=True)

    assert "project (~0 rows):" in prompt
    assert "project_master (~15 rows):" in prompt
    assert "client_master (~4 rows):" in prompt


def test_mysql_schema_prompt_can_include_all_columns_for_broad_llm_context():
    adapter = MySQLAdapter()
    adapter.inspect_schema = lambda database: warehouse_schema()
    adapter._largest_tables = lambda database: [{"table": "vendor_mst", "approx_rows": 256}]

    prompt = adapter.schema_prompt("demo", allowed_tables=["vendor_mst"], max_columns=0, include_row_counts=True)

    assert "vendor_mst (~256 rows):" in prompt
    assert "vendor_id int" in prompt
    assert "vendor_name varchar" in prompt
    assert "vendor_active_status varchar" in prompt


def test_focused_mysql_schema_prompt_includes_index_and_detailed_candidates(monkeypatch):
    pipeline = ChatPipeline()
    calls = []

    def fake_schema_prompt(database, allowed_tables=None, max_columns=18, include_row_counts=False):
        calls.append({
            "allowed_tables": allowed_tables,
            "max_columns": max_columns,
            "include_row_counts": include_row_counts,
        })
        return "SCHEMA\n"

    monkeypatch.setattr(chat_pipeline_module, "MYSQL_LLM_SCHEMA_MODE", "focused")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_prompt", fake_schema_prompt)

    prompt = pipeline._mysql_schema_prompt(
        database="demo",
        relevant_tables=["project_mst", "po_form"],
        entity_resolver_prompt="ENTITY PLAN",
        question_memory_prompt="MEMORY",
    )

    assert "Compact database-wide table index" in prompt
    assert "Detailed schema for the strongest candidate" in prompt
    assert calls[0]["allowed_tables"] is None
    assert calls[0]["max_columns"] == 6
    assert calls[1]["allowed_tables"] == ["project_mst", "po_form"]
    assert calls[1]["max_columns"] == 24


def test_schema_retriever_prefers_populated_business_master_over_empty_name_match(tmp_path):
    retriever = SchemaRetriever(profile_dir=tmp_path)
    schema = warehouse_schema()
    row_counts = {"project": 0, "project_master": 15, "client_master": 4}
    catalog = BusinessCatalog().build(
        schema,
        source_type="mysql",
        row_counts=row_counts,
        fingerprint="schema-retriever-test",
    )
    plan = semantic_parser.parse("how many projects are there").to_dict()

    result = retriever.retrieve(
        question="how many projects are there",
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="schema-retriever-test",
        semantic_plan=plan,
        candidate_limit=5,
    )

    ranked = [table.table for table in result.ranked_tables]
    assert ranked.index("project_master") < ranked.index("project")
    project_reasons = next(table.reasons for table in result.ranked_tables if table.table == "project")
    assert "empty table penalty" in project_reasons


def test_schema_retriever_compact_prompt_excludes_unrelated_full_schema(tmp_path):
    retriever = SchemaRetriever(profile_dir=tmp_path)
    schema = {
        **warehouse_schema(),
        "audit_log": {
            "columns": [
                {"column_name": "audit_log_id", "data_type": "int", "primary_key": True},
                {"column_name": "payload_blob", "data_type": "longtext"},
            ],
            "foreign_keys": [],
        },
        "notification_history": {
            "columns": [
                {"column_name": "notification_history_id", "data_type": "int", "primary_key": True},
                {"column_name": "message_body", "data_type": "text"},
            ],
            "foreign_keys": [],
        },
    }
    row_counts = {"project_master": 15, "project": 0, "vendor_mst": 25, "audit_log": 100000}
    catalog = BusinessCatalog().build(
        schema,
        source_type="mysql",
        row_counts=row_counts,
        fingerprint="schema-compact-test",
    )
    plan = semantic_parser.parse("which vendor has the highest purchase value").to_dict()

    result = retriever.retrieve(
        question="which vendor has the highest purchase value",
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="schema-compact-test",
        semantic_plan=plan,
        candidate_limit=3,
    )
    profiles = retriever.profiles_for(
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="schema-compact-test",
    )
    prompt = retriever.compact_prompt(result, profiles)

    assert "vendor_mst" in prompt
    assert "audit_log" not in prompt
    assert "notification_history" not in prompt
    assert len(result.ranked_tables) <= 3


def test_schema_retriever_forces_resolved_entity_table_into_context(tmp_path):
    retriever = SchemaRetriever(profile_dir=tmp_path)
    schema = warehouse_schema()
    row_counts = {"project_master": 15, "vendor_mst": 25}
    catalog = BusinessCatalog().build(
        schema,
        source_type="mysql",
        row_counts=row_counts,
        fingerprint="schema-force-resolved-test",
    )
    plan = semantic_parser.parse("which vendor has the highest purchase value for KOLLAPUR").to_dict()

    result = retriever.retrieve(
        question="which vendor has the highest purchase value for KOLLAPUR",
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="schema-force-resolved-test",
        semantic_plan=plan,
        entity_resolution={
            "entities": [{
                "table": "project_master",
                "column": "project_name",
                "value": "KOLLAPUR",
                "confidence": 99,
            }]
        },
        candidate_limit=1,
    )

    ranked = [table.table for table in result.ranked_tables]
    assert "project_master" in ranked


def test_mysql_focused_schema_prompt_uses_retrieval_prompt_without_full_schema(monkeypatch):
    pipeline = ChatPipeline()
    calls = []

    def fake_schema_prompt(*args, **kwargs):
        calls.append((args, kwargs))
        return "FULL DATABASE SHOULD NOT BE USED"

    monkeypatch.setattr(chat_pipeline_module, "MYSQL_LLM_SCHEMA_MODE", "focused")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_prompt", fake_schema_prompt)
    prompt = pipeline._mysql_schema_prompt(
        database="demo",
        relevant_tables=["project_master"],
        entity_resolver_prompt="ENTITY PLAN",
        question_memory_prompt="MEMORY",
        semantic_plan={"operation": "ranking"},
        schema_retrieval_prompt="Retrieved compact schema context:\n- project_master: project_id, project_name",
        schema_retrieval_result={"ranked_tables": [{"table": "project_master"}]},
    )

    assert "Retrieved compact schema context" in prompt
    assert "ranked_tables" not in prompt
    assert "FULL DATABASE SHOULD NOT BE USED" not in prompt
    assert calls == []


def test_entity_resolver_does_not_extract_metric_phrase_as_entity():
    extracted = entity_resolver.extract("which project has the highest purchase value")

    assert extracted == []


def test_mysql_entity_scoped_empty_result_gets_rewritten(monkeypatch):
    pipeline = ChatPipeline()
    empty = pd.DataFrame()
    answered = pd.DataFrame([{"project_name": "RDSS", "purchase_value": 100}])
    calls = {"execute": 0, "rewrite": 0}

    monkeypatch.setattr(
        chat_pipeline_module.question_classifier,
        "classify",
        lambda question: {"intent": "data_query", "normalized_question": question.lower()},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "analyze_question",
        lambda question, database: {"candidates": [], "metrics": [], "dimensions": []},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "resolve_verified_entities",
        lambda question, database, **kwargs: {
            "clarification_required": False,
            "matches": [{
                "entity": "RDSS",
                "entity_type": "project",
                "table": "project_mst",
                "column": "project_name",
                "primary_key": 2,
                "confidence": 99,
            }],
            "entities": [{
                "mention": "RDSS",
                "resolved_type": "project",
                "table": "project_mst",
                "column": "project_name",
                "primary_key": 2,
                "value": "RDSS",
                "confidence": 99,
            }],
        },
    )
    monkeypatch.setattr(chat_pipeline_module.response_cache, "get_by_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.response_cache, "set_by_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "database_fingerprint", lambda *args, **kwargs: "dbfp")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_fingerprint", lambda database: "schemafp")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "source_identity", lambda database: f"mysql:{database}")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "relevant_tables", lambda *args, **kwargs: ["project_mst"])
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "database_memory", lambda database: {})
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "retrieve_schema_context",
        lambda *args, **kwargs: {
            "prompt": "Retrieved compact schema context",
            "result": {"ranked_tables": [{"table": "project_mst"}]},
            "tables": ["project_mst"],
            "columns": {"project_mst": ["project_id", "project_name"]},
        },
    )
    monkeypatch.setattr(chat_pipeline_module.database_memory, "prompt_for_question", lambda *args, **kwargs: "MEMORY")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_prompt", lambda *args, **kwargs: "SCHEMA")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "count_table_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "catalog_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "business_overview_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "validate_sql", lambda *args, **kwargs: {"valid": True, "errors": []})
    monkeypatch.setattr(chat_pipeline_module.llm, "generate_mysql_sql", lambda question, schema_prompt: "SELECT * FROM project_mst WHERE project_name = 'RDSS'")

    def fake_execute(sql, database):
        calls["execute"] += 1
        return empty if calls["execute"] == 1 else answered

    def fake_rewrite(**kwargs):
        calls["rewrite"] += 1
        assert "Resolved anchors" in kwargs["error_message"]
        return "SELECT project_name, 100 AS purchase_value FROM project_mst WHERE project_id = 2"

    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "execute_read_query", fake_execute)
    monkeypatch.setattr(chat_pipeline_module.llm, "rewrite_failed_mysql_sql", fake_rewrite)
    monkeypatch.setattr(chat_pipeline_module.llm, "generate_answer", lambda question, dataframe: "Answered")

    response = pipeline.ask_mysql("give me details on RDSS project", "demo", "mysql:demo")

    assert response["success"] is True
    assert response["answer"] == "Answered"
    assert "project_id = 2" in response["sql"]
    assert calls["rewrite"] == 1
    assert calls["execute"] == 2


def test_mysql_validation_failure_returns_structured_business_clarification(monkeypatch):
    pipeline = ChatPipeline()
    schema = {
        "project_mst": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "po_form": {
            "columns": [
                {"column_name": "vendor_name", "data_type": "varchar"},
                {"column_name": "total_value", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "po_items": {
            "columns": [
                {"column_name": "total_price", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
    }
    monkeypatch.setattr(
        chat_pipeline_module.question_classifier,
        "classify",
        lambda question: {"intent": "data_query", "normalized_question": question.lower()},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "analyze_question",
        lambda question, database: {"candidates": [], "metrics": [], "dimensions": []},
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "resolve_verified_entities",
        lambda question, database, **kwargs: {
            "clarification_required": False,
            "matches": [],
            "entities": [],
            "unresolved": [],
        },
    )
    monkeypatch.setattr(chat_pipeline_module.response_cache, "get_by_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.response_cache, "set_by_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "database_fingerprint", lambda *args, **kwargs: "dbfp")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_fingerprint", lambda database: "schemafp")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "source_identity", lambda database: f"mysql:{database}")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "relevant_tables", lambda *args, **kwargs: ["po_form", "po_items"])
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "database_memory", lambda database: {})
    monkeypatch.setattr(chat_pipeline_module.database_memory, "prompt_for_question", lambda *args, **kwargs: "MEMORY")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "schema_prompt", lambda *args, **kwargs: "SCHEMA")
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "_approx_row_counts", lambda database: {"project_mst": 2})
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "retrieve_schema_context",
        lambda *args, **kwargs: {
            "prompt": "Retrieved compact schema context",
            "result": {
                "selected_tables": ["po_form", "po_items"],
                "relevant_columns": {"po_form": ["vendor_name", "total_value"], "po_items": ["total_price"]},
            },
            "tables": ["po_form", "po_items"],
            "columns": {"po_form": ["vendor_name", "total_value"], "po_items": ["total_price"]},
        },
    )
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "count_table_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "catalog_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "business_overview_sql", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_pipeline_module.mysql_adapter, "validate_sql", lambda *args, **kwargs: {"valid": False, "errors": ["Unknown table(s): bad_table"]})
    monkeypatch.setattr(chat_pipeline_module.llm, "generate_mysql_sql", lambda question, schema_prompt: "SELECT * FROM bad_table")
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "execute_read_query",
        lambda sql, database: pd.DataFrame([
            {"project_name": "RDSS-VIZAG"},
            {"project_name": "RDSS-UGCABLE"},
        ]),
    )
    monkeypatch.setattr(
        chat_pipeline_module.mysql_adapter,
        "business_catalog",
        lambda database: {
            "metrics": [
                {"metric": "purchase_value", "table": "po_form", "column": "total_value", "confidence": 92},
                {"metric": "material_price", "table": "po_items", "column": "total_price", "confidence": 82},
            ]
        },
    )

    response = pipeline.ask_mysql(
        "tell me the cost of materials purchased for the last 30 days",
        "demo",
        "mysql:demo",
    )

    assert response["success"] is True
    assert response["sql"] is None
    assert "clarification" in response["answer"].lower() or "confirm" in response["answer"].lower() or "detail" in response["answer"].lower()
    assert "date" in response["answer"].lower() or "time period" in response["answer"].lower()
    assert "project" in response["answer"].lower() or "site" in response["answer"].lower()
    assert "cost" in response["answer"].lower() or "value" in response["answer"].lower()
    assert "PO total value" in response["answer"]
    assert "po_form.total_value" not in response["answer"]
    assert "RDSS-VIZAG" in response["answer"]
    assert "option" in response["answer"].lower()
    assert "Related questions that could be asked" in response["answer"]


def test_mysql_compare_po_clarification_is_manager_friendly(monkeypatch):
    pipeline = ChatPipeline()
    monkeypatch.setattr(
        pipeline,
        "_mysql_project_clarification_options",
        lambda database, question: ["RDSS-VIZAG", "RDSS-UGCABLE", "Yavatmal-Nanded"],
    )
    monkeypatch.setattr(
        pipeline,
        "_mysql_metric_clarification_options",
        lambda database, question, schema_context=None: [
            "PO total value\nTotal purchase order amount.",
            "PO count\nNumber of purchase orders.",
            "PO item value\nTotal value from purchase order line items.",
        ],
    )

    answer = pipeline._mysql_clarification_answer(
        question="compare any 2 projects by po",
        database="demo",
        semantic_plan={},
        schema_context={},
        errors=["SQL is missing required COUNT aggregation."],
    )

    assert "project" in answer.lower()
    assert "compare **" in answer.lower()
    assert "po" in answer.lower()
    assert "PO can mean total amount, order count, subtotal, or line-item value." in answer
    assert "`1A 2A`" in answer
    assert "Related questions that could be asked" in answer
    assert "SQL is missing required COUNT aggregation" not in answer


def test_mysql_empty_material_cost_result_should_clarify():
    pipeline = ChatPipeline()
    plan = semantic_parser.parse(
        "tell me the cost of materials purchased for the last 30 days",
        allow_llm_fallback=False,
    ).to_dict()

    assert pipeline._mysql_should_clarify_empty_result(
        "tell me the cost of materials purchased for the last 30 days",
        plan,
        pd.DataFrame(columns=["material", "cost"]),
    )


def test_mysql_material_purchase_cost_without_entity_clarifies_before_sql():
    pipeline = ChatPipeline()
    plan = semantic_parser.parse(
        "tell me the cost of materials purchased for the last 30 days",
        allow_llm_fallback=False,
    ).to_dict()

    assert pipeline._mysql_should_clarify_before_sql(
        "tell me the cost of materials purchased for the last 30 days",
        plan,
        {"matches": [], "entities": []},
    )
    assert not pipeline._mysql_should_clarify_before_sql(
        "tell me the cost of materials purchased for RDSS-VIZAG in the last 30 days",
        plan,
        {"matches": [{"entity": "RDSS-VIZAG"}], "entities": [{"value": "RDSS-VIZAG"}]},
    )


def test_mysql_material_issue_window_with_auto_pick_does_not_clarify_before_sql():
    pipeline = ChatPipeline()
    plan = semantic_parser.parse(
        "For the last 30 days, pick one project or site and tell me what materials were issued or used the most.",
        allow_llm_fallback=False,
    ).to_dict()

    assert not pipeline._mysql_should_clarify_before_sql(
        "For the last 30 days, pick one project or site and tell me what materials were issued or used the most.",
        plan,
        {"matches": [], "entities": []},
    )


def test_mysql_material_return_window_still_clarifies_before_sql():
    pipeline = ChatPipeline()
    plan = semantic_parser.parse(
        "For the last 30 days, how much material was returned for any one project?",
        allow_llm_fallback=False,
    ).to_dict()

    assert pipeline._mysql_should_clarify_before_sql(
        "For the last 30 days, how much material was returned for any one project?",
        plan,
        {"matches": [], "entities": []},
    )


def test_entity_resolver_extracts_business_identifiers():
    extracted = entity_resolver.extract("Show me details on RDSS-VIZAG project")

    assert any(item["text"] == "RDSS-VIZAG" for item in extracted)


def test_entity_resolver_scores_normalized_and_fuzzy_matches():
    normalized = entity_resolver.score_value("RDSS-VIZAG", "RDSS VIZAG")
    fuzzy = entity_resolver.score_value("ABC Industries", "ABC Industries Pvt Ltd")

    assert normalized[0] >= 90
    assert fuzzy[0] >= 70


def test_mysql_entity_resolution_uses_dynamic_value_columns(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "project_mst": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        }
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "_largest_tables", lambda database: [{"table": "project_mst", "approx_rows": 1}])
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda database: "schema-resolve-test")
    monkeypatch.setattr(adapter, "database_fingerprint", lambda database, connection_id=None: "db-resolve-test")

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [{"__pk": 421, "__value": "RDSS-VIZAG"}]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def exec_driver_sql(self, sql, params=None):
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(adapter, "engine", lambda database=None: FakeEngine())

    result = adapter.resolve_entity("give me details on RDSS-VIZAG project", "demo")

    assert result["matches"]
    assert result["matches"][0]["table"] == "project_mst"
    assert result["matches"][0]["primary_key"] == 421
    assert result["matches"][0]["confidence"] >= 90


def test_entity_resolution_returns_no_matches_without_hallucination(monkeypatch):
    adapter = MySQLAdapter()
    monkeypatch.setattr(
        adapter,
        "inspect_schema",
        lambda database: {
            "projects": {
                "columns": [
                    {"column_name": "project_id", "data_type": "int", "primary_key": True},
                    {"column_name": "project_name", "data_type": "varchar"},
                ],
                "foreign_keys": [],
            }
        },
    )
    monkeypatch.setattr(adapter, "_largest_tables", lambda database: [{"table": "projects", "approx_rows": 1}])
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda database: "schema-no-match")
    monkeypatch.setattr(adapter, "database_fingerprint", lambda database, connection_id=None: "db-no-match")

    class EmptyResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def exec_driver_sql(self, sql, params=None):
            return EmptyResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(adapter, "engine", lambda database=None: FakeEngine())

    result = adapter.resolve_entity("show Missing Project", "demo")

    assert result["matches"] == []


def test_mysql_catalog_sql_ranks_projects_by_project_value_before_plain_list(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "project_mst": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
                {"column_name": "project_value", "data_type": "decimal"},
                {"column_name": "client_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        }
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "_approx_row_counts", lambda database: {"project_mst": 10})

    sql = adapter.catalog_sql("show me the top 5 projects by project value", "demo")

    assert "project_value" in sql
    assert "ORDER BY metric_value DESC" in sql
    assert "LIMIT 5" in sql


def test_fallback_answer_uses_ranked_project_wording_for_metric_rows():
    pipeline = ChatPipeline()
    answer = pipeline._fallback_answer(
        "show me top 5 projects by project value",
        pd.DataFrame([
            {
                "project_id": 4,
                "project_name": "RDSS-UGCABLE",
                "client_name": "Demo Client",
                "project_value": 100,
                "metric_value": 100,
                "metric_column": "project_value",
            }
        ]),
    )

    assert "ranked by project value" in answer
    assert "project list only" not in answer


def test_mysql_catalog_sql_uses_billing_tables_before_generic_project_rank(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "project_mst": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
                {"column_name": "project_value", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "client_bill_raising": {
            "columns": [
                {"column_name": "billing_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_id", "data_type": "int"},
                {"column_name": "gross_bill_value", "data_type": "decimal"},
                {"column_name": "bill_amount", "data_type": "decimal"},
                {"column_name": "amount_to_be_received_this_bill", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "_approx_row_counts", lambda database: {"project_mst": 10, "client_bill_raising": 100})

    sql = adapter.catalog_sql("compare billing across projects and show top 3", "demo")

    assert "client_bill_raising" in sql
    assert "gross_bill_value" in sql
    assert "project_value" not in sql
    assert "LIMIT 3" in sql


def test_mysql_bill_count_output_does_not_turn_billing_ranking_into_count(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "project_mst": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "client_bill_raising": {
            "columns": [
                {"column_name": "billing_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_id", "data_type": "int"},
                {"column_name": "gross_bill_value", "data_type": "decimal"},
                {"column_name": "bill_amount", "data_type": "decimal"},
                {"column_name": "amount_to_be_received_this_bill", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "_approx_row_counts", lambda database: {"project_mst": 10, "client_bill_raising": 100})

    question = "Compare billing across projects and tell me the top 3 projects where billing is highest, with bill count also."
    sql = adapter.count_table_sql(question, "demo") or adapter.catalog_sql(question, "demo")

    assert "client_bill_raising" in sql
    assert "COUNT(*) AS bill_count" in sql
    assert "ORDER BY gross_bill_value DESC" in sql
    assert "LIMIT 3" in sql


def test_mysql_catalog_sql_supports_top_vendor_by_purchase_value(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "po_form": {
            "columns": [
                {"column_name": "po_form_id", "data_type": "int", "primary_key": True},
                {"column_name": "vendor_name", "data_type": "varchar"},
                {"column_name": "total_value", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        }
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)

    sql = adapter.catalog_sql("which vendor has the highest purchase value?", "demo")

    assert "vendor_name" in sql
    assert "total_purchase_value" in sql
    assert "ORDER BY total_purchase_value DESC" in sql
    assert "LIMIT 1" in sql


def test_query_plan_guard_accepts_group_by_with_newlines():
    plan = query_plan_guard.build(
        question="compare billing across projects and show top 3",
        semantic_plan=semantic_parser.parse(
            "compare billing across projects and show top 3",
            allow_llm_fallback=False,
        ).to_dict(),
        entity_resolution={"matches": [], "entities": [], "unresolved": []},
        retrieval={
            "selected_tables": ["client_bill_raising", "project_mst"],
            "relevant_columns": {},
            "relationship_paths": [],
        },
    )
    sql = """
SELECT p.project_name, SUM(c.gross_bill_value) AS gross_bill_value
FROM client_bill_raising c
JOIN project_mst p ON c.project_id = p.project_id
GROUP BY p.project_name
ORDER BY gross_bill_value DESC
LIMIT 3
"""

    result = query_plan_guard.validate_sql(sql, plan)

    assert result["valid"]


def test_entity_resolver_extracts_multiple_comparison_mentions():
    extracted = entity_resolver.extract("Compare RDSS-VIZAG and APDRP billing in 2025")
    texts = [item["text"] for item in extracted]

    assert "RDSS-VIZAG" in texts
    assert "APDRP" in texts


def test_entity_resolver_extracts_dates_status_and_limits_as_constraints():
    constraints = entity_resolver.extract_constraints(
        "Show top 5 pending invoices for ABC Industries in 2025"
    )

    assert constraints["limit"] == 5
    assert constraints["dates"][0]["start"] == "2025-01-01"
    assert constraints["filters"] == [{"type": "status", "values": ["pending"]}]


def test_entity_resolver_predicts_project_anchor_after_in_preposition():
    predicted = entity_resolver.predict_types(
        "RDSS-NELLORE",
        "materials used recently in RDSS-NELLORE",
    )

    assert "project" in predicted


def test_entity_resolver_predicts_project_anchor_for_plural_purchases():
    predicted = entity_resolver.predict_types(
        "Mission Bhagiratha",
        "what purchases happened for Mission Bhagiratha",
    )

    assert "project" in predicted


def test_mysql_semantic_empty_mentions_do_not_fall_back_to_phrase_extractor():
    adapter = MySQLAdapter()
    mentions = adapter._semantic_mentions(
        "give me top clients by billing",
        {"candidate_entity_mentions": []},
    )

    assert mentions == []


def test_multi_entity_result_preserves_same_type_comparison_order():
    result = entity_resolver.build_multi_result(
        "Compare RDSS-VIZAG and APDRP",
        [
            {
                "mention": "RDSS-VIZAG",
                "matches": [{
                    "entity": "RDSS-VIZAG",
                    "entity_type": "project",
                    "table": "project_mst",
                    "column": "project_name",
                    "primary_key": 2,
                    "value": "RDSS-VIZAG",
                    "confidence": 99,
                    "match_type": "Exact",
                }],
                "ambiguous": False,
            },
            {
                "mention": "APDRP",
                "matches": [{
                    "entity": "APDRP",
                    "entity_type": "project",
                    "table": "project_mst",
                    "column": "project_name",
                    "primary_key": 3,
                    "value": "APDRP",
                    "confidence": 96,
                    "match_type": "Exact",
                }],
                "ambiguous": False,
            },
        ],
    )

    assert [entity["mention"] for entity in result["entities"]] == ["RDSS-VIZAG", "APDRP"]
    assert result["comparisons"]
    assert result["relationships"][0]["relationship_type"] == "same_entity_type_comparison"
    assert result["needs_clarification"] is False


def test_multi_entity_result_reports_partial_unresolved_without_clarifying():
    result = entity_resolver.build_multi_result(
        "Compare RDSS-VIZAG and UNKNOWN-PROJECT",
        [
            {
                "mention": "RDSS-VIZAG",
                "matches": [{
                    "entity": "RDSS-VIZAG",
                    "entity_type": "project",
                    "table": "project_mst",
                    "column": "project_name",
                    "primary_key": 2,
                    "value": "RDSS-VIZAG",
                    "confidence": 99,
                    "match_type": "Exact",
                }],
                "ambiguous": False,
            },
            {"mention": "UNKNOWN-PROJECT", "matches": [], "ambiguous": False},
        ],
    )

    assert result["needs_clarification"] is False
    assert result["unresolved"][0]["mention"] == "UNKNOWN-PROJECT"
    assert result["combined_confidence"] == 0


def test_mysql_entity_relationship_validation_finds_join_path():
    adapter = MySQLAdapter()
    schema = {
        "project_master": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "vendor_master": {
            "columns": [
                {"column_name": "vendor_id", "data_type": "int", "primary_key": True},
                {"column_name": "vendor_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "invoice_table": {
            "columns": [
                {"column_name": "invoice_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_id", "data_type": "int"},
                {"column_name": "vendor_id", "data_type": "int"},
            ],
            "foreign_keys": [],
        },
    }
    entities = [
        {"mention": "Vendor ABC", "resolved_type": "vendor", "table": "vendor_master"},
        {"mention": "RDSS", "resolved_type": "project", "table": "project_master"},
    ]

    relationships = adapter._validate_entity_relationships(schema, entities)

    assert relationships[0]["relationship_type"] == "validated_join_path"
    assert relationships[0]["validated"] is True
    assert relationships[0]["confidence"] >= 55


def test_mysql_entity_relationship_validation_reports_gap_without_entity_clarification():
    adapter = MySQLAdapter()
    schema = {
        "project_master": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "vendor_master": {
            "columns": [
                {"column_name": "vendor_id", "data_type": "int", "primary_key": True},
                {"column_name": "vendor_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
    }
    entities = [
        {"mention": "Vendor ABC", "resolved_type": "vendor", "table": "vendor_master"},
        {"mention": "RDSS", "resolved_type": "project", "table": "project_master"},
    ]

    relationships = adapter._validate_entity_relationships(schema, entities)
    result = entity_resolver.build_multi_result(
        "Show Vendor ABC invoices for RDSS",
        [
            {"mention": "Vendor ABC", "matches": [{"entity_type": "vendor", "table": "vendor_master", "column": "vendor_name", "primary_key": 1, "value": "Vendor ABC", "confidence": 99, "match_type": "Exact"}], "ambiguous": False},
            {"mention": "RDSS", "matches": [{"entity_type": "project", "table": "project_master", "column": "project_name", "primary_key": 2, "value": "RDSS", "confidence": 99, "match_type": "Exact"}], "ambiguous": False},
        ],
        relationships=relationships,
    )

    assert relationships[0]["relationship_type"] == "no_reliable_path"
    assert relationships[0]["validated"] is False
    assert result["needs_clarification"] is False
    assert result["combined_confidence"] == 0
