import hashlib
import json
import os
from pathlib import Path


FINGERPRINT_VERSION = "fingerprint:v2"


def stable_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _column_name(column: dict) -> str:
    return column.get("name") or column.get("column_name") or column.get("COLUMN_NAME") or ""


def _column_type(column: dict) -> str:
    return column.get("type") or column.get("data_type") or column.get("DATA_TYPE") or ""


def normalized_schema(schema: dict) -> dict:
    normalized = {}
    for table, info in sorted(schema.items()):
        columns = []
        for column in info.get("columns", []):
            columns.append(
                {
                    "name": _column_name(column).lower(),
                    "type": str(_column_type(column)).lower(),
                    "nullable": not bool(column.get("not_null") or column.get("IS_NULLABLE") == "NO"),
                    "primary_key": bool(column.get("primary_key") or column.get("COLUMN_KEY") == "PRI"),
                    "unique": bool(column.get("unique") or column.get("COLUMN_KEY") == "UNI"),
                    "indexed": bool(column.get("indexed") or column.get("COLUMN_KEY") in {"PRI", "MUL", "UNI"}),
                }
            )
        foreign_keys = []
        for foreign_key in info.get("foreign_keys", []):
            foreign_keys.append(
                {
                    "column": str(foreign_key.get("column") or "").lower(),
                    "table": str(foreign_key.get("table") or "").lower(),
                    "references": str(foreign_key.get("references") or "").lower(),
                }
            )
        indexes = []
        for index in info.get("indexes", []):
            indexes.append(
                {
                    "name": str(index.get("name") or "").lower(),
                    "columns": [str(column).lower() for column in index.get("columns", [])],
                    "unique": bool(index.get("unique")),
                }
            )
        normalized[table.lower()] = {
            "columns": sorted(columns, key=lambda item: item["name"]),
            "foreign_keys": sorted(foreign_keys, key=lambda item: (item["column"], item["table"])),
            "indexes": sorted(indexes, key=lambda item: (item["name"], item["columns"])),
            "views": sorted(str(view).lower() for view in info.get("views", [])),
        }
    return normalized


def schema_fingerprint(schema: dict) -> str:
    return sha256_text(stable_json({"version": FINGERPRINT_VERSION, "schema": normalized_schema(schema)}))


def database_fingerprint(
    *,
    source_type: str,
    dialect: str,
    database_name: str | None = None,
    connection_id: str | None = None,
    host_identity: str | None = None,
    file_path: str | Path | None = None,
    uploaded_file_checksum: str | None = None,
) -> str:
    file_identity = None
    if file_path:
        path = Path(file_path)
        stat = path.stat()
        file_identity = {
            "name": path.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "checksum": uploaded_file_checksum or sha256_file(path),
        }
    payload = {
        "version": FINGERPRINT_VERSION,
        "source_type": source_type,
        "dialect": dialect,
        "database_name": database_name,
        "connection_id": connection_id,
        "host_identity": host_identity,
        "file": file_identity,
    }
    return sha256_text(stable_json(payload))


def data_version_for_file(path: str | Path) -> str:
    file_path = Path(path)
    stat = file_path.stat()
    return sha256_text(stable_json({"path": file_path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}))


def safe_host_identity(host: str | None, port: int | str | None = None) -> str | None:
    if not host:
        return None
    return sha256_text(f"{host}:{port or ''}:{os.name}")[:16]
