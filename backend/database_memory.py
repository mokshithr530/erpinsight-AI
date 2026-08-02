import json
import re
from pathlib import Path

from business_catalog import business_catalog
from database_fingerprint import sha256_text, stable_json


MEMORY_VERSION = "database-memory:v1"
MEMORY_DIR = Path(__file__).resolve().parent / "runtime" / "database_memory"


class DatabaseMemory:
    def __init__(self, memory_dir: Path = MEMORY_DIR):
        self.memory_dir = memory_dir
        self._cache = {}

    def path_for(self, fingerprint: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", fingerprint)
        return self.memory_dir / f"{safe}.json"

    def get_or_build(
        self,
        *,
        source_type: str,
        database_name: str,
        schema: dict,
        row_counts: dict | None = None,
        fingerprint: str,
    ) -> dict:
        cached = self.get(fingerprint)
        if cached and cached.get("memory_version") == MEMORY_VERSION:
            return cached

        memory = self.build(
            source_type=source_type,
            database_name=database_name,
            schema=schema,
            row_counts=row_counts or {},
            fingerprint=fingerprint,
        )
        self.save(memory)
        return memory

    def get(self, fingerprint: str) -> dict | None:
        if fingerprint in self._cache:
            return self._cache[fingerprint]
        path = self.path_for(fingerprint)
        if not path.exists():
            return None
        try:
            memory = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        self._cache[fingerprint] = memory
        return memory

    def save(self, memory: dict) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = memory["schema_fingerprint"]
        self.path_for(fingerprint).write_text(
            json.dumps(memory, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._cache[fingerprint] = memory

    def build(
        self,
        *,
        source_type: str,
        database_name: str,
        schema: dict,
        row_counts: dict,
        fingerprint: str,
    ) -> dict:
        catalog = business_catalog.build(
            schema,
            source_type=source_type,
            row_counts=row_counts,
            fingerprint=fingerprint,
        )
        entity_by_table = {}
        for entity, detail in catalog.get("entities", {}).items():
            for table in detail.get("tables", []):
                entity_by_table.setdefault(table["table"], set()).add(entity)

        tables = []
        for table, info in sorted(schema.items()):
            columns = [self._column(column) for column in info.get("columns", [])]
            role = self._table_role(table, columns, entity_by_table.get(table, set()))
            tables.append(
                {
                    "table": table,
                    "role": role,
                    "entities": sorted(entity_by_table.get(table, [])),
                    "aliases": sorted(self._expanded_words(f"{table} {role}")),
                    "row_count": row_counts.get(table, info.get("row_count")),
                    "important_columns": self._important_columns(columns),
                    "columns": [
                        {
                            "name": column["name"],
                            "type": column["type"],
                            "primary_key": column["primary_key"],
                            "indexed": column["indexed"],
                        }
                        for column in columns
                    ],
                }
            )

        memory = {
            "memory_version": MEMORY_VERSION,
            "source_type": source_type,
            "database_name": database_name,
            "schema_fingerprint": fingerprint,
            "table_count": len(schema),
            "table_memory_hash": sha256_text(stable_json(tables)),
            "summary": catalog.get("summary"),
            "detected_modules": list(catalog.get("entities", {}).keys())[:16],
            "important_tables": catalog.get("important_tables", [])[:30],
            "relationships": catalog.get("join_paths", [])[:80],
            "metrics": catalog.get("metrics", [])[:40],
            "dimensions": catalog.get("dimensions", [])[:40],
            "tables": tables,
        }
        memory["overview_prompt"] = self._overview_prompt(memory)
        return memory

    def relevant_tables(self, question: str, memory: dict, limit: int = 10) -> list[str]:
        words = self._expanded_words(question)
        scores = []
        for table in memory.get("tables", []):
            haystack = set(table.get("aliases", []))
            haystack.update(self._expanded_words(table["table"]))
            haystack.update(self._expanded_words(table.get("role") or ""))
            for entity in table.get("entities", []):
                haystack.update(self._expanded_words(entity))

            column_hits = []
            for column in table.get("columns", []):
                column_words = self._expanded_words(column["name"])
                if words & column_words:
                    column_hits.append(column["name"])

            score = len(words & haystack) * 12 + len(column_hits) * 4
            if table.get("table", "").lower() in question.lower():
                score += 80
            if table.get("role") in {"master", "transaction", "financial"}:
                score += 3
            if table.get("row_count"):
                score += min(8, int(table["row_count"] or 0) // 10000)
            if score:
                scores.append((score, table["table"]))
        return [table for _, table in sorted(scores, key=lambda item: (-item[0], item[1]))[:limit]]

    def prompt_for_question(self, question: str, memory: dict, limit: int = 10) -> str:
        selected = set(self.relevant_tables(question, memory, limit=limit))
        if not selected:
            selected = {
                item["table"] if isinstance(item, dict) else item
                for item in memory.get("important_tables", [])[:limit]
            }
        lines = [
            "Cached database memory:",
            f"- Source: {memory.get('source_type')} database",
            f"- Tables: {memory.get('table_count')}",
            f"- Modules: {', '.join(memory.get('detected_modules', [])[:12])}",
            f"- Schema fingerprint: {memory.get('schema_fingerprint')}",
            "Relevant remembered tables:",
        ]
        table_lookup = {table["table"]: table for table in memory.get("tables", [])}
        for table_name in selected:
            table = table_lookup.get(table_name)
            if not table:
                continue
            columns = ", ".join(column["name"] for column in table.get("important_columns", [])[:14])
            lines.append(
                f"- {table_name}: role={table.get('role')}; "
                f"entities={', '.join(table.get('entities', [])[:4]) or 'general'}; "
                f"columns={columns}"
            )
        return "\n".join(lines)

    def _overview_prompt(self, memory: dict) -> str:
        important = [
            item["table"] if isinstance(item, dict) else item
            for item in memory.get("important_tables", [])[:12]
        ]
        return "\n".join(
            [
                f"{memory.get('source_type')} database with {memory.get('table_count')} tables.",
                f"Detected modules: {', '.join(memory.get('detected_modules', [])[:12])}.",
                f"Important tables: {', '.join(important)}.",
                f"Summary: {memory.get('summary') or 'No summary available.'}",
            ]
        )

    def _column(self, column: dict) -> dict:
        return {
            "name": column.get("name") or column.get("column_name") or column.get("COLUMN_NAME") or "",
            "type": column.get("type") or column.get("data_type") or column.get("DATA_TYPE") or "",
            "primary_key": bool(column.get("primary_key") or column.get("COLUMN_KEY") == "PRI"),
            "indexed": bool(column.get("indexed") or column.get("COLUMN_KEY") in {"PRI", "MUL", "UNI"}),
        }

    def _important_columns(self, columns: list[dict], limit: int = 18) -> list[dict]:
        markers = (
            "id",
            "name",
            "date",
            "amount",
            "total",
            "price",
            "cost",
            "qty",
            "quantity",
            "status",
            "project",
            "client",
            "customer",
            "vendor",
            "employee",
            "material",
            "department",
        )
        scored = []
        for index, column in enumerate(columns):
            lower = column["name"].lower()
            score = 20 if column["primary_key"] else 0
            score += 8 if column["indexed"] else 0
            score += sum(3 for marker in markers if marker in lower)
            scored.append((score, -index, column))
        return [column for _, _, column in sorted(scored, reverse=True)[:limit]]

    def _table_role(self, table: str, columns: list[dict], entities: set[str]) -> str:
        lower = table.lower()
        column_text = " ".join(column["name"].lower() for column in columns)
        if lower.endswith(("_mst", "_master")):
            return "master"
        if any(word in lower for word in ("bill", "invoice", "payment", "bank")):
            return "financial"
        if any(word in lower for word in ("transaction", "usage", "issue", "receipt", "grn")):
            return "transaction"
        if any(word in lower for word in ("po", "purchase", "quote", "vendor")):
            return "procurement"
        if "project" in entities or "project" in column_text:
            return "project-linked"
        return "reference"

    def _expanded_words(self, text: str) -> set[str]:
        words = set(re.findall(r"[a-z0-9]+", str(text).lower()))
        expanded = set(words)
        for word in words:
            if word.endswith("s") and len(word) > 3:
                expanded.add(word[:-1])
            if word == "billing":
                expanded.update({"bill", "invoice", "revenue"})
            if word == "client":
                expanded.add("customer")
            if word == "customer":
                expanded.add("client")
            if word == "material":
                expanded.update({"item", "product", "inventory"})
            if word == "project":
                expanded.update({"job", "site", "work"})
        return expanded


database_memory = DatabaseMemory()
