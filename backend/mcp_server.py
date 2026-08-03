import os
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from active_database import get_session_database_path, get_session_source
from config import MCP_DATABASE_PATH, QUERY_ROW_LIMIT
from database_manager import db_manager
from schema_reader import SchemaReader
from mysql_adapter import mysql_adapter
from sqlserver_adapter import sqlserver_adapter
from validator import validator
from business_catalog import business_catalog
from entity_resolver import entity_resolver
from relationship_discovery import relationship_discovery
from schema_retriever import schema_retriever
from semantic_parser import semantic_parser
from erp_semantic_binder import erp_semantic_binder
from erp_sql_context import erp_sql_context_builder
from query_plan import QueryPlan, query_plan_guard
from clarification_planner import ordered_clarification_planner
from verified_sql_compiler import verified_sql_compiler


mcp = FastMCP("SQL Assistant")
_SUPPORTED_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
_WORDS = re.compile(r"[A-Za-z0-9]+")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROFILE_LIMIT = 8
_SAMPLE_LIMIT = 10


def _tool_response(
    *,
    status: str,
    result: dict | list | None = None,
    confidence: float = 1.0,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    next_tools: list[str] | None = None,
    **compat,
) -> dict:
    payload = {
        "status": status,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "result": result or {},
        "reasoning": {
            "reasons": reasons or [],
        },
        "warnings": warnings or [],
        "next_recommended_tools": next_tools or [],
        "execution_metadata": {
            "tool_contract": "verified-erp-mcp:v2",
            "llm_free": True,
            "read_only": True,
        },
    }
    payload.update(compat)
    return payload


def _blocked_response(errors, *, warnings=None, next_tools=None, **compat):
    normalized_errors = [
        str(error)
        for error in (errors if isinstance(errors, list) else [errors])
        if str(error)
    ]
    return _tool_response(
        status="blocked",
        confidence=0.0,
        result={"errors": normalized_errors},
        reasons=normalized_errors,
        warnings=warnings or [],
        next_tools=next_tools or ["resolve_business_terms", "inspect_verified_modules"],
        **compat,
    )


def _catalog_items(catalog: dict, section: str) -> list[dict]:
    return [
        item
        for item in catalog.get(section) or []
        if isinstance(item, dict)
    ]


def _matches_text(item: dict, *terms: str) -> bool:
    haystack = " ".join(
        str(value).lower()
        for value in item.values()
        if not isinstance(value, (dict, list))
    )
    haystack = " ".join([
        haystack,
        " ".join(str(value).lower() for value in item.get("aliases", []) or []),
        " ".join(str(value).lower() for value in item.get("business_terms", []) or []),
    ])
    return all(str(term or "").lower() in haystack for term in terms if term)


def _query_plan_from_payload(query_plan: dict) -> QueryPlan:
    allowed = set(QueryPlan.__dataclass_fields__)
    return QueryPlan(**{
        key: value
        for key, value in dict(query_plan or {}).items()
        if key in allowed
    })


