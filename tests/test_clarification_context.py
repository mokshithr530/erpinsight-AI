from pathlib import Path

from fastapi.testclient import TestClient

import clarification_context as context_module
import main as main_module
from chat_pipeline import ChatPipeline
from clarification_context import (
    ClarificationContext,
    clear_clarification_context,
    get_clarification_context,
    save_clarification_context,
)


def test_clarification_context_save_get_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module, "CLARIFICATION_DIR", tmp_path)
    save_clarification_context(
        "session-a",
        ClarificationContext(
            active=True,
            original_question="Show purchase orders for ABC.",
            intent="data_query",
            module="purchase_orders",
            missing_slots=["entity"],
            expected_entity_types=["project", "vendor"],
        ),
    )

    stored = get_clarification_context("session-a")

    assert stored["active"] is True
    assert stored["original_question"] == "Show purchase orders for ABC."
    assert stored["missing_slots"] == ["entity"]

    clear_clarification_context("session-a")

    assert get_clarification_context("session-a") is None


def test_single_turn_followup_merges_entity():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show purchase orders for ABC.",
        "missing_slots": ["entity"],
        "filled_slots": {},
        "expected_entity_types": ["project", "vendor"],
    }

    merged = pipeline.merge_clarification_followup("RDSS-VIZAG", context)

    assert merged["handled"] is True
    assert merged["question"] == "Show purchase orders for RDSS-VIZAG."


def test_multi_turn_followup_preserves_entity_pair_and_metric():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Compare purchase orders.",
        "missing_slots": ["entity_1", "entity_2", "metric"],
        "filled_slots": {},
        "entity_options": [],
        "metric_options": ["PO total value", "PO count"],
    }

    first = pipeline.merge_clarification_followup(
        "RDSS-VIZAG and RDSS-UGCABLE",
        context,
    )
    assert first["handled"] is True
    assert "RDSS-VIZAG and RDSS-UGCABLE" in first["question"]

    second_context = {
        **context,
        "filled_slots": first["filled_slots"],
        "missing_slots": ["metric"],
    }
    second = pipeline.merge_clarification_followup("total value", second_context)

    assert second["handled"] is True
    assert "purchase order total value" in second["question"].lower()


def test_letter_followup_selects_entity_option():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show project details.",
        "missing_slots": ["entity"],
        "current_expected_slot": "entity",
        "entity_options": ["RDSS-VIZAG", "RDSS-UGCABLE"],
        "filled_slots": {},
    }

    result = pipeline.merge_clarification_followup("A", context)

    assert result["handled"] is True
    assert result["filled_slots"]["entity"] == "RDSS-VIZAG"
    assert result["question"] == "Show project details for RDSS-VIZAG."


def test_letter_followup_selects_status_option():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show active projects.",
        "missing_slots": ["project_status"],
        "current_expected_slot": "project_status",
        "options": ["Project status is active", "Project end date has not passed"],
        "filled_slots": {},
    }

    result = pipeline.merge_clarification_followup("A", context)

    assert result["handled"] is True
    assert result["filled_slots"]["status"] == "Project status is active"
    assert "Project status is active" in result["question"]


def test_metric_letter_uses_metric_options_even_when_entity_options_exist():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show purchase orders for ABC.",
        "missing_slots": ["metric"],
        "current_expected_slot": "metric",
        "entity_options": ["RDSS-VIZAG", "RDSS-UGCABLE"],
        "metric_options": ["PO total value", "PO count"],
        "filled_slots": {"entity": "RDSS-VIZAG", "entity_type": "project"},
    }

    result = pipeline.merge_clarification_followup("B", context)

    assert result["handled"] is True
    assert result["filled_slots"]["entity"] == "RDSS-VIZAG"
    assert result["filled_slots"]["metric"] == "PO count"
    assert "purchase order count" in result["question"].lower()


def test_material_metric_followup_does_not_become_entity():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show material movement.",
        "missing_slots": ["entity", "metric"],
        "metric_options": ["Material requested quantity", "Material issued quantity"],
        "filled_slots": {},
    }

    result = pipeline.merge_clarification_followup("Material issued quantity", context)

    assert result["handled"] is True
    assert result["filled_slots"]["metric"] == "Material issued quantity"
    assert "entity" not in result["filled_slots"]


def test_material_entity_followup_keeps_metric_separate():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show material movement.",
        "missing_slots": ["entity"],
        "current_expected_slot": "entity",
        "filled_slots": {"metric": "Material issued quantity"},
        "expected_entity_types": ["project", "material", "store"],
    }

    result = pipeline.merge_clarification_followup("RDSS-VIZAG", context)

    assert result["handled"] is True
    assert result["filled_slots"]["entity"] == "RDSS-VIZAG"
    assert result["question"] == "Show material issued quantity by project for project RDSS-VIZAG."


def test_vendor_comparison_metric_followup_preserves_vendor_context():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Compare vendors.",
        "missing_slots": ["metric"],
        "current_expected_slot": "metric",
        "metric_options": ["PO total value", "PO count"],
        "filled_slots": {
            "entity_1": "POLYCAB INDIA LIMITED",
            "entity_2": "STEEL AUTHORITY OF INDIA LIMITED",
        },
    }

    result = pipeline.merge_clarification_followup("PO total value", context)

    assert result["handled"] is True
    assert result["question"] == (
        "Compare vendors by purchase value for POLYCAB INDIA LIMITED "
        "and STEEL AUTHORITY OF INDIA LIMITED."
    )


