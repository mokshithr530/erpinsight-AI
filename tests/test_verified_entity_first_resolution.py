from chat_pipeline import ChatPipeline
from clarification_planner import ordered_clarification_planner
from entity_resolver import entity_resolver
from mysql_adapter import MySQLAdapter


def _match(
    value,
    *,
    entity_type="project",
    table=None,
    column=None,
    primary_key=1,
    confidence=98,
    match_type="exact_name",
):
    table = table or f"{entity_type}_mst"
    column = column or f"{entity_type}_name"
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
        "anchor_score": 100,
        "match_type": match_type,
    }


def _resolver(monkeypatch, lookup):
    adapter = MySQLAdapter()
    monkeypatch.setattr(entity_resolver, "get_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entity_resolver, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "inspect_schema", lambda _db: {
        "project_mst": {"columns": [{"name": "project_id", "primary_key": True}, {"name": "project_name"}]},
        "vendor_mst": {"columns": [{"name": "vendor_id", "primary_key": True}, {"name": "vendor_name"}]},
        "material_mst": {"columns": [{"name": "material_id", "primary_key": True}, {"name": "material_name"}]},
    })
    monkeypatch.setattr(adapter, "_approx_row_counts", lambda _db: {
        "project_mst": 10,
        "vendor_mst": 20,
        "material_mst": 30,
    })
    monkeypatch.setattr(adapter, "business_catalog", lambda _db: {"catalog_version": "test"})
    monkeypatch.setattr(adapter, "database_fingerprint", lambda _db: "db-fp")
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda _db: "schema-fp")
    monkeypatch.setattr(adapter, "_validate_entity_relationships", lambda _schema, _entities: [])
    monkeypatch.setattr(adapter, "_entity_candidates", lambda **kwargs: [{
        "table": f"{kwargs['entity_type']}_mst",
        "column": f"{kwargs['entity_type']}_name",
        "entity_type": kwargs["entity_type"],
        "score": 120,
    }])
    monkeypatch.setattr(adapter, "_lookup_entity_value", lookup)
    return adapter


def _plan(question, mentions):
    return {
        "original_question": question,
        "operation": "lookup",
        "candidate_entity_mentions": mentions,
        "metrics": [],
        "dimensions": [],
        "filters": [{"raw_value": mention, "essential": True} for mention in mentions],
    }


def test_exact_project_vendor_and_material_resolution(monkeypatch):
    def lookup(_db, text, candidates):
        entity_type = candidates[0]["entity_type"]
        values = {
            "project": "RDSS-VIZAG",
            "vendor": "POLYCAB INDIA LIMITED",
            "material": "AAAC RABBIT CONDUCTOR 55 SQMM",
        }
        return [_match(values[entity_type], entity_type=entity_type, primary_key=11)]

    adapter = _resolver(monkeypatch, lookup)

    assert adapter.resolve_verified_entities("Show project RDSS-VIZAG", "erp", _plan("", ["RDSS-VIZAG"]), expected_entity_types=["project"])["entities"][0]["entity_type"] == "project"
    assert adapter.resolve_verified_entities("Show vendor POLYCAB", "erp", _plan("", ["POLYCAB"]), expected_entity_types=["vendor"])["entities"][0]["entity_type"] == "vendor"
    assert adapter.resolve_verified_entities("Show inventory for AAAC RABBIT", "erp", _plan("", ["AAAC RABBIT"]), expected_entity_types=["material"])["entities"][0]["entity_type"] == "material"


def test_alias_and_partial_matches_are_structured(monkeypatch):
    def lookup(_db, _text, candidates):
        return [_match("RDSS-VIZAG", entity_type=candidates[0]["entity_type"], confidence=92, match_type="alias_match")]

    adapter = _resolver(monkeypatch, lookup)
    result = adapter.resolve_verified_entities(
        "Compare RDSS",
        "erp",
        _plan("Compare RDSS", ["RDSS"]),
        expected_entity_types=["project"],
    )

    assert result["entities"][0]["matched_value"] == "RDSS-VIZAG"
    assert result["entities"][0]["match_type"] == "alias_match"
    assert result["needs_clarification"] is False


def test_multiple_same_type_candidates_ask_candidate_choice(monkeypatch):
    def lookup(_db, _text, candidates):
        entity_type = candidates[0]["entity_type"]
        return [
            _match("RDSS-VIZAG", entity_type=entity_type, primary_key=1, confidence=95, match_type="prefix_match"),
            _match("RDSS-UGCABLE", entity_type=entity_type, primary_key=2, confidence=93, match_type="prefix_match"),
        ]

    adapter = _resolver(monkeypatch, lookup)
    result = adapter.resolve_verified_entities(
        "Compare RDSS and APDRP",
        "erp",
        _plan("Compare RDSS and APDRP", ["RDSS"]),
        expected_entity_types=["project"],
    )

    assert result["needs_clarification"] is True
    assert result["ambiguities"][0]["reason"] == "multiple_verified_candidates"
    assert [item["matched_value"] for item in result["ambiguities"][0]["options"][:2]] == ["RDSS-VIZAG", "RDSS-UGCABLE"]


def test_entity_type_ambiguity_is_returned_before_metric_clarification(monkeypatch):
    def lookup(_db, text, candidates):
        return [_match(text, entity_type=candidates[0]["entity_type"], confidence=96)]

    adapter = _resolver(monkeypatch, lookup)
    entity_resolution = adapter.resolve_verified_entities(
        "Show purchase orders for ABC.",
        "erp",
        _plan("Show purchase orders for ABC.", ["ABC"]),
        expected_entity_types=["project", "vendor"],
    )
    clarification = ordered_clarification_planner.plan(
        question="Show purchase orders for ABC.",
        semantic_plan=_plan("Show purchase orders for ABC.", ["ABC"]),
        entity_resolution=entity_resolution,
        semantic_binding={},
    )

    assert entity_resolution["ambiguities"][0]["reason"] == "entity_type_ambiguity"
    assert clarification.next_slot == "entity_type"
    assert "project" in clarification.question
    assert "vendor" in clarification.question


def test_unknown_entity_safe_fails(monkeypatch):
    adapter = _resolver(monkeypatch, lambda _db, _text, _candidates: [])

    result = adapter.resolve_verified_entities(
        "Show project UNKNOWN-42",
        "erp",
        _plan("Show project UNKNOWN-42", ["UNKNOWN-42"]),
        expected_entity_types=["project"],
    )

    assert result["entities"] == []
    assert result["unresolved"][0]["mention"] == "UNKNOWN-42"
    assert result["needs_clarification"] is True


def test_multiple_entities_preserve_order(monkeypatch):
    def lookup(_db, text, candidates):
        return [_match(text, entity_type=candidates[0]["entity_type"], primary_key=len(text))]

    adapter = _resolver(monkeypatch, lookup)
    result = adapter.resolve_verified_entities(
        "Compare RDSS-VIZAG and RDSS-UGCABLE",
        "erp",
        _plan("Compare RDSS-VIZAG and RDSS-UGCABLE", ["RDSS-VIZAG", "RDSS-UGCABLE"]),
        expected_entity_types=["project"],
    )

    assert [item["matched_value"] for item in result["entities"]] == ["RDSS-VIZAG", "RDSS-UGCABLE"]


def test_context_aware_resolution_searches_only_expected_type(monkeypatch):
    seen = []

    def lookup(_db, text, candidates):
        seen.append(candidates[0]["entity_type"])
        return [_match(text, entity_type=candidates[0]["entity_type"])]

    adapter = _resolver(monkeypatch, lookup)
    result = adapter.resolve_verified_entities(
        "RDSS-VIZAG",
        "erp",
        _plan("RDSS-VIZAG", ["RDSS-VIZAG"]),
        expected_entity_types=["project"],
    )

    assert seen == ["project"]
    assert result["entities"][0]["entity_type"] == "project"


def test_exact_master_match_beats_embedded_document_number_alias():
    adapter = MySQLAdapter()

    matches = [
        _match(
            "RDSS-VIZAG",
            entity_type="project",
            table="project_mst",
            column="project_name",
            confidence=99,
            match_type="exact_match",
        ),
        _match(
            "WO/SC-1001/RDSS VIZAG/0001",
            entity_type="material",
            table="store_material_usage_quantity",
            column="workorder_no",
            confidence=94,
            match_type="alias_match",
        ),
    ]

    assert adapter._verified_mention_is_ambiguous(matches) is False


def test_duplicate_display_matches_collapse_before_ambiguity():
    adapter = MySQLAdapter()
    matches = [
        _match(
            "STEEL AUTHORITY OF INDIA LIMITED",
            entity_type="vendor",
            primary_key=1,
            confidence=99,
            match_type="exact_match",
        ),
        _match(
            "STEEL AUTHORITY OF INDIA LIMITED",
            entity_type="vendor",
            primary_key=2,
            confidence=98,
            match_type="exact_match",
        ),
    ]

    collapsed = adapter._collapse_verified_display_matches(matches)

    assert len(collapsed) == 1
    assert adapter._verified_mention_is_ambiguous(collapsed) is False


def test_exact_vendor_name_beats_extended_alias_candidate():
    adapter = MySQLAdapter()
    matches = [
        _match(
            "STEEL AUTHORITY OF INDIA LIMITED",
            entity_type="vendor",
            primary_key=1,
            confidence=99,
            match_type="exact_match",
        ),
        _match(
            "STEEL AUTHORITY OF INDIA LIMITED -NKLIS",
            entity_type="vendor",
            primary_key=2,
            confidence=98,
            match_type="alias_match",
        ),
    ]

    assert adapter._verified_mention_is_ambiguous(matches) is False


def test_low_confidence_match_is_rejected(monkeypatch):
    def lookup(_db, text, candidates):
        return [_match(text, entity_type=candidates[0]["entity_type"], confidence=55, match_type="fuzzy_match")]

    adapter = _resolver(monkeypatch, lookup)
    result = adapter.resolve_verified_entities(
        "Show vendor uncertain",
        "erp",
        _plan("Show vendor uncertain", ["uncertain"]),
        expected_entity_types=["vendor"],
    )

    assert result["entities"] == []
    assert result["unresolved"][0]["reason"] == "no_verified_entity_match"


def test_entity_type_followup_merges_into_original_question():
    pipeline = ChatPipeline()

    result = pipeline.merge_clarification_followup(
        "project",
        {
            "active": True,
            "original_question": "Show purchase orders for ABC.",
            "missing_slots": ["entity_type"],
            "filled_slots": {},
            "expected_entity_types": ["project", "vendor"],
        },
    )

    assert result["handled"] is True
    assert result["question"] == "Show purchase orders for project ABC."


def test_module_words_narrow_entity_resolution_types_before_lookup():
    pipeline = ChatPipeline()

    assert pipeline._expected_entity_types_from_question("Show billing for ABC") == ["project", "client"]
    assert pipeline._expected_entity_types_from_question("Show purchase orders for ABC") == ["project", "vendor"]
    assert pipeline._expected_entity_types_from_question("Show inventory for ABC") == ["material", "project", "store"]
