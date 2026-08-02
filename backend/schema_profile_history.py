import hashlib
import json
import re
from pathlib import Path
from threading import Lock


HISTORY_DIR = Path(__file__).resolve().parent / "runtime" / "schema_profile_history"


class SchemaProfileHistory:
    def __init__(self, history_dir=HISTORY_DIR, max_records=100):
        self.history_dir = Path(history_dir)
        self.max_records = max(1, int(max_records))
        self._lock = Lock()

    def list(self, schema_fingerprint):
        path = self._path(schema_fingerprint)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        return payload if isinstance(payload, list) else []

    def record_success(self, schema_fingerprint, sql, selected_tables):
        tables = list(dict.fromkeys(str(table) for table in selected_tables if table))
        if not schema_fingerprint or not sql or not tables:
            return
        query_fingerprint = hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()
        record = {
            "query_fingerprint": query_fingerprint,
            "selected_tables": tables,
            "validated": True,
            "execution_success": True,
            "confidence": 100,
        }
        with self._lock:
            records = [
                item for item in self.list(schema_fingerprint)
                if item.get("query_fingerprint") != query_fingerprint
            ]
            records.append(record)
            records = records[-self.max_records:]
            path = self._path(schema_fingerprint)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
            temporary.replace(path)

    def _path(self, schema_fingerprint):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(schema_fingerprint))
        return self.history_dir / f"{safe}.json"


schema_profile_history = SchemaProfileHistory()
