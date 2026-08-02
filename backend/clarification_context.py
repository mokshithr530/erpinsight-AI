import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CLARIFICATION_DIR = Path(__file__).resolve().parent / "runtime" / "clarifications"
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass
class ClarificationContext:
    active: bool
    original_question: str
    intent: str | None = None
    module: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    filled_slots: dict[str, Any] = field(default_factory=dict)
    expected_entity_types: list[str] = field(default_factory=list)
    expires_after_turns: int = 3
    turns_remaining: int = 3
    created_at: str = field(default_factory=lambda: _now())
    last_updated: str = field(default_factory=lambda: _now())
    last_answer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context_file(session_id: str) -> Path:
    if not session_id or not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("Invalid session_id.")
    return CLARIFICATION_DIR / f"{session_id}.json"


def get_clarification_context(session_id: str) -> dict[str, Any] | None:
    path = _context_file(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not data.get("active"):
        return None
    if int(data.get("turns_remaining") or 0) <= 0:
        clear_clarification_context(session_id)
        return None
    return data


def save_clarification_context(session_id: str, context: ClarificationContext | dict[str, Any]):
    data = context.to_dict() if isinstance(context, ClarificationContext) else dict(context)
    data["active"] = bool(data.get("active", True))
    data["last_updated"] = _now()
    data.setdefault("created_at", data["last_updated"])
    data.setdefault("expires_after_turns", 3)
    data.setdefault("turns_remaining", data["expires_after_turns"])
    path = _context_file(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def clear_clarification_context(session_id: str):
    try:
        _context_file(session_id).unlink()
    except FileNotFoundError:
        pass


def consume_unrelated_turn(session_id: str, context: dict[str, Any]) -> bool:
    remaining = int(context.get("turns_remaining") or context.get("expires_after_turns") or 3) - 1
    if remaining <= 0:
        clear_clarification_context(session_id)
        return False
    context["turns_remaining"] = remaining
    save_clarification_context(session_id, context)
    return True
