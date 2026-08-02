import pytest

from entity_resolver import EntityCandidate, entity_resolver
from mysql_adapter import MySQLAdapter
from semantic_parser import semantic_parser


def plan(question):
    return semantic_parser.parse(question, allow_llm_fallback=False).to_dict()


def candidates(question):
    return entity_resolver.candidates_from_plan(plan(question))


def match(value, *, entity_type="project", table="projects", column=None, primary_key=1, confidence=98, anchor_score=0, match_type="Exact"):
    return {
        "entity": value,
        "entity_type": entity_type,
        "table": table,
        "column": column or f"{entity_type}_name",
        "primary_key": primary_key,
        "value": value,
        "confidence": confidence,
        "anchor_score": anchor_score,
        "match_type": match_type,
    }


@pytest.mark.parametrize(
    ("question", "expected", "predicted_type"),
    [
        ("Show pending orders for supplier ABC Engineering.", "ABC Engineering", "vendor"),
        ("Give me details on RDSS-VIZAG.", "RDSS-VIZAG", None),
        ("Show material use for Site A last month.", "Site A", "location"),
        ("Show Employee 1045 attendance.", "Employee 1045", "employee"),
        ("Find Invoice INV-1005.", "Invoice INV-1005", "invoice"),
        ("Find order PO-2026-0042.", "order PO-2026-0042", "purchase_order"),
        ("Show records for 'M/s ABC & Sons'.", "M/s ABC & Sons", None),
    ],
)
def test_only_genuine_entity_candidates_are_emitted(question, expected, predicted_type):
    result = candidates(question)

    assert [item["text"] for item in result] == [expected]
    assert result[0]["evidence"]
    if predicted_type:
        assert predicted_type in result[0]["predicted_types"]


@pytest.mark.parametrize(
    "question",
    [
        "Which vendor has the highest purchase value?",
        "Show pending orders.",
        "Show project performance.",
        "Show monthly trend.",
        "Show top 10 active projects.",
        "Show revenue for the last 30 days.",
        "Which department has the highest overtime?",
        "Show current financial year billing.",
        "Show average salary by department.",
        "Which site consumed the most material last month?",
        "What Business Areas Does This Database Cover?",
        "How many projects are listed in the ERP?",
        "What is this database about?",
        "Describe the ERP system.",
        "Show PENDING Orders for the Last 30 Days.",
    ],
)
def test_semantic_phrases_never_reach_value_resolution(question):
    assert candidates(question) == []


def test_multiple_comparison_entities_preserve_user_order():
    result = candidates("Compare Project Alpha, Project Beta and Site C in 2026.")

    assert [item["text"] for item in result] == ["Project Alpha", "Project Beta", "Site C"]
    assert result[0]["predicted_types"] == ["project"]
    assert result[2]["predicted_types"] == ["location"]


def test_candidate_extraction_uses_parser_output_not_legacy_question_extractor(monkeypatch):
    adapter = MySQLAdapter()
    parsed = plan("Show Employee 1045 attendance.")
    monkeypatch.setattr(entity_resolver, "extract", lambda _question: pytest.fail("legacy extractor called"))

    result = adapter._semantic_mentions("this text must not be reparsed", parsed)

    assert [item["text"] for item in result] == ["Employee 1045"]


def test_entity_candidate_model_is_structured():
    item = EntityCandidate("ABC", "abc", "quoted", ["quoted_text"], 0.99, 4, ["vendor"])

    assert item.to_dict()["predicted_types"] == ["vendor"]


def test_alias_and_abbreviation_matching():
    alias = entity_resolver.score_value("M/s ABC & Sons", "ABC & Sons Private Limited")
    abbreviation = entity_resolver.score_value("ABC Eng", "ABC Engineering")
    partial = entity_resolver.score_value("North Plant", "North Plant Expansion")

    assert alias[1] == "alias_match"
    assert abbreviation[0] >= 84
    assert partial[1] == "partial_match"


def test_entity_alias_dictionary_handles_business_abbreviations_and_typos():
    vendor_alias = entity_resolver.score_resolved_value(
        "pollycab",
        "POLYCAB INDIA LIMITED",
        "vendor",
    )
    project_alias = entity_resolver.score_resolved_value(
        "RDSS VIZG",
        "RDSS-VIZAG",
        "project",
    )
    material_alias = entity_resolver.score_resolved_value(
        "rabbit conductor",
        "AAAC RABBIT CONDUCTOR 55 SQMM",
        "material",
    )
    acronym_alias = entity_resolver.score_resolved_value(
        "SAIL",
        "STEEL AUTHORITY OF INDIA LIMITED",
        "vendor",
    )

    assert vendor_alias[1] == "alias_match"
    assert project_alias[1] in {"alias_match", "fuzzy_match"}
    assert material_alias[0] >= 84
    assert acronym_alias[1] == "alias_match"


def test_lookup_variants_strip_common_code_prefixes():
    assert "V12" in entity_resolver.lookup_variants("vendor code V12", "vendor")
    assert "P100" in entity_resolver.lookup_variants("code P100", "project")


