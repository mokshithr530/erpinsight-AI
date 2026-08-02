from database_fingerprint import sha256_text, stable_json
from question_classifier import question_classifier
from relationship_discovery import RELATIONSHIP_GRAPH_VERSION
from schema_retriever import SCHEMA_RETRIEVER_VERSION
from query_plan import QUERY_PLAN_VERSION
from verified_sql_validator import VERIFIED_SQL_VALIDATOR_VERSION
from verified_execution import VERIFIED_EXECUTION_VERSION
from verified_sql_compiler import VERIFIED_SQL_COMPILER_VERSION
from config import LLM_POLISH_VERIFIED_ANSWERS


CACHE_IDENTITY_VERSION = "answer-cache:v81"
CATALOG_VERSION = "business-catalog:v6+verified-erp:v13"
MAPPING_VERSION = "erp-semantic-binder:v19+entity-v16-verified-first+parser-v15+clarification-v2+cross-v4"
PROMPT_TEMPLATE_VERSION = "prompt-template:v19+erp-inventory-context:v1"
RESULT_SHAPING_VERSION = "result-shaping:v38+po-empty-label-fallback:v1"


def sql_hash(sql: str | None) -> str | None:
    if not sql:
        return None
    return sha256_text(" ".join(sql.strip().split()).lower())


def resolution_hash(value: dict | None) -> str:
    return sha256_text(stable_json(value or {}))


def build_cache_identity(
    *,
    question: str,
    database_fingerprint: str,
    schema_fingerprint: str,
    dialect: str,
    source_identity: str,
    provider: str | None,
    model: str | None,
    session_scope: str | None = None,
    sql: str | None = None,
    resolution: dict | None = None,
    data_version: str | None = None,
) -> dict:
    classification = question_classifier.classify(question)
    metadata = {
        "cache_identity_version": CACHE_IDENTITY_VERSION,
        "database_fingerprint": database_fingerprint,
        "schema_fingerprint": schema_fingerprint,
        "catalog_version": CATALOG_VERSION,
        "relationship_graph_version": RELATIONSHIP_GRAPH_VERSION,
        "schema_retriever_version": SCHEMA_RETRIEVER_VERSION,
        "query_plan_version": QUERY_PLAN_VERSION,
        "sql_validator_version": VERIFIED_SQL_VALIDATOR_VERSION,
        "verified_execution_version": VERIFIED_EXECUTION_VERSION,
        "verified_sql_compiler_version": VERIFIED_SQL_COMPILER_VERSION,
        "mapping_version": MAPPING_VERSION,
        "database_dialect": dialect,
        "source_identity": source_identity,
        "question_hash": sha256_text(classification["normalized_question"]),
        "normalized_intent": classification["intent"],
        "resolution_hash": resolution_hash(resolution),
        "sql_hash": sql_hash(sql),
        "provider": provider,
        "model": model,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "result_shaping_version": RESULT_SHAPING_VERSION,
        "llm_polish_verified_answers": LLM_POLISH_VERIFIED_ANSWERS,
        "session_scope": session_scope,
        "data_version": data_version,
    }
    key_material = {
        key: value
        for key, value in metadata.items()
        if key not in {"data_version"}
    }
    return {
        "key": "sql_assistant:answer:" + sha256_text(stable_json(key_material)),
        "metadata": metadata,
        "classification": classification,
    }
