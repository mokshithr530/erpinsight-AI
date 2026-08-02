import sqlite3

import pytest

import mcp_server
from entity_resolver import ResolvedEntity, entity_resolver
from mysql_adapter import MySQLAdapter
from semantic_parser import semantic_parser


def _plan(question):
    return semantic_parser.parse(question, allow_llm_fallback=False).to_dict()


def _match(
    value,
    *,
    entity_type="project",
    table="project_master",
    column="project_name",
    primary_key=1,
    confidence=98,
    match_type="exact_match",
):
    return {
        "entity": value,
        "entity_type": entity_type,
        "table": table,
        "column": column,
        "primary_key_column": f"{entity_type}_id",
        "primary_key": primary_key,
        "primary_key_value": primary_key,
        "value": value,
        "matched_value": value,
        "confidence": confidence,
        "match_type": match_type,
    }


def _entity_database(tmp_path):
    database = tmp_path / "phase4_entities.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE project_master (
                project_id INTEGER PRIMARY KEY,
                project_name TEXT NOT NULL,
                project_code TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO project_master(project_name, project_code) VALUES (?, ?)",
            [
                ("Project Alpha", "ALPHA-01"),
                ("Project Beta", "BETA-02"),
                ("Project Gamma", "GAMMA-03"),
            ],
        )
        connection.execute(
            """
            CREATE TABLE vendor_master (
                vendor_id INTEGER PRIMARY KEY,
                vendor_name TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO vendor_master(vendor_name) VALUES (?)",
            [("ABC Engineering",), ("ABC Electricals",)],
        )
        connection.execute(
            "CREATE TABLE employee_master (employee_id INTEGER PRIMARY KEY, employee_code TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO employee_master(employee_code) VALUES (?)",
            [("1045",), ("EMP-2048",)],
        )
        connection.execute(
            "CREATE TABLE invoice_master (invoice_id INTEGER PRIMARY KEY, invoice_number TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO invoice_master(invoice_number) VALUES (?)",
            [("INV-1005",), ("INV-2026-88",)],
        )
        connection.execute(
            "CREATE TABLE site_master (site_id INTEGER PRIMARY KEY, site_code TEXT, site_name TEXT)"
        )
        connection.executemany(
            "INSERT INTO site_master(site_code, site_name) VALUES (?, ?)",
            [("A", "East Campus"), ("NP-01", "North Plant")],
        )
    return database


def test_resolver_consumes_only_parser_candidate_mentions():
    parsed = {
        "original_question": "Compare Project Alpha with highest purchase value in 2025",
        "candidate_entity_mentions": ["Project Alpha"],
        "metrics": ["purchase_value"],
        "date_constraints": [{"type": "year", "value": 2025}],
    }

    assert [item["text"] for item in entity_resolver.candidates_from_plan(parsed)] == ["Project Alpha"]


def test_resolver_does_not_reextract_raw_question_when_parser_has_no_candidates():
    parsed = {
        "original_question": "Show RDSS-VIZAG and highest purchase value",
        "candidate_entity_mentions": [],
        "metrics": ["purchase_value"],
    }

    assert entity_resolver.candidates_from_plan(parsed) == []


@pytest.mark.parametrize(
    ("mention", "stored_value", "expected_type"),
    [
        ("ABC", "ABC", "exact_match"),
        ("North Plant", "North Plant", "exact_match"),
        ("abc", "ABC", "case_insensitive_match"),
        ("north plant", "North Plant", "case_insensitive_match"),
        (" ABC ", "ABC", "trimmed_match"),
        (" North Plant ", "North Plant", "trimmed_match"),
        ("M/s ABC & Sons", "ABC & Sons Private Limited", "alias_match"),
        ("ABC Eng", "ABC Engineering", "alias_match"),
        ("North Plant", "North Plant Expansion", "partial_match"),
        ("INV-10", "INV-1005", "partial_match"),
        ("Alfa Engineering", "Alpha Engineering", "fuzzy_match"),
        ("Centrl Warehouse", "Central Warehouse", "fuzzy_match"),
    ],
)
def test_match_types_are_distinct_and_deterministic(mention, stored_value, expected_type):
    assert entity_resolver.score_value(mention, stored_value)[1] == expected_type


@pytest.mark.parametrize(
    ("question", "expected_order"),
    [
        ("Compare Project Alpha and Project Beta.", ["Project Alpha", "Project Beta"]),
        ("Compare Project Gamma and Project Alpha.", ["Project Gamma", "Project Alpha"]),
    ],
)
def test_multiple_same_type_entities_resolve_from_database_in_mention_order(
    tmp_path,
    question,
    expected_order,
):
    result = mcp_server.resolve_entities(question, database_path=str(_entity_database(tmp_path)))

    assert [item["original_text"] for item in result["entities"]] == expected_order
    assert [item["entity_type"] for item in result["entities"]] == ["project", "project"]
    assert all(item["primary_key_column"] == "project_id" for item in result["entities"])
    assert all(item["primary_key_value"] is not None for item in result["entities"])
    assert result["comparisons"][0]["mentions"] == expected_order
    assert result["needs_clarification"] is False
    assert result["searched_tables_first"][0] == "project_master"


@pytest.mark.parametrize(
    ("question", "original_text", "entity_type", "matched_value"),
    [
        ("Show purchases from supplier ABC Engineering.", "ABC Engineering", "vendor", "ABC Engineering"),
        ("Show details for vendor ABC Electricals.", "ABC Electricals", "vendor", "ABC Electricals"),
        ("Show attendance for Employee 1045.", "Employee 1045", "employee", "1045"),
        ("Show details for employee EMP-2048.", "employee EMP-2048", "employee", "EMP-2048"),
        ("Find Invoice INV-1005.", "Invoice INV-1005", "invoice", "INV-1005"),
        ("Show invoice INV-2026-88 details.", "invoice INV-2026-88", "invoice", "INV-2026-88"),
        ("Show Site A details.", "Site A", "location", "A"),
        ("Show activity for site North Plant.", "site North Plant", "location", "North Plant"),
    ],
)
def test_two_real_value_lookups_per_entity_topic(
    tmp_path,
    question,
    original_text,
    entity_type,
    matched_value,
):
    result = mcp_server.resolve_entities(question, database_path=str(_entity_database(tmp_path)))

    assert result["needs_clarification"] is False
    assert len(result["entities"]) == 1
    assert result["entities"][0]["original_text"] == original_text
    assert result["entities"][0]["entity_type"] == entity_type
    assert result["entities"][0]["matched_value"] == matched_value


@pytest.mark.parametrize(
    "question",
    [
        "Compare Project Alpha and Project Beta in 2025.",
        "Show Project Alpha activity during the last 30 days.",
    ],
)
def test_dates_never_reach_entity_resolution(question):
    candidates = entity_resolver.candidates_from_plan(_plan(question))

    assert all("2025" not in item["text"] and "30 days" not in item["text"].lower() for item in candidates)


@pytest.mark.parametrize(
    "question",
    [
        "Show pending orders.",
        "List active employees.",
    ],
)
def test_status_words_never_reach_entity_resolution(question):
    assert entity_resolver.candidates_from_plan(_plan(question)) == []


@pytest.mark.parametrize(
    "question",
    [
        "Which vendor has the highest purchase value?",
        "Show the top 10 monthly revenue records.",
    ],
)
def test_ranking_phrases_never_reach_entity_resolution(question):
    assert entity_resolver.candidates_from_plan(_plan(question)) == []


@pytest.mark.parametrize("mention", ["Project UNKNOWN-42", "Vendor MISSING-99"])
def test_unresolved_essential_entity_requests_clarification(mention):
    parsed = {
        "original_question": f"Show details for {mention}.",
        "operation": "lookup",
        "candidate_entity_mentions": [mention],
        "filters": [{"raw_value": mention, "essential": True}],
    }
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [{
            "mention": mention,
            "matches": [],
            "predicted_types": [],
            "essential": entity_resolver.mention_is_essential(mention, parsed),
        }],
        semantic_plan=parsed,
    )

    assert result["unresolved"][0]["original_text"] == mention
    assert result["unresolved"][0]["essential"] is True
    assert result["needs_clarification"] is True
    assert result["clarification"]["candidates"][0]["reason"] == "required_filter_value_not_found"


@pytest.mark.parametrize("mention", ["Regional context", "Executive summary"])
def test_unresolved_nonessential_phrase_does_not_request_clarification(mention):
    parsed = {
        "original_question": "Describe this database.",
        "operation": "metadata",
        "candidate_entity_mentions": [mention],
        "filters": [],
    }
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [{"mention": mention, "matches": [], "predicted_types": [], "essential": False}],
        semantic_plan=parsed,
    )

    assert result["unresolved"][0]["essential"] is False
    assert result["needs_clarification"] is False


@pytest.mark.parametrize(
    ("mention", "entity_type"),
    [("ABC", "vendor"), ("Central", "project")],
)
def test_ambiguous_essential_entities_return_ranked_candidates(mention, entity_type):
    parsed = {
        "original_question": f"Show {entity_type} {mention}.",
        "operation": "lookup",
        "candidate_entity_mentions": [mention],
        "filters": [{"raw_value": mention, "essential": True}],
    }
    matches = [
        _match(
            f"{mention} East",
            entity_type=entity_type,
            table=f"{entity_type}_master",
            column=f"{entity_type}_name",
            primary_key=2,
            confidence=88,
            match_type="partial_match",
        ),
        _match(
            f"{mention} West",
            entity_type=entity_type,
            table=f"{entity_type}_master",
            column=f"{entity_type}_name",
            primary_key=1,
            confidence=89,
            match_type="partial_match",
        ),
    ]
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [{
            "mention": mention,
            "matches": matches,
            "predicted_types": [entity_type],
            "essential": True,
        }],
        semantic_plan=parsed,
    )

    assert result["needs_clarification"] is True
    assert result["ambiguities"][0]["options"][0]["confidence"] == 89
    assert len(result["ambiguities"][0]["options"]) == 2


def test_database_backed_ambiguity_returns_ranked_vendor_candidates(tmp_path):
    result = mcp_server.resolve_entities(
        "Show vendor ABC.",
        database_path=str(_entity_database(tmp_path)),
    )

    assert result["needs_clarification"] is True
    assert result["ambiguities"][0]["mention"] == "ABC"
    assert [item["matched_value"] for item in result["ambiguities"][0]["options"]] == [
        "ABC Engineering",
        "ABC Electricals",
    ]


def test_database_backed_missing_required_project_is_explicit(tmp_path):
    result = mcp_server.resolve_entities(
        "Show details for Project UNKNOWN-42.",
        database_path=str(_entity_database(tmp_path)),
    )

    assert result["entities"] == []
    assert result["unresolved"][0]["original_text"] == "Project UNKNOWN-42"
    assert result["unresolved"][0]["essential"] is True
    assert result["needs_clarification"] is True


def test_resolved_entity_contract_has_only_required_fields():
    entity = ResolvedEntity(
        original_text="Project Alpha",
        normalized_text="project alpha",
        entity_type="project",
        table="project_master",
        column="project_name",
        primary_key_column="project_id",
        primary_key_value=1,
        matched_value="Project Alpha",
        confidence=100,
        match_type="exact_match",
    )

    assert set(entity.to_dict()) == {
        "original_text",
        "normalized_text",
        "entity_type",
        "table",
        "column",
        "primary_key_column",
        "primary_key_value",
        "matched_value",
        "confidence",
        "match_type",
    }


def _mysql_resolver_adapter(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "project_master": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
        "project_archive": {
            "columns": [
                {"column_name": "project_id", "data_type": "int", "primary_key": True},
                {"column_name": "project_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda _database: schema)
    monkeypatch.setattr(adapter, "_approx_row_counts", lambda _database: {
        "project_master": 10,
        "project_archive": 1,
    })
    monkeypatch.setattr(adapter, "business_catalog", lambda _database: {
        "entities": {"project": {}},
        "catalog_version": "phase4-test",
    })
    monkeypatch.setattr(adapter, "database_fingerprint", lambda _database: "phase4-db")
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda _database: "phase4-schema")
    monkeypatch.setattr(entity_resolver, "get_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entity_resolver, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        adapter,
        "_entity_retrieval_context",
        lambda *_args, **_kwargs: {
            "tables": ["project_master"],
            "columns": {"project_master": ["project_id", "project_name"]},
        },
    )
    return adapter


def test_mysql_resolution_searches_retrieved_schema_before_global_fallback(monkeypatch):
    adapter = _mysql_resolver_adapter(monkeypatch)
    searched = []

    def lookup(_database, mention, candidates):
        searched.append([item["table"] for item in candidates])
        return [_match(mention)] if candidates else []

    monkeypatch.setattr(adapter, "_lookup_entity_value", lookup)
    parsed = _plan("Show details for Project Alpha.")

    result = adapter.resolve_entities("Show details for Project Alpha.", "demo", semantic_plan=parsed)

    assert len(searched) == 1
    assert set(searched[0]) == {"project_master"}
    assert result["entities"][0]["table"] == "project_master"
    assert result["searched_tables_first"] == ["project_master"]


def test_mysql_resolution_uses_bounded_global_fallback_after_relevant_miss(monkeypatch):
    adapter = _mysql_resolver_adapter(monkeypatch)
    searched = []

    def lookup(_database, mention, candidates):
        tables = [item["table"] for item in candidates]
        searched.append(tables)
        if "project_archive" in tables:
            return [_match(mention, table="project_archive")]
        return []

    monkeypatch.setattr(adapter, "_lookup_entity_value", lookup)
    parsed = _plan("Show details for Project Alpha.")

    result = adapter.resolve_entities("Show details for Project Alpha.", "demo", semantic_plan=parsed)

    assert set(searched[0]) == {"project_master"}
    assert "project_archive" in searched[1]
    assert result["entities"][0]["table"] == "project_archive"


def test_mysql_resolution_does_not_leave_verified_entity_scope(monkeypatch):
    adapter = _mysql_resolver_adapter(monkeypatch)
    searched = []

    def lookup(_database, _mention, candidates):
        searched.append([
            (item["table"], item["column"])
            for item in candidates
        ])
        return []

    monkeypatch.setattr(adapter, "_lookup_entity_value", lookup)
    parsed = _plan("Show details for Project Alpha.")
    binding = {
        "authoritative": True,
        "entity_locations": {
            "project": [
                {
                    "table": "project_master",
                    "column": "project_name",
                    "role": "display",
                    "confidence_score": 98,
                },
                {
                    "table": "project_master",
                    "column": "project_id",
                    "role": "identifier",
                    "confidence_score": 98,
                },
            ]
        },
    }

    result = adapter.resolve_entities(
        "Show details for Project Alpha.",
        "demo",
        semantic_plan=parsed,
        semantic_binding=binding,
    )

    assert len(searched) == 1
    assert set(searched[0]) == {
        ("project_master", "project_name"),
        ("project_master", "project_id"),
    }
    assert result["entities"] == []
    assert all(table != "project_archive" for table, _ in searched[0])
