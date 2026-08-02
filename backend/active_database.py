import json
import os
import re
from pathlib import Path


SESSIONS_DIR = Path(__file__).resolve().parent / "runtime" / "sessions"
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _session_file(session_id: str) -> Path:
    if not session_id or not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("Invalid session_id.")
    return SESSIONS_DIR / f"{session_id}.json"


def save_session_database_path(
    session_id: str,
    path: str,
    name: str | None = None,
):
    state_file = _session_file(session_id)
    database_path = str(Path(path).expanduser().resolve())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = state_file.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps({"path": database_path, "name": name}),
        encoding="utf-8",
    )
    os.replace(temporary_file, state_file)


def save_session_source(session_id: str, source: dict):
    state_file = _session_file(session_id)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = state_file.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(source), encoding="utf-8")
    os.replace(temporary_file, state_file)


def save_session_sqlserver_connection(
    session_id: str,
    connection_id: str,
    database_name: str,
    name: str | None = None,
    schema_fingerprint: str | None = None,
    imported_tables: list[str] | None = None,
):
    save_session_source(
        session_id,
        {
            "source_type": "sqlserver",
            "connection_id": connection_id,
            "database": database_name,
            "name": name,
            "schema_fingerprint": schema_fingerprint,
            "imported_tables": imported_tables or [],
        },
    )


def save_session_mysql_connection(
    session_id: str,
    connection_id: str,
    database_name: str,
    name: str | None = None,
    schema_fingerprint: str | None = None,
    table_count: int | None = None,
    column_count: int | None = None,
):
    source = {
        "source_type": "mysql",
        "connection_id": connection_id,
        "database": database_name,
        "name": name,
        "schema_fingerprint": schema_fingerprint,
    }
    if table_count is not None:
        source["tables"] = table_count
    if column_count is not None:
        source["columns"] = column_count
    save_session_source(session_id, source)


def get_session_source(session_id: str) -> dict | None:
    state_file = _session_file(session_id)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    if "source_type" not in data and data.get("path"):
        data["source_type"] = "sqlite"
    return data


def get_session_database_path(session_id: str) -> str | None:
    data = get_session_source(session_id)
    if not data or data.get("source_type") not in {None, "sqlite"}:
        return None

    path = data.get("path")
    return path if isinstance(path, str) and path else None


def clear_session_database_path(session_id: str):
    state_file = _session_file(session_id)
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass
