from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any


EVALUATION_TRACE_VERSION = "evaluation-trace:v1"


@dataclass
class EvaluationTrace:
    original_question: str
    parsed_intent: dict[str, Any] | None = None
    question_contract: dict[str, Any] | None = None
    extracted_entity_candidates: list[dict[str, Any]] = field(default_factory=list)
    resolved_entities: list[dict[str, Any]] = field(default_factory=list)
    selected_tables: list[str] = field(default_factory=list)
    selected_columns: dict[str, list[str]] = field(default_factory=dict)
    relationship_path: list[dict[str, Any]] = field(default_factory=list)
    query_plan: dict[str, Any] | None = None
    generated_sql: str | None = None
    validation_result: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    final_response: dict[str, Any] | None = None
    latency_ms: float | None = None
    confidence: dict[str, Any] | None = None
    error_category: str | None = None
    version: str = EVALUATION_TRACE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TraceTimer:
    def __init__(self):
        self._started = perf_counter()

    def elapsed_ms(self) -> float:
        return round((perf_counter() - self._started) * 1000, 2)


def classify_error_category(
    *,
    validation_result: dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
    final_response: dict[str, Any] | None = None,
) -> str | None:
    if validation_result and not validation_result.get("valid", validation_result.get("success", True)):
        return "validation_failed"
    if execution_result and not execution_result.get("success", True):
        return "execution_failed"
    if final_response and not final_response.get("success", True):
        return "response_failed"
    return None