def test_candidate_choice_updates_second_entity_in_pair():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Compare vendors.",
        "missing_slots": ["entity"],
        "current_expected_slot": "entity",
        "entity_options": [
            "STEEL AUTHORITY OF INDIA LIMITED",
            "STEEL AUTHORITY OF INDIA LIMITED -NKLIS",
        ],
        "filled_slots": {
            "entity_1": "POLYCAB INDIA LIMITED",
            "entity_2": "STEEL AUTHORITY OF INDIA LIMITED",
        },
    }

    result = pipeline.merge_clarification_followup("B", context)

    assert result["handled"] is True
    assert result["filled_slots"]["entity_2"] == "STEEL AUTHORITY OF INDIA LIMITED -NKLIS"


def test_threshold_followup_is_preserved_in_merged_question():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show low stock items.",
        "missing_slots": ["threshold"],
        "current_expected_slot": "threshold",
        "filled_slots": {},
    }

    result = pipeline.merge_clarification_followup("below 10", context)

    assert result["handled"] is True
    assert result["filled_slots"]["threshold"] == "10"
    assert "below 10" in result["question"]


def test_unrelated_question_is_detected():
    pipeline = ChatPipeline()
    context = {
        "active": True,
        "original_question": "Show purchase orders for ABC.",
        "missing_slots": ["entity"],
        "filled_slots": {},
    }

    result = pipeline.merge_clarification_followup(
        "What is the total billing?",
        context,
    )

    assert result["handled"] is False
    assert result["unrelated"] is True


def test_expired_context_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module, "CLARIFICATION_DIR", tmp_path)
    save_clarification_context(
        "session-a",
        {
            "active": True,
            "original_question": "Show project details.",
            "missing_slots": ["entity"],
            "turns_remaining": 0,
        },
    )

    assert get_clarification_context("session-a") is None
    assert not Path(tmp_path / "session-a.json").exists()


def test_route_uses_pending_context_and_clears_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module, "CLARIFICATION_DIR", tmp_path)
    monkeypatch.setattr(main_module, "get_session_source", lambda session_id: {
        "source_type": "mysql",
        "database": "erp",
        "schema_fingerprint": "fp",
    })
    seen = []

    def fake_ask_mysql(question, database, identity):
        seen.append(question)
        return {
            "success": True,
            "answer": "answered",
            "sql": "SELECT 1",
            "result": {"records": [{"x": 1}]},
            "confidence": {"confidence_score": 90, "confidence_level": "high", "confidence_reasons": [], "limitations": []},
        }

    monkeypatch.setattr(main_module.chat_pipeline, "ask_mysql", fake_ask_mysql)
    save_clarification_context(
        "session-a",
        {
            "active": True,
            "original_question": "Show purchase orders for ABC.",
            "missing_slots": ["entity"],
            "filled_slots": {},
            "turns_remaining": 3,
        },
    )

    client = TestClient(main_module.app)
    response = client.post("/chat", json={"session_id": "session-a", "question": "RDSS-VIZAG"})

    assert response.status_code == 200
    assert seen == ["Show purchase orders for RDSS-VIZAG."]
    assert get_clarification_context("session-a") is None


def test_route_preserves_filled_slots_across_multiple_clarifications(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module, "CLARIFICATION_DIR", tmp_path)
    monkeypatch.setattr(main_module, "get_session_source", lambda session_id: {
        "source_type": "mysql",
        "database": "erp",
        "schema_fingerprint": "fp",
    })
    seen = []

    def fake_ask_mysql(question, database, identity):
        seen.append(question)
        if len(seen) == 1:
            return {
                "success": True,
                "answer": "Which metric?",
                "sql": None,
                "result": None,
                "clarification_context": {
                    "active": True,
                    "original_question": question,
                    "missing_slots": ["metric"],
                    "current_expected_slot": "metric",
                    "metric_options": ["PO total value", "PO count"],
                    "filled_slots": {},
                },
                "confidence": {"confidence_score": 40, "confidence_level": "low", "confidence_reasons": [], "limitations": []},
            }
        return {
            "success": True,
            "answer": "answered",
            "sql": "SELECT 1",
            "result": {"records": [{"x": 1}]},
            "confidence": {"confidence_score": 90, "confidence_level": "high", "confidence_reasons": [], "limitations": []},
        }

    monkeypatch.setattr(main_module.chat_pipeline, "ask_mysql", fake_ask_mysql)
    save_clarification_context(
        "session-a",
        {
            "active": True,
            "original_question": "Show purchase orders for ABC.",
            "missing_slots": ["entity"],
            "filled_slots": {},
            "turns_remaining": 3,
        },
    )

    client = TestClient(main_module.app)
    first = client.post("/chat", json={"session_id": "session-a", "question": "RDSS-VIZAG"})

    assert first.status_code == 200
    stored = get_clarification_context("session-a")
    assert stored["original_question"] == "Show purchase orders for ABC."
    assert stored["filled_slots"]["entity"] == "RDSS-VIZAG"

    second = client.post("/chat", json={"session_id": "session-a", "question": "PO total value"})

    assert second.status_code == 200
    assert seen[-1] == "Show purchase order total value by project for project RDSS-VIZAG."
    assert get_clarification_context("session-a") is None


def test_route_context_is_session_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module, "CLARIFICATION_DIR", tmp_path)
    save_clarification_context(
        "session-a",
        {
            "active": True,
            "original_question": "Show purchase orders for ABC.",
            "missing_slots": ["entity"],
            "filled_slots": {},
            "turns_remaining": 3,
        },
    )

    assert get_clarification_context("session-b") is None
    assert get_clarification_context("session-a") is not None
