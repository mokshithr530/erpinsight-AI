import re
from collections import deque


RELATIONSHIP_GRAPH_VERSION = "relationship-graph:v2"


def _column_name(column: dict) -> str:
    return column.get("name") or column.get("column_name") or column.get("COLUMN_NAME") or ""


def _column_type(column: dict) -> str:
    return str(column.get("type") or column.get("data_type") or column.get("DATA_TYPE") or "").lower()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _singular(value: str) -> str:
    if value.endswith("ies"):
        return f"{value[:-3]}y"
    if value.endswith("s") and len(value) > 3:
        return value[:-1]
    return value


class RelationshipDiscovery:
    def build_graph(self, schema: dict, verified_relationships: list[dict] | None = None) -> dict:
        edges = []
        edges.extend(self._declared_edges(schema))
        edges.extend(self._verified_edges(schema, verified_relationships or []))
        edges.extend(self._inferred_edges(schema))
        unique = {}
        for edge in edges:
            key = (
                edge["left_table"],
                tuple(edge["left_columns"]),
                edge["right_table"],
                tuple(edge["right_columns"]),
            )
            reverse_key = (
                edge["right_table"],
                tuple(edge["right_columns"]),
                edge["left_table"],
                tuple(edge["left_columns"]),
            )
            if reverse_key in unique:
                continue
            if key not in unique or self._priority(edge) > self._priority(unique[key]):
                unique[key] = edge
        return {
            "version": RELATIONSHIP_GRAPH_VERSION,
            "edges": sorted(
                unique.values(),
                key=lambda edge: (
                    self._evidence_rank(edge),
                    edge["left_table"],
                    edge["right_table"],
                    edge["left_columns"],
                    -edge["confidence_score"],
                ),
            ),
        }

    def _priority(self, edge):
        return (self._evidence_rank(edge), edge["confidence_score"])

    def _evidence_rank(self, edge):
        evidence = set(edge.get("evidence") or [])
        if "declared_foreign_key" in evidence:
            return 5
        if "verified_erp_catalog" in evidence or edge.get("verification_status") == "verified":
            return 4
        if "existing_application_query" in evidence:
            return 3
        if "overlapping_key_values" in evidence:
            return 2
        return 1

    def find_paths(self, schema: dict, start_tables: list[str], end_tables: list[str], max_hops=4) -> list[dict]:
        graph = self.build_graph(schema)
        adjacency = {}
        for edge in graph["edges"]:
            adjacency.setdefault(edge["left_table"], []).append(edge)
            adjacency.setdefault(edge["right_table"], []).append(self._reverse(edge))

        targets = set(end_tables)
        paths = []
        for start in start_tables:
            queue = deque([(start, [], {start})])
            while queue:
                table, path, seen = queue.popleft()
                if len(path) > max_hops:
                    continue
                if table in targets and path:
                    paths.append(self._score_path(path))
                    continue
                for edge in adjacency.get(table, []):
                    next_table = edge["right_table"]
                    if next_table in seen:
                        continue
                    queue.append((next_table, [*path, edge], {*seen, next_table}))
        return sorted(paths, key=lambda path: (-path["confidence_score"], path["row_multiplication_risk"], len(path["edges"])))[:10]

    def _declared_edges(self, schema: dict) -> list[dict]:
        edges = []
        for table, info in schema.items():
            for foreign_key in info.get("foreign_keys", []):
                column = foreign_key.get("column")
                ref_table = foreign_key.get("table")
                ref_column = foreign_key.get("references")
                if not column or not ref_table or not ref_column:
                    continue
                edges.append(
                    {
                        "left_table": table,
                        "left_columns": [column],
                        "right_table": ref_table,
                        "right_columns": [ref_column],
                        "join_type": "foreign_key",
                        "declared": True,
                        "evidence": ["declared_foreign_key"],
                        "confidence_score": 95,
                        "cardinality": "many_to_one",
                        "row_multiplication_risk": "low",
                        "sample_validation": "not_sampled",
                        "verification_status": "declared",
                        "required_pre_aggregation": False,
                        "applicable_business_modules": [],
                    }
                )
        return edges

    def _verified_edges(self, schema: dict, relationships: list[dict]) -> list[dict]:
        edges = []
        for item in relationships:
            left_table = item.get("left_table") or item.get("source_table")
            right_table = item.get("right_table") or item.get("target_table")
            left_column = item.get("left_column") or item.get("source_column")
            right_column = item.get("right_column") or item.get("target_column")
            if not left_table or not right_table or not left_column or not right_column:
                continue
            if left_table not in schema or right_table not in schema:
                continue
            if not self._schema_has_column(schema, left_table, left_column):
                continue
            if not self._schema_has_column(schema, right_table, right_column):
                continue
            risk = self._duplicate_risk(item)
            edges.append({
                "left_table": left_table,
                "left_columns": [left_column],
                "right_table": right_table,
                "right_columns": [right_column],
                "join_type": item.get("relationship_type") or "verified_catalog",
                "declared": False,
                "evidence": ["verified_erp_catalog", item.get("source_of_evidence") or "verified_catalog"],
                "confidence_score": self._confidence_score(item.get("confidence")),
                "cardinality": item.get("cardinality") or item.get("relationship_type") or "unknown",
                "row_multiplication_risk": risk,
                "sample_validation": item.get("sample_validation") or "verified_catalog",
                "verification_status": item.get("verification_status") or "verified",
                "required_pre_aggregation": self._requires_pre_aggregation(item, risk),
                "applicable_business_modules": self._modules(item),
                "ambiguity_notes": item.get("ambiguity_notes") or "",
            })
        return edges

    def _inferred_edges(self, schema: dict) -> list[dict]:
        columns_by_name = {}
        primary_by_table = {}
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = _column_name(column)
                if not name:
                    continue
                lowered = name.lower()
                columns_by_name.setdefault(lowered, []).append((table, column))
                if column.get("primary_key") or column.get("COLUMN_KEY") == "PRI":
                    primary_by_table.setdefault(table, set()).add(lowered)

        edges = []
        for column_name, owners in columns_by_name.items():
            if len(owners) < 2:
                continue
            if not (column_name == "id" or column_name.endswith("_id")):
                continue
            for index, (left_table, left_column) in enumerate(owners):
                for right_table, right_column in owners[index + 1:]:
                    if left_table == right_table:
                        continue
                    score, evidence = self._score_inferred_edge(
                        left_table,
                        left_column,
                        right_table,
                        right_column,
                        primary_by_table,
                    )
                    if score < 45:
                        continue
                    edges.append(
                        {
                            "left_table": left_table,
                            "left_columns": [_column_name(left_column)],
                            "right_table": right_table,
                            "right_columns": [_column_name(right_column)],
                            "join_type": "inferred",
                            "declared": False,
                            "evidence": evidence,
                            "confidence_score": score,
                            "cardinality": "unknown",
                            "row_multiplication_risk": "medium" if score < 70 else "low",
                            "sample_validation": "not_sampled",
                            "verification_status": "inferred",
                            "required_pre_aggregation": score < 70,
                            "applicable_business_modules": [],
                        }
                    )
        return edges

    def _score_inferred_edge(self, left_table, left_column, right_table, right_column, primary_by_table):
        left_name = _column_name(left_column).lower()
        right_name = _column_name(right_column).lower()
        score = 35
        evidence = ["shared_identifier_name"]
        if _column_type(left_column) and _column_type(left_column) == _column_type(right_column):
            score += 10
            evidence.append("compatible_type")
        if left_name in primary_by_table.get(left_table, set()) or right_name in primary_by_table.get(right_table, set()):
            score += 20
            evidence.append("primary_key_evidence")
        left_tokens = {_singular(token) for token in _tokens(left_table)}
        right_tokens = {_singular(token) for token in _tokens(right_table)}
        column_tokens = {_singular(token) for token in _tokens(left_name)}
        if left_tokens & column_tokens or right_tokens & column_tokens:
            score += 8
            evidence.append("table_column_name_similarity")
        noisy = re.compile(r"(hist|history|log|attachment|notification)", re.I)
        if noisy.search(left_table) or noisy.search(right_table):
            score -= 12
            evidence.append("history_or_log_table_penalty")
        return min(score, 85), evidence

    def _reverse(self, edge: dict) -> dict:
        return {
            **edge,
            "left_table": edge["right_table"],
            "left_columns": edge["right_columns"],
            "right_table": edge["left_table"],
            "right_columns": edge["left_columns"],
        }

    def _score_path(self, edges: list[dict]) -> dict:
        confidence = min(edge["confidence_score"] for edge in edges) if edges else 0
        confidence -= max(0, len(edges) - 1) * 8
        if any(edge["row_multiplication_risk"] == "medium" for edge in edges):
            confidence -= 8
        if any(edge["row_multiplication_risk"] == "high" for edge in edges):
            confidence -= 16
        risk = "low"
        if any(edge["row_multiplication_risk"] == "high" for edge in edges):
            risk = "high"
        elif len(edges) > 2 or any(edge["row_multiplication_risk"] == "medium" for edge in edges):
            risk = "medium"
        if confidence < 50:
            risk = "high"
        return {
            "tables": [edges[0]["left_table"], *[edge["right_table"] for edge in edges]],
            "edges": edges,
            "confidence_score": max(0, min(100, confidence)),
            "confidence_level": "high" if confidence >= 80 else "medium" if confidence >= 55 else "low",
            "row_multiplication_risk": risk,
            "limitations": [] if risk == "low" else ["Join path is inferred or multi-hop; verify aggregation grain before totals."],
        }

    def _schema_has_column(self, schema, table, column):
        return any(_column_name(item) == column for item in schema.get(table, {}).get("columns", []))

    def _confidence_score(self, value):
        if isinstance(value, (int, float)):
            number = float(value)
            return int(round(number * 100 if number <= 1 else number))
        lowered = str(value or "").lower()
        if lowered == "high":
            return 92
        if lowered == "medium":
            return 76
        if lowered == "low":
            return 55
        return 70

    def _duplicate_risk(self, item):
        explicit = item.get("duplicate_amplification_risk") or item.get("row_multiplication_risk")
        if explicit:
            return str(explicit).lower()
        text = " ".join(str(item.get(key) or "") for key in ("cardinality", "ambiguity_notes", "relationship_type")).lower()
        if "many-to-many" in text or "many to many" in text:
            return "high"
        if "duplicate" in text or "detail" in text or "many" in text:
            return "medium"
        return "low"

    def _requires_pre_aggregation(self, item, risk):
        explicit = item.get("required_pre_aggregation")
        if explicit is not None:
            return bool(explicit)
        text = " ".join(str(item.get(key) or "") for key in ("cardinality", "ambiguity_notes", "relationship_type")).lower()
        return risk in {"medium", "high"} and ("detail" in text or "duplicate" in text or "many-to-many" in text)

    def _modules(self, item):
        value = item.get("applicable_business_modules") or item.get("applicable_module") or []
        if isinstance(value, str):
            return [value]
        return list(value)


relationship_discovery = RelationshipDiscovery()
