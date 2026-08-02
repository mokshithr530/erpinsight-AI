import json
from pathlib import Path


BENCHMARK_FILE = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "benchmarks"
    / "poc_pattern_benchmark_v1.json"
)


def test_poc_pattern_benchmark_has_strong_module_coverage():
    payload = json.loads(BENCHMARK_FILE.read_text())
    questions = payload["questions"]
    modules = {item["module"] for item in questions}

    assert payload["version"] == "poc-pattern-benchmark:v1"
    assert len(questions) >= 80
    assert {
        "projects",
        "billing",
        "purchase_orders",
        "vendors",
        "store_material_movement",
        "inventory",
        "safe_cross_module",
    } <= modules

    by_module = {
        module: sum(1 for item in questions if item["module"] == module)
        for module in modules
    }
    assert by_module["projects"] >= 10
    assert by_module["billing"] >= 10
    assert by_module["purchase_orders"] >= 10
    assert by_module["vendors"] >= 10
    assert by_module["store_material_movement"] >= 10
    assert by_module["inventory"] >= 8
    assert by_module["safe_cross_module"] >= 15


def test_poc_pattern_benchmark_marks_clarification_and_safe_refusal_cases():
    payload = json.loads(BENCHMARK_FILE.read_text())
    behaviors = {item["expected_behavior"] for item in payload["questions"]}

    assert "answer" in behaviors
    assert "clarify" in behaviors
    assert "clarify_or_safe_refusal" in behaviors
    assert any(
        item["module"] == "safe_cross_module"
        and item["expected_behavior"] != "answer"
        for item in payload["questions"]
    )


def test_poc_module_readiness_is_explicit_and_poc_or_limited():
    payload = json.loads(BENCHMARK_FILE.read_text())
    readiness = payload["module_readiness"]

    assert readiness
    for item in readiness:
        assert 0 <= item["completion_percent"] <= 100
        assert item["poc_ready"] in {True, False, "limited"}
        assert item["why"]


def test_phase_4_manager_llm_polish_smoke_is_recorded():
    payload = json.loads(BENCHMARK_FILE.read_text())
    phase_4 = payload["phase_4_manager_llm_polish"]
    smoke = phase_4["latest_llm_smoke"]

    assert phase_4["enabled_by_env"] == "LLM_POLISH_VERIFIED_ANSWERS=1"
    assert phase_4["provider"] == "openrouter"
    assert phase_4["model"]
    assert smoke["questions"] >= 10
    assert smoke["passed"] == smoke["questions"]
    assert smoke["api_calls"] == smoke["questions"]
    assert smoke["total_tokens"] > 0
    assert len(phase_4["questions"]) >= 10
    assert any("verified SQL compiler" in rule for rule in phase_4["runtime_contract"])