def test_ambiguous_projects_require_clarification():
    decision = entity_resolver.clarification_decision([{
        "mention": "Alpha",
        "matches": [
            match("Alpha East", primary_key=1, confidence=88, match_type="Prefix"),
            match("Alpha West", primary_key=2, confidence=88, match_type="Prefix"),
        ],
        "predicted_types": ["project"],
    }])

    assert decision.required is True
    assert decision.candidates[0]["mention"] == "Alpha"
    assert len(decision.candidates[0]["options"]) == 2


def test_ambiguous_vendors_require_clarification():
    decision = entity_resolver.clarification_decision([{
        "mention": "ABC",
        "matches": [
            match("ABC Engineering", entity_type="vendor", table="vendors", primary_key=10, confidence=88, match_type="Prefix"),
            match("ABC Electricals", entity_type="vendor", table="vendors", primary_key=11, confidence=88, match_type="Prefix"),
        ],
        "predicted_types": ["vendor"],
    }])

    assert decision.required is True


def test_same_entity_name_with_distinct_records_requires_clarification():
    decision = entity_resolver.clarification_decision([{
        "mention": "Central Site",
        "matches": [
            match("Central Site", primary_key=101),
            match("Central Site", primary_key=202),
        ],
        "predicted_types": ["project"],
    }])

    assert decision.required is True


def test_entity_type_evidence_resolves_same_name_without_clarification():
    decision = entity_resolver.clarification_decision([{
        "mention": "Alpha",
        "matches": [
            match("Alpha", entity_type="project", table="projects", primary_key=1),
            match("Alpha", entity_type="vendor", table="vendors", primary_key=2),
        ],
        "predicted_types": ["project"],
    }])

    assert decision.required is False


def test_clear_exact_match_does_not_clarify_against_partial_match():
    decision = entity_resolver.clarification_decision([{
        "mention": "ABC Engineering",
        "matches": [
            match("ABC Engineering", entity_type="vendor", table="vendors", primary_key=1, confidence=98),
            match("ABC Engineering Services", entity_type="vendor", table="vendors", primary_key=2, confidence=88, match_type="Prefix"),
        ],
        "predicted_types": ["vendor"],
    }])

    assert decision.required is False


def test_unresolved_candidate_does_not_trigger_clarification():
    parsed = plan("Show details for Project UNKNOWN-42.")
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [{"mention": "Project UNKNOWN-42", "matches": [], "predicted_types": ["project"]}],
        semantic_plan=parsed,
    )

    assert result["unresolved"]
    assert result["needs_clarification"] is False
    assert result["clarification"]["required"] is False


def test_missing_relationship_does_not_trigger_entity_clarification():
    parsed = plan("Show Vendor ABC invoices for Project Alpha.")
    mention_results = [
        {"mention": "Vendor ABC", "matches": [match("Vendor ABC", entity_type="vendor", table="vendors")], "predicted_types": ["vendor"]},
        {"mention": "Project Alpha", "matches": [match("Project Alpha", entity_type="project", table="projects", primary_key=2)], "predicted_types": ["project"]},
    ]
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        mention_results,
        relationships=[{
            "from_entity": "Vendor ABC",
            "to_entity": "Project Alpha",
            "relationship_type": "no_reliable_path",
            "confidence": 0,
            "validated": False,
        }],
        semantic_plan=parsed,
    )

    assert result["relationship_gaps"]
    assert result["needs_clarification"] is False


def test_multiple_clear_comparison_entities_do_not_clarify():
    parsed = plan("Compare Project Alpha and Project Beta.")
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [
            {"mention": "Project Alpha", "matches": [match("Project Alpha", primary_key=1)], "predicted_types": ["project"]},
            {"mention": "Project Beta", "matches": [match("Project Beta", primary_key=2)], "predicted_types": ["project"]},
        ],
        semantic_plan=parsed,
    )

    assert result["needs_clarification"] is False
    assert result["comparisons"][0]["mentions"] == ["Project Alpha", "Project Beta"]


def test_unknown_metric_mapping_never_causes_entity_clarification():
    parsed = plan("Show quantum efficiency by division.")
    result = entity_resolver.build_multi_result(
        parsed["original_question"],
        [],
        semantic_plan=parsed,
    )

    assert result["needs_clarification"] is False
    assert result["clarification"]["candidates"] == []


def test_project_like_codes_are_predicted_as_projects_from_context():
    detail_plan = plan(
        "Give me details about RDSS-VIZAG project including value, client, dates, state, and anything useful you can safely find."
    )
    comparison_plan = plan("Compare RDSS-VIZAG and RDSS-UGCABLE billing and tell me which is higher.")

    detail_candidates = entity_resolver.candidates_from_plan(detail_plan)
    comparison_candidates = entity_resolver.candidates_from_plan(comparison_plan)

    assert detail_candidates[0]["text"] == "RDSS-VIZAG"
    assert detail_candidates[0]["predicted_types"][0] == "project"
    assert [item["predicted_types"][0] for item in comparison_candidates] == ["project", "project"]


def test_same_record_matched_by_multiple_display_columns_does_not_clarify():
    decision = entity_resolver.clarification_decision([{
        "mention": "RDSS-VIZAG",
        "matches": [
            match("RDSS-VIZAG", entity_type="project", table="project_mst", column="project_name", primary_key=2, confidence=99),
            match("RDSS-VIZAG", entity_type="project", table="project_mst", column="project_short_name", primary_key=2, confidence=99),
        ],
        "predicted_types": ["project"],
        "essential": True,
    }])

    assert decision.required is False