def _estimate_sql_cost(sql: str) -> dict:
    cleaned = str(sql or "")
    tables = re.findall(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*)`?", cleaned, re.IGNORECASE)
    joins = len(re.findall(r"\bJOIN\b", cleaned, re.IGNORECASE))
    aggregations = len(re.findall(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", cleaned, re.IGNORECASE))
    has_limit = bool(re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE))
    score = min(100, 15 + len(set(tables)) * 10 + joins * 15 + aggregations * 8 + (0 if has_limit else 20))
    level = "low" if score < 35 else "medium" if score < 70 else "high"
    return {
        "estimated_cost_score": score,
        "estimated_cost_level": level,
        "table_count": len(set(tables)),
        "join_count": joins,
        "aggregation_count": aggregations,
        "has_limit": has_limit,
    }


def _compat_payload(payload: dict) -> dict:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"status", "confidence", "result", "reasoning", "warnings", "next_recommended_tools", "execution_metadata"}
    }


def _database_path(
    database_path: str | None = None,
    session_id: str | None = None,
) -> Path:
    session_path = get_session_database_path(session_id) if session_id else None
    candidate = database_path or session_path or MCP_DATABASE_PATH
    if not candidate:
        raise ValueError(
            "No valid database selected. Pass database_path, provide a session_id "
            "with an uploaded database, or set MCP_DATABASE_PATH."
        )

    path = Path(candidate).expanduser().resolve()
    if path.suffix.lower() not in _SUPPORTED_EXTENSIONS or not path.is_file():
        raise ValueError(
            "No valid database selected. The resolved path must be an existing "
            ".db, .sqlite, or .sqlite3 file."
        )
    return path


def _source(database_path=None, session_id=None, source_type=None):
    if source_type == "sqlserver":
        return {"source_type": "sqlserver"}
    if source_type == "mysql":
        session_source = get_session_source(session_id) if session_id else None
        if session_source and session_source.get("source_type") == "mysql":
            return session_source
        raise ValueError("MySQL MCP tools require a session_id with an uploaded MySQL dump.")
    session_source = get_session_source(session_id) if session_id else None
    if session_source and session_source.get("source_type") == "sqlserver":
        return session_source
    if session_source and session_source.get("source_type") == "mysql":
        return session_source
    return {"source_type": "sqlite", "path": _database_path(database_path, session_id)}


def _mysql_database(database: str | None = None, session_id: str | None = None) -> str:
    if database:
        return database
    session_source = get_session_source(session_id) if session_id else None
    if session_source and session_source.get("source_type") == "mysql":
        return session_source["database"]
    raise ValueError(
        "Verified ERP MCP tools require a MySQL database name or a session_id "
        "attached to a MySQL ERP database."
    )


def _verified_erp_plan(question: str, database: str) -> dict:
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    catalog = mysql_adapter.business_catalog(database)
    semantic_binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=catalog,
    ).to_dict()
    entity_resolution = mysql_adapter.resolve_verified_entities(
        question,
        database,
        semantic_plan=semantic_plan,
        semantic_binding=semantic_binding,
    )
    semantic_binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=catalog,
        entity_resolution=entity_resolution,
    ).to_dict()
    clarification = ordered_clarification_planner.plan(
        question=question,
        semantic_plan=semantic_plan,
        entity_resolution=entity_resolution,
        semantic_binding=semantic_binding,
    )
    if clarification.required:
        return {
            "status": "clarification_required",
            "question": question,
            "database": database,
            "semantic_plan": semantic_plan,
            "entity_resolution": entity_resolution,
            "semantic_binding": semantic_binding,
            "clarification": clarification.to_context(question),
            "clarification_question": clarification.question,
            "options": clarification.options,
            "sql": None,
            "validation": {
                "valid": False,
                "errors": ["Clarification required before SQL generation."],
                "warnings": [],
            },
        }

    resolved_tables = [
        match["table"]
        for match in entity_resolution.get("matches", [])
        if match.get("confidence", 0) >= 70
    ]
    relevant_tables = list(dict.fromkeys([
        *(semantic_binding.get("required_tables") or []),
        *resolved_tables,
    ]))
    retrieval = {
        "selected_tables": relevant_tables,
        "relevant_columns": semantic_binding.get("required_columns") or {},
        "relationship_paths": semantic_binding.get("approved_relationships") or [],
        "retrieval_confidence": (semantic_binding.get("confidence_score", 0) / 100),
        "reasons": semantic_binding.get("reasons") or [],
        "warnings": semantic_binding.get("warnings") or [],
        "unresolved_concepts": [
            item.get("concept")
            for item in semantic_binding.get("unresolved_concepts") or []
            if isinstance(item, dict) and item.get("concept")
        ],
    }
    query_plan = query_plan_guard.build(
        question=question,
        semantic_plan=semantic_plan,
        entity_resolution=entity_resolution,
        retrieval=retrieval,
        semantic_binding=semantic_binding,
    )
    plan_validation = query_plan_guard.validate_plan(query_plan)
    schema = mysql_adapter.inspect_schema(database)
    canonical_context = None
    if semantic_binding.get("authoritative"):
        canonical_context = erp_sql_context_builder.build(
            schema=schema,
            query_plan=query_plan,
            semantic_binding=semantic_binding,
            catalog=catalog,
            schema_fingerprint=mysql_adapter.schema_fingerprint(database),
        )
        existing_generation_columns = dict(query_plan.generation_columns)
        query_plan.generation_tables = list(canonical_context.selected_tables)
        query_plan.generation_columns = query_plan_guard._merge_columns(
            existing_generation_columns,
            dict(canonical_context.selected_columns),
        )
    sql = verified_sql_compiler.compile(query_plan)
    validation = (
        mysql_adapter.validate_sql(sql, database)
        if sql
        else {"valid": False, "errors": ["No verified ERP SQL could be generated."], "warnings": []}
    )
    if sql and validation.get("valid"):
        plan_sql_validation = query_plan_guard.validate_sql(sql, query_plan, schema=schema)
        validation = {
            "valid": bool(plan_sql_validation["valid"]),
            "errors": [
                *validation.get("errors", []),
                *plan_sql_validation.get("errors", []),
            ],
            "warnings": [
                *validation.get("warnings", []),
                *plan_sql_validation.get("warnings", []),
            ],
        }
    return {
        "status": "ready" if plan_validation["valid"] and validation["valid"] else "blocked",
        "question": question,
        "database": database,
        "semantic_plan": semantic_plan,
        "entity_resolution": entity_resolution,
        "semantic_binding": semantic_binding,
        "query_plan": query_plan.to_dict(),
        "plan_validation": plan_validation,
        "canonical_context": canonical_context.to_dict() if canonical_context else None,
        "sql": sql,
        "validation": validation,
    }


def _connect_read_only(path: Path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER.match(identifier):
        raise ValueError(f"Invalid identifier: {identifier}")
    return f'"{identifier}"'


def _table_columns(path: Path, table_name: str) -> list[str]:
    schema = _schema(path)
    if table_name not in schema:
        raise ValueError(f"Unknown table: {table_name}")
    return [column["name"] for column in schema[table_name]["columns"]]


def _validated_columns(path: Path, table_name: str, columns: list[str] | None) -> list[str]:
    available = _table_columns(path, table_name)
    if not columns:
        return available[:_PROFILE_LIMIT]

    unknown = [column for column in columns if column not in available]
    if unknown:
        raise ValueError(f"Unknown column(s): {', '.join(unknown)}")
    return columns[:_PROFILE_LIMIT]


def _schema(path: Path) -> dict:
    with _connect_read_only(path) as connection:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        schema = {}
        for (table,) in table_rows:
            escaped = table.replace("'", "''")
            columns = connection.execute(
                f"PRAGMA table_info('{escaped}')"
            ).fetchall()
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list('{escaped}')"
            ).fetchall()
            schema[table] = {
                "columns": [
                    {
                        "name": row[1],
                        "type": row[2],
                        "not_null": bool(row[3]),
                        "primary_key": bool(row[5]),
                    }
                    for row in columns
                ],
                "foreign_keys": [
                    {"column": row[3], "table": row[2], "references": row[4]}
                    for row in foreign_keys
                ],
            }
        return schema


def _sqlite_row_counts(path: Path, schema: dict) -> dict:
    counts = {}
    with _connect_read_only(path) as connection:
        for table in schema:
            quoted_table = _quote_identifier(table)
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_table}"
            ).fetchone()[0]
    return counts


def _catalog_for_source(source):
    if source["source_type"] == "mysql":
        return mysql_adapter.business_catalog(source["database"])
    if source["source_type"] == "sqlserver":
        schema = sqlserver_adapter.inspect_schema()
        return business_catalog.build(schema, source_type="sqlserver")
    path = source["path"]
    schema = _schema(path)
    return business_catalog.build(
        schema,
        source_type="sqlite",
        row_counts=_sqlite_row_counts(path, schema),
        fingerprint=business_catalog.fingerprint(schema),
    )


def _sqlite_primary_key(schema, table_name):
    for column in schema[table_name]["columns"]:
        if column.get("primary_key"):
            return column["name"]
    for column in schema[table_name]["columns"]:
        if column["name"].lower() in {"id", f"{table_name.lower()}_id"}:
            return column["name"]
    return None


def _sqlite_resolve_entity(question, path: Path, entity_type=None):
    schema = _schema(path)
    row_counts = _sqlite_row_counts(path, schema)
    for table, row_count in row_counts.items():
        schema[table]["row_count"] = row_count
    catalog = business_catalog.build(
        schema,
        source_type="sqlite",
        row_counts=row_counts,
        fingerprint=business_catalog.fingerprint(schema),
    )
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    extracted = entity_resolver.candidates_from_plan(semantic_plan)
    retrieval = schema_retriever.retrieve(
        question=question,
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint=catalog["schema_fingerprint"],
        semantic_plan=semantic_plan,
        candidate_limit=12,
    )
    matches = []
    with _connect_read_only(path) as connection:
        for item in extracted:
            entity_text = item["text"]
            item_type = entity_type
            predicted_types = item.get("predicted_types") or []
            if item_type is None and len(predicted_types) == 1:
                item_type = predicted_types[0]
            preferred_candidates = entity_resolver.candidate_columns(
                schema,
                catalog=catalog,
                entity_type=item_type,
                max_columns=120,
                relevant_tables=retrieval.selected_tables,
                relevant_columns=retrieval.relevant_columns,
            )
            fallback_candidates = entity_resolver.candidate_columns(
                schema,
                catalog=catalog,
                entity_type=item_type,
                max_columns=120,
            )
            mention_matches = []
            search_stages = [preferred_candidates]
            if retrieval.selected_tables:
                search_stages.append(fallback_candidates)
            for candidates in search_stages:
                if mention_matches:
                    break
                for candidate in candidates:
                    table = candidate["table"]
                    column = candidate["column"]
                    variants = entity_resolver.lookup_variants(entity_text, candidate.get("entity_type"))
                    normalized_variants = [entity_resolver.normalize(value) for value in variants]
                    compact_variants = [entity_resolver.compact(value) for value in variants]
                    like_terms = list(dict.fromkeys(
                        term
                        for normalized in normalized_variants
                        for term in normalized.split()
                        if len(term) >= 2
                    ))[:6]
                    pk = _sqlite_primary_key(schema, table)
                    quoted_table = _quote_identifier(table)
                    quoted_column = _quote_identifier(column)
                    value_expr = f"CAST({quoted_column} AS TEXT)"
                    normalized_expr = (
                        f"LOWER(TRIM(REPLACE(REPLACE(REPLACE({value_expr}, '-', ' '), '_', ' '), '.', ' ')))"
                    )
                    compact_expr = (
                        f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(TRIM({value_expr}), '-', ''), '_', ''), '.', ''), ' ', ''))"
                    )
                    conditions = []
                    params = []
                    for variant, normalized, compact in zip(variants, normalized_variants, compact_variants):
                        conditions.extend([
                            f"LOWER(TRIM({value_expr})) = ?",
                            f"{normalized_expr} = ?",
                            f"{compact_expr} = ?",
                        ])
                        params.extend([variant.strip().lower(), normalized, compact])
                    for term in like_terms:
                        conditions.append(f"LOWER({value_expr}) LIKE ?")
                        params.append(f"%{term}%")
                    select_pk = _quote_identifier(pk) if pk else "NULL"
                    rows = connection.execute(
                        f"""
                        SELECT {select_pk} AS __pk, {quoted_column} AS __value
                        FROM {quoted_table}
                        WHERE {quoted_column} IS NOT NULL
                          AND ({' OR '.join(conditions)})
                        LIMIT 10
                        """,
                        params,
                    ).fetchall()
                    for row in rows:
                        scored = entity_resolver.score_resolved_value(
                            entity_text,
                            row[1],
                            candidate.get("entity_type"),
                        )
                        if not scored:
                            continue
                        confidence, match_type = scored
                        mention_matches.append({
                            "entity": entity_text,
                            "entity_type": candidate.get("entity_type") or "reference",
                            "table": table,
                            "column": column,
                            "primary_key_column": pk,
                            "primary_key": row[0],
                            "primary_key_value": row[0],
                            "value": row[1],
                            "matched_value": row[1],
                            "confidence": min(99, confidence + min(8, candidate.get("score", 0) // 15)),
                            "match_type": match_type,
                            "source": "value_lookup",
                        })
            matches.extend(mention_matches)
    matches = sorted(matches, key=lambda item: (-item["confidence"], item["table"], item["column"]))[:12]
    return {
        "question": question,
        "extracted_entities": extracted,
        "matches": matches,
        "ambiguous": len({(m["entity_type"], m["table"], str(m["primary_key"])) for m in matches[:3]}) > 1 and len(matches) > 1,
        "schema_fingerprint": catalog["schema_fingerprint"],
        "catalog_version": catalog["catalog_version"],
        "resolved_database": str(path),
        "source_type": "sqlite",
        "semantic_plan": semantic_plan,
        "searched_tables_first": retrieval.selected_tables,
    }


def _validate(sql: str, path: Path) -> dict:
    db_manager.connect(str(path))
    try:
        result = validator.validate(sql)
    finally:
        db_manager.disconnect()

    error = result.get("error")
    warnings = []
    if result["success"] and not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        warnings.append(f"Results will be limited to {QUERY_ROW_LIMIT} rows.")
    return {
        "valid": result["success"],
        "errors": [] if result["success"] else [error or "SQL validation failed."],
        "warnings": warnings,
        "resolved_database": str(path),
    }


@mcp.tool()
def inspect_schema(
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Return real tables, columns, primary keys, and foreign keys."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "sqlserver":
        return {
            "database": source.get("database"),
            "source_type": "sqlserver",
            "schema": sqlserver_adapter.inspect_schema(),
        }
    if source["source_type"] == "mysql":
        return {
            "database": source.get("database"),
            "source_type": "mysql",
            "schema": mysql_adapter.inspect_schema(source["database"]),
        }
    path = source["path"]
    return {"database": str(path), "schema": _schema(path)}


@mcp.tool()
def validate_sql(
    sql: str,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Validate one read-only SELECT/WITH query against the resolved schema."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "sqlserver":
        validation = sqlserver_adapter.validate_sql(sql)
        return {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": [],
            "resolved_database": source.get("database"),
            "source_type": "sqlserver",
        }
    if source["source_type"] == "mysql":
        validation = mysql_adapter.validate_sql(sql, source["database"])
        return {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": [],
            "resolved_database": source.get("database"),
            "source_type": "mysql",
        }
    path = source["path"]
    return _validate(sql, path)


@mcp.tool()
def dry_run_query(
    sql: str,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Validate SQL and return SQLite EXPLAIN QUERY PLAN without running it."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "sqlserver":
        return {
            **sqlserver_adapter.dry_run_query(sql),
            "resolved_database": source.get("database"),
            "source_type": "sqlserver",
        }
    if source["source_type"] == "mysql":
        return {
            **mysql_adapter.dry_run_query(sql, source["database"]),
            "resolved_database": source.get("database"),
            "source_type": "mysql",
        }
    path = source["path"]
    validation = _validate(sql, path)
    if not validation["valid"]:
        return {**validation, "query_plan": []}

    with _connect_read_only(path) as connection:
        rows = connection.execute(f"EXPLAIN QUERY PLAN {sql.strip()}").fetchall()
    return {
        **validation,
        "query_plan": [
            {"id": row[0], "parent": row[1], "not_used": row[2], "detail": row[3]}
            for row in rows
        ],
    }


@mcp.tool()
def execute_read_query(
    sql: str,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Validate and execute one bounded, read-only SQLite query."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "sqlserver":
        dataframe = sqlserver_adapter.execute_read_query(sql)
        return {
            "sql": sql.strip().rstrip(";"),
            "columns": list(dataframe.columns),
            "rows": dataframe.to_dict(orient="records"),
            "row_count": len(dataframe),
            "resolved_database": source.get("database"),
            "source_type": "sqlserver",
        }
    if source["source_type"] == "mysql":
        dataframe = mysql_adapter.execute_read_query(sql, source["database"])
        return {
            "sql": sql.strip().rstrip(";"),
            "columns": list(dataframe.columns),
            "rows": dataframe.to_dict(orient="records"),
            "row_count": len(dataframe),
            "resolved_database": source.get("database"),
            "source_type": "mysql",
        }
    path = source["path"]
    validation = _validate(sql, path)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"]))

    cleaned = sql.strip().rstrip(";")
    limited_sql = f"SELECT * FROM ({cleaned}) LIMIT {QUERY_ROW_LIMIT}"
    with _connect_read_only(path) as connection:
        cursor = connection.execute(limited_sql)
        columns = [description[0] for description in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return {
        "sql": cleaned,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "resolved_database": str(path),
    }


@mcp.tool()
def analyze_question(
    question: str,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Match question keywords to real schema tables and columns without an LLM."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "mysql":
        result = mysql_adapter.analyze_question(question, source["database"])
        resolved_database = source.get("database")
    elif source["source_type"] == "sqlserver":
        schema = sqlserver_adapter.inspect_schema()
        result = business_catalog.analyze_question(question, schema, source_type="sqlserver")
        resolved_database = source.get("database")
    else:
        path = source["path"]
        schema = _schema(path)
        result = business_catalog.analyze_question(
            question,
            schema,
            source_type="sqlite",
            row_counts=_sqlite_row_counts(path, schema),
        )
        resolved_database = str(path)

    return {
        "question": question,
        "candidates": result["candidates"],
        "missing_kpis": result["missing_kpis"],
        "metrics": result.get("metrics", []),
        "dimensions": result.get("dimensions", []),
        "ambiguity": result.get("ambiguity", []),
        "catalog_version": result.get("catalog_version"),
        "schema_fingerprint": result["schema_fingerprint"],
        "resolved_database": resolved_database,
        "source_type": source["source_type"],
    }


@mcp.tool()
def resolve_entity(
    question: str,
    entity_type: str | None = None,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Resolve business object names/codes in a question to real table rows without an LLM."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "mysql":
        result = mysql_adapter.resolve_entity(question, source["database"], entity_type=entity_type)
        return {
            **result,
            "resolved_database": source["database"],
            "source_type": "mysql",
        }
    if source["source_type"] == "sqlserver":
        return {
            "question": question,
            "extracted_entities": entity_resolver.extract(question),
            "matches": [],
            "ambiguous": False,
            "resolved_database": source.get("database"),
            "source_type": "sqlserver",
            "limitations": ["SQL Server entity value lookup is not enabled in this MCP tool yet."],
        }
    return _sqlite_resolve_entity(question, source["path"], entity_type=entity_type)


@mcp.tool()
def resolve_entities(
    question: str,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Resolve multiple entities, dates, filters, and relationships in one question without an LLM."""
    source = _source(database_path, session_id, source_type)
    if source["source_type"] == "mysql":
        result = mysql_adapter.resolve_entities(question, source["database"])
        return {
            **result,
            "resolved_database": source["database"],
            "source_type": "mysql",
        }
    if source["source_type"] == "sqlserver":
        extracted = entity_resolver.extract(question)
        multi = entity_resolver.build_multi_result(
            question,
            [{"mention": item["text"], "matches": [], "ambiguous": False} for item in extracted],
        )
        return {
            **multi,
            "question": question,
            "extracted_entities": extracted,
            "resolved_database": source.get("database"),
            "source_type": "sqlserver",
            "limitations": ["SQL Server multi-entity value lookup is not enabled in this MCP tool yet."],
        }
    single = _sqlite_resolve_entity(question, source["path"])
    semantic_plan = single.get("semantic_plan") or semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    mention_results = []
    grouped = {}
    for match in single.get("matches", []):
        grouped.setdefault(match["entity"], []).append(match)
    extracted = single.get("extracted_entities", [])
    for item in extracted:
        matches = grouped.get(item["text"], [])
        mention_results.append({
            "mention": item["text"],
            "matches": matches,
            "ambiguous": len(matches) > 1,
            "predicted_types": item.get("predicted_types", []),
            "essential": entity_resolver.mention_is_essential(item["text"], semantic_plan),
        })
    multi = entity_resolver.build_multi_result(question, mention_results, semantic_plan=semantic_plan)
    return {
        **single,
        **multi,
        "source_type": "sqlite",
    }


@mcp.tool()
def database_overview(
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Return compact dynamic business catalog overview without using an LLM."""
    source = _source(database_path, session_id, source_type)
    catalog = _catalog_for_source(source)
    return {
        "source_type": source["source_type"],
        "catalog_version": catalog["catalog_version"],
        "database_fingerprint": catalog["database_fingerprint"],
        "schema_fingerprint": catalog["schema_fingerprint"],
        "table_count": catalog["table_count"],
        "detected_modules": list(catalog["entities"].keys())[:12],
        "important_tables": catalog["important_tables"][:12],
        "metrics": catalog["metrics"][:20],
        "dimensions": catalog["dimensions"][:20],
        "ambiguity": catalog["ambiguity"][:12],
        "unsupported_kpis": catalog["unsupported_kpis"],
        "summary": catalog["summary"],
    }


@mcp.tool()
def discover_business_entities(
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Return discovered business entities, aliases, and matched schema objects."""
    source = _source(database_path, session_id, source_type)
    catalog = _catalog_for_source(source)
    return {
        "source_type": source["source_type"],
        "catalog_version": catalog["catalog_version"],
        "database_fingerprint": catalog["database_fingerprint"],
        "schema_fingerprint": catalog["schema_fingerprint"],
        "entities": catalog["entities"],
        "aliases": catalog["aliases"],
        "metrics": catalog["metrics"][:40],
        "dimensions": catalog["dimensions"][:40],
        "ambiguity": catalog["ambiguity"][:12],
        "unsupported_kpis": catalog["unsupported_kpis"],
    }


@mcp.tool()
def discover_join_paths(
    start_tables: list[str] | None = None,
    end_tables: list[str] | None = None,
    session_id: str | None = None,
    database_path: str | None = None,
    source_type: str | None = None,
) -> dict:
    """Return declared and inferred join paths with evidence/confidence."""
    source = _source(database_path, session_id, source_type)
    catalog = _catalog_for_source(source)
    paths = []
    if start_tables and end_tables:
        if source["source_type"] == "mysql":
            schema = mysql_adapter.inspect_schema(source["database"])
        elif source["source_type"] == "sqlserver":
            schema = sqlserver_adapter.inspect_schema()
        else:
            schema = _schema(source["path"])
        paths = relationship_discovery.find_paths(schema, start_tables, end_tables)
    return {
        "source_type": source["source_type"],
        "catalog_version": catalog["catalog_version"],
        "database_fingerprint": catalog["database_fingerprint"],
        "schema_fingerprint": catalog["schema_fingerprint"],
        "relationship_graph": catalog["relationship_graph"],
        "join_paths": catalog["join_paths"],
        "paths": paths,
    }


@mcp.tool()
def inspect_verified_modules(
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Use first. Lists verified ERP modules. Do not call for raw SQL execution."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    modules = {}
    for section in ["business_concepts", "metrics", "dimensions", "join_paths", "business_definitions"]:
        for item in _catalog_items(catalog, section):
            module = item.get("applicable_module") or item.get("module")
            if module:
                modules.setdefault(str(module), {
                    "module": str(module),
                    "concept_count": 0,
                    "metric_count": 0,
                    "dimension_count": 0,
                    "relationship_count": 0,
                })
                if section == "business_concepts":
                    modules[str(module)]["concept_count"] += 1
                elif section == "metrics":
                    modules[str(module)]["metric_count"] += 1
                elif section == "dimensions":
                    modules[str(module)]["dimension_count"] += 1
                elif section == "join_paths":
                    modules[str(module)]["relationship_count"] += 1
    result = {
        "database": resolved_database,
        "schema_fingerprint": catalog.get("schema_fingerprint"),
        "modules": sorted(modules.values(), key=lambda item: item["module"]),
        "unsupported_kpis": catalog.get("unsupported_kpis", []),
    }
    return _tool_response(
        status="success",
        confidence=0.99 if modules else 0.35,
        result=result,
        reasons=["loaded verified ERP catalog modules"],
        next_tools=["detect_required_modules", "inspect_verified_metrics", "inspect_verified_entities"],
        **result,
    )


@mcp.tool()
def detect_required_modules(
    question: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Classify which verified ERP modules are relevant. Call before metric or join lookup."""
    resolved_database = _mysql_database(database, session_id)
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    catalog = mysql_adapter.business_catalog(resolved_database)
    binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=catalog,
    ).to_dict()
    modules = list(dict.fromkeys(
        str(item.get("applicable_module") or item.get("module"))
        for section in ["bound_metrics", "bound_dimensions", "approved_relationships"]
        for item in binding.get(section) or []
        if isinstance(item, dict) and (item.get("applicable_module") or item.get("module"))
    ))
    if not modules:
        modules = [
            module
            for module in ["billing", "purchase_orders", "project_master", "vendors", "store_material", "inventory"]
            if module.replace("_", " ") in question.lower()
        ]
    result = {
        "question": question,
        "modules": modules,
        "semantic_plan": semantic_plan,
        "binding_confidence": binding.get("confidence_score", 0) / 100,
        "unresolved_concepts": binding.get("unresolved_concepts", []),
    }
    return _tool_response(
        status="success" if modules else "needs_more_evidence",
        confidence=result["binding_confidence"] if modules else 0.25,
        result=result,
        reasons=binding.get("reasons") or ["detected modules from verified catalog binding"],
        warnings=binding.get("warnings") or [],
        next_tools=["resolve_entities", "resolve_dates", "resolve_business_terms"],
        **result,
    )


@mcp.tool()
def inspect_verified_metrics(
    module: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """List verified metrics for one module. Do not use to invent unsupported formulas."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    module_lower = str(module or "").lower()
    metrics = [
        item
        for item in _catalog_items(catalog, "metrics")
        if module_lower in str(item.get("applicable_module") or item.get("module") or "").lower()
    ]
    definitions = [
        item
        for item in _catalog_items(catalog, "business_definitions")
        if module_lower in str(item.get("applicable_module") or item.get("module") or "").lower()
    ]
    result = {
        "database": resolved_database,
        "module": module,
        "metrics": metrics,
        "business_definitions": definitions,
    }
    return _tool_response(
        status="success" if metrics or definitions else "not_found",
        confidence=0.98 if metrics or definitions else 0.2,
        result=result,
        reasons=["returned verified module metrics only"],
        next_tools=["get_verified_metric", "get_verified_dimension", "get_verified_join_path"],
        **result,
    )


@mcp.tool()
def inspect_verified_entities(
    entity_type: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """List verified entity types and their tables. Use before resolving entity names."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    target = str(entity_type or "").lower()
    entities = {}
    for key, value in (catalog.get("entities") or {}).items():
        if target and target not in str(key).lower():
            continue
        entities[key] = value
    result = {
        "database": resolved_database,
        "entities": entities,
        "aliases": catalog.get("aliases", {}),
    }
    return _tool_response(
        status="success" if entities else "not_found",
        confidence=0.97 if entities else 0.25,
        result=result,
        reasons=["returned verified entity types from catalog"],
        next_tools=["resolve_entities"],
        **result,
    )


@mcp.tool()
def resolve_dates(question: str) -> dict:
    """Extract date constraints only. Does not inspect schema or execute SQL."""
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    dates = semantic_plan.get("date_constraints") or []
    result = {
        "question": question,
        "date_constraints": dates,
    }
    return _tool_response(
        status="success" if dates else "not_found",
        confidence=0.95 if dates else 0.6,
        result=result,
        reasons=["parsed dates deterministically from user question"],
        next_tools=["resolve_business_terms", "plan_verified_query"],
        **result,
    )


@mcp.tool()
def resolve_business_terms(
    question: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Bind user wording to verified ERP concepts, metrics and dimensions. Does not generate SQL."""
    resolved_database = _mysql_database(database, session_id)
    semantic_plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    catalog = mysql_adapter.business_catalog(resolved_database)
    binding = erp_semantic_binder.bind(
        question=question,
        semantic_plan=semantic_plan,
        catalog=catalog,
    ).to_dict()
    result = {
        "question": question,
        "semantic_plan": semantic_plan,
        "semantic_binding": binding,
    }
    return _tool_response(
        status="clarification_required" if binding.get("clarification_required") else "success",
        confidence=binding.get("confidence_score", 0) / 100,
        result=result,
        reasons=binding.get("reasons") or [],
        warnings=binding.get("warnings") or [],
        next_tools=["resolve_entities", "get_verified_metric", "plan_verified_query"],
        **result,
    )


@mcp.tool()
def inspect_verified_erp_catalog(
    module: str | None = None,
    concept: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Return verified ERP catalog facts for a MySQL ERP database without using an LLM."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    module_lower = str(module or "").lower()
    concept_lower = str(concept or "").lower()

    def keep(item):
        text = " ".join(str(value).lower() for value in item.values() if not isinstance(value, (list, dict)))
        if module_lower and module_lower not in text:
            return False
        if concept_lower and concept_lower not in text:
            aliases = " ".join(
                str(alias).lower()
                for alias in item.get("aliases", [])
            )
            return concept_lower in aliases
        return True

    sections = {}
    for section in [
        "business_concepts",
        "metrics",
        "dimensions",
        "date_fields",
        "status_fields",
        "business_definitions",
        "unresolved_definitions",
    ]:
        values = catalog.get(section) or []
        if isinstance(values, list):
            sections[section] = [
                item for item in values
                if isinstance(item, dict) and keep(item)
            ][:25]
    result = {
        "source_type": "mysql",
        "database": resolved_database,
        "catalog_version": catalog.get("catalog_version"),
        "schema_fingerprint": catalog.get("schema_fingerprint"),
        "authoritative": bool(catalog.get("verified_catalog")),
        "sections": sections,
        "important_tables": catalog.get("important_tables", [])[:20],
        "unsupported_kpis": catalog.get("unsupported_kpis", []),
    }
    return _tool_response(
        status="success",
        confidence=0.99 if result["authoritative"] else 0.55,
        result=result,
        reasons=["returned verified ERP catalog facts only"],
        next_tools=["detect_required_modules", "inspect_verified_metrics", "resolve_business_terms"],
        **result,
    )


@mcp.tool()
def get_verified_metric(
    metric: str,
    module: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Find verified ERP metric definitions, source columns, formulas, and grains."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    target = str(metric or "").lower()
    module_lower = str(module or "").lower()
    matches = []
    for item in [
        *(catalog.get("metrics") or []),
        *(catalog.get("business_definitions") or []),
    ]:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(value).lower()
            for value in item.values()
            if not isinstance(value, (list, dict))
        )
        aliases = " ".join(str(alias).lower() for alias in item.get("aliases", []))
        if target and target not in text and target not in aliases:
            continue
        if module_lower and module_lower not in text:
            continue
        matches.append(item)
    result = {
        "source_type": "mysql",
        "database": resolved_database,
        "metric": metric,
        "matches": matches[:12],
        "unresolved": not bool(matches),
        "message": (
            "No verified metric found. Ask for clarification or add this KPI to the ERP catalog."
            if not matches else None
        ),
    }
    return _tool_response(
        status="success" if matches else "not_found",
        confidence=0.98 if matches else 0.2,
        result=result,
        reasons=["searched verified ERP metrics and business definitions"],
        warnings=[] if matches else ["No verified metric matched the requested wording."],
        next_tools=["get_verified_dimension", "get_verified_join_path", "plan_verified_query"],
        **result,
    )


@mcp.tool()
def get_verified_dimension(
    name: str,
    module: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Find verified ERP dimensions such as project, vendor, material, client or store."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    target = str(name or "").lower()
    module_lower = str(module or "").lower()
    matches = []
    for item in _catalog_items(catalog, "dimensions"):
        if target and not _matches_text(item, target):
            continue
        if module_lower and module_lower not in str(item.get("applicable_module") or item.get("module") or "").lower():
            continue
        matches.append(item)
    result = {
        "database": resolved_database,
        "dimension": name,
        "matches": matches[:12],
        "unresolved": not bool(matches),
    }
    return _tool_response(
        status="success" if matches else "not_found",
        confidence=0.97 if matches else 0.2,
        result=result,
        reasons=["searched verified ERP dimensions"],
        next_tools=["resolve_entities", "get_verified_join_path", "plan_verified_query"],
        **result,
    )


@mcp.tool()
def get_verified_join_path(
    from_concept: str,
    to_concept: str,
    module: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Return approved ERP join paths and duplicate-risk notes for two concepts."""
    resolved_database = _mysql_database(database, session_id)
    catalog = mysql_adapter.business_catalog(resolved_database)
    left = str(from_concept or "").lower()
    right = str(to_concept or "").lower()
    module_lower = str(module or "").lower()
    matches = []
    for item in catalog.get("join_paths") or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(value).lower() for value in item.values() if not isinstance(value, (list, dict)))
        if left in text and right in text and (not module_lower or module_lower in text):
            matches.append(item)
    if not matches:
        for item in catalog.get("relationship_evidence") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(value).lower() for value in item.values() if not isinstance(value, (list, dict)))
            if left in text and right in text and (not module_lower or module_lower in text):
                matches.append(item)
    result = {
        "source_type": "mysql",
        "database": resolved_database,
        "from_concept": from_concept,
        "to_concept": to_concept,
        "approved_paths": matches[:10],
        "unresolved": not bool(matches),
        "message": (
            "No approved ERP relationship found. Do not guess this join; ask clarification or mark unsupported."
            if not matches else None
        ),
    }
    return _tool_response(
        status="success" if matches else "clarification_required",
        confidence=0.95 if matches else 0.15,
        result=result,
        reasons=["searched approved ERP relationship graph"],
        warnings=[] if matches else ["No approved ERP relationship path was found."],
        next_tools=["plan_verified_query"] if matches else ["resolve_business_terms", "inspect_verified_modules"],
        **result,
    )


@mcp.tool()
def check_relationship(
    entity_a: str,
    entity_b: str,
    module: str | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Check whether two ERP concepts have an approved relationship. Never infer a risky join."""
    result = get_verified_join_path(
        entity_a,
        entity_b,
        module=module,
        session_id=session_id,
        database=database,
    )
    approved = result.get("approved_paths") or []
    payload = {
        "entity_a": entity_a,
        "entity_b": entity_b,
        "approved": bool(approved),
        "approved_paths": approved,
    }
    return _tool_response(
        status="success" if approved else "clarification_required",
        confidence=0.95 if approved else 0.15,
        result=payload,
        reasons=["checked verified relationship graph"],
        warnings=[] if approved else ["No approved relationship path was found."],
        next_tools=["plan_verified_query"] if approved else ["inspect_verified_modules", "resolve_business_terms"],
        **payload,
    )


@mcp.tool()
def plan_verified_erp_query(
    question: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Build a verified ERP query plan and SQL candidate without executing it."""
    resolved_database = _mysql_database(database, session_id)
    plan = _verified_erp_plan(question, resolved_database)
    next_tools = []
    if plan.get("status") == "ready":
        next_tools = ["compile_verified_sql", "validate_sql", "execute_verified_sql"]
    elif plan.get("status") == "clarification_required":
        next_tools = ["resolve_entities", "resolve_business_terms"]
    else:
        next_tools = ["inspect_verified_modules", "resolve_business_terms"]
    return _tool_response(
        status=plan.get("status", "blocked"),
        confidence=0.9 if plan.get("status") == "ready" else 0.25,
        result=plan,
        reasons=["built verified ERP query plan without executing SQL"],
        warnings=[
            *plan.get("plan_validation", {}).get("warnings", []),
            *plan.get("validation", {}).get("warnings", []),
        ],
        next_tools=next_tools,
        **_compat_payload(plan),
    )


@mcp.tool()
def compile_verified_sql(
    query_plan: dict,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Compile SQL from a verified query plan only. Does not execute SQL."""
    resolved_database = _mysql_database(database, session_id)
    plan = _query_plan_from_payload(query_plan)
    sql = verified_sql_compiler.compile(plan)
    if not sql:
        return _blocked_response(
            "No verified SQL pattern matched this query plan.",
            next_tools=["resolve_business_terms", "plan_verified_query"],
            database=resolved_database,
            sql=None,
        )
    validation = mysql_adapter.validate_sql(sql, resolved_database)
    result = {
        "database": resolved_database,
        "sql": sql,
        "validation": validation,
    }
    return _tool_response(
        status="success" if validation.get("valid") else "blocked",
        confidence=0.98 if validation.get("valid") else 0.2,
        result=result,
        reasons=["compiled SQL with verified SQL compiler"],
        warnings=validation.get("warnings", []),
        next_tools=["validate_sql", "estimate_query_cost"],
        **result,
    )


@mcp.tool()
def estimate_query_cost(
    sql: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Estimate query risk before execution. Does not execute SQL or inspect data values."""
    resolved_database = _mysql_database(database, session_id)
    validation = mysql_adapter.validate_sql(sql, resolved_database)
    cost = _estimate_sql_cost(sql)
    result = {
        "database": resolved_database,
        "validation": validation,
        "cost": cost,
    }
    status = "success" if validation.get("valid") else "blocked"
    return _tool_response(
        status=status,
        confidence=0.9 if validation.get("valid") else 0.1,
        result=result,
        reasons=["estimated query cost from SQL shape and validation result"],
        warnings=validation.get("warnings", []),
        next_tools=["execute_verified_sql"] if validation.get("valid") else ["compile_verified_sql"],
        **result,
    )


@mcp.tool()
def execute_verified_sql(
    sql: str,
    query_plan: dict | None = None,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Execute only validated read-only SQL. Prefer SQL from compile_verified_sql."""
    resolved_database = _mysql_database(database, session_id)
    validation = mysql_adapter.validate_sql(sql, resolved_database)
    if validation.get("valid") and query_plan:
        schema = mysql_adapter.inspect_schema(resolved_database)
        plan_validation = query_plan_guard.validate_sql(
            sql,
            _query_plan_from_payload(query_plan),
            schema=schema,
        )
        validation = {
            "valid": bool(plan_validation["valid"]),
            "errors": [
                *validation.get("errors", []),
                *plan_validation.get("errors", []),
            ],
            "warnings": [
                *validation.get("warnings", []),
                *plan_validation.get("warnings", []),
            ],
        }
    if not validation.get("valid"):
        return _blocked_response(
            validation.get("errors") or ["SQL validation failed."],
            warnings=validation.get("warnings", []),
            next_tools=["compile_verified_sql", "validate_sql"],
            database=resolved_database,
            sql=sql,
            validation=validation,
        )
    dataframe = mysql_adapter.execute_read_query(sql, resolved_database)
    result = {
        "database": resolved_database,
        "sql": sql.strip().rstrip(";"),
        "columns": list(dataframe.columns),
        "rows": dataframe.to_dict(orient="records"),
        "row_count": len(dataframe),
        "validation": validation,
    }
    return _tool_response(
        status="success",
        confidence=0.98,
        result=result,
        reasons=["validated SQL before read-only execution"],
        warnings=validation.get("warnings", []),
        next_tools=["format_result"],
        **result,
    )


@mcp.tool()
def format_result(
    rows: list[dict],
    columns: list[str] | None = None,
    question: str | None = None,
) -> dict:
    """Format executed rows into a deterministic structured result. Does not call an LLM."""
    selected_columns = columns or list(rows[0].keys()) if rows else (columns or [])
    result = {
        "question": question,
        "columns": selected_columns,
        "rows": rows,
        "row_count": len(rows),
        "empty": len(rows) == 0,
    }
    return _tool_response(
        status="success",
        confidence=1.0,
        result=result,
        reasons=["formatted already-executed facts without adding new claims"],
        warnings=[] if rows else ["No rows were returned."],
        next_tools=[],
        **result,
    )


@mcp.tool()
def execute_verified_erp_query(
    question: str,
    session_id: str | None = None,
    database: str | None = None,
) -> dict:
    """Plan, validate, and execute one verified read-only ERP question without an LLM."""
    resolved_database = _mysql_database(database, session_id)
    plan = _verified_erp_plan(question, resolved_database)
    if plan["status"] == "clarification_required":
        result = {
            "success": False,
            "status": "clarification_required",
            "question": question,
            "clarification_question": plan["clarification_question"],
            "options": plan["options"],
            "resolved_database": resolved_database,
            "source_type": "mysql",
        }
        return _tool_response(
            status="clarification_required",
            confidence=0.2,
            result=result,
            reasons=["verified planner requires a missing slot before SQL can run"],
            next_tools=["resolve_entities", "resolve_business_terms"],
            **_compat_payload(result),
        )
    if plan["status"] != "ready" or not plan.get("sql"):
        result = {
            "success": False,
            "status": "blocked",
            "question": question,
            "errors": [
                *plan.get("plan_validation", {}).get("errors", []),
                *plan.get("validation", {}).get("errors", []),
            ],
            "warnings": [
                *plan.get("plan_validation", {}).get("warnings", []),
                *plan.get("validation", {}).get("warnings", []),
            ],
            "resolved_database": resolved_database,
            "source_type": "mysql",
        }
        return _tool_response(
            status="blocked",
            confidence=0.0,
            result=result,
            reasons=result["errors"] or ["verified planner did not produce safe SQL"],
            warnings=result["warnings"],
            next_tools=["resolve_business_terms", "inspect_verified_modules"],
            **_compat_payload(result),
        )
    dataframe = mysql_adapter.execute_read_query(plan["sql"], resolved_database)
    result = {
        "success": True,
        "status": "executed",
        "question": question,
        "sql": plan["sql"].strip().rstrip(";"),
        "columns": list(dataframe.columns),
        "rows": dataframe.to_dict(orient="records"),
        "row_count": len(dataframe),
        "query_plan": plan["query_plan"],
        "validation": plan["validation"],
        "resolved_database": resolved_database,
        "source_type": "mysql",
    }
    return _tool_response(
        status="executed",
        confidence=0.98,
        result=result,
        reasons=["planned, validated and executed verified read-only ERP SQL"],
        warnings=plan.get("validation", {}).get("warnings", []),
        next_tools=["format_result"],
        **_compat_payload(result),
    )


@mcp.tool()
def list_tables(
    session_id: str | None = None,
    database_path: str | None = None,
) -> dict:
    """Return compact table names and row counts for the resolved database."""
    path = _database_path(database_path, session_id)
    schema = _schema(path)
    tables = []
    with _connect_read_only(path) as connection:
        for table_name in schema:
            quoted_table = _quote_identifier(table_name)
            row_count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_table}"
            ).fetchone()[0]
            tables.append({"table": table_name, "row_count": row_count})
    return {"tables": tables, "resolved_database": str(path)}


@mcp.tool()
def get_table_profile(
    table_name: str,
    columns: list[str] | None = None,
    session_id: str | None = None,
    database_path: str | None = None,
) -> dict:
    """Return safe column profile metrics for selected real columns."""
    path = _database_path(database_path, session_id)
    selected_columns = _validated_columns(path, table_name, columns)
    quoted_table = _quote_identifier(table_name)
    profile = []

    with _connect_read_only(path) as connection:
        for column in selected_columns:
            quoted_column = _quote_identifier(column)
            row = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END),
                    COUNT(DISTINCT {quoted_column}),
                    MIN({quoted_column}),
                    MAX({quoted_column})
                FROM {quoted_table}
                """
            ).fetchone()
            profile.append(
                {
                    "column": column,
                    "null_count": row[0] or 0,
                    "distinct_count": row[1] or 0,
                    "min": row[2],
                    "max": row[3],
                }
            )

    return {
        "table": table_name,
        "profile": profile,
        "resolved_database": str(path),
    }


@mcp.tool()
def sample_rows(
    table_name: str,
    limit: int = 5,
    session_id: str | None = None,
    database_path: str | None = None,
) -> dict:
    """Return a small bounded sample from one real table."""
    path = _database_path(database_path, session_id)
    columns = _table_columns(path, table_name)
    bounded_limit = max(1, min(int(limit), _SAMPLE_LIMIT))
    quoted_table = _quote_identifier(table_name)

    with _connect_read_only(path) as connection:
        cursor = connection.execute(
            f"SELECT * FROM {quoted_table} LIMIT ?",
            (bounded_limit,),
        )
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    return {
        "table": table_name,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "resolved_database": str(path),
    }


if __name__ == "__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))
