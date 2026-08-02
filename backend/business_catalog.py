import hashlib
import json
import re

from cache_identity import CATALOG_VERSION
from database_fingerprint import schema_fingerprint as stable_schema_fingerprint
from relationship_discovery import relationship_discovery
from verified_semantic_catalog import (
    column_exists,
    load_verified_semantic_catalog,
    table_exists,
)


class BusinessCatalog:
    ENTITY_ALIASES = {
        "billing": ("bill", "billing", "invoice", "receivable"),
        "project": ("project", "site", "work"),
        "employee": ("employee", "emp", "staff", "worker"),
        "department": ("department", "dept", "division"),
        "customer": ("customer", "client"),
        "vendor": ("vendor", "supplier", "seller"),
        "product": ("product", "item", "material", "asset", "spare"),
        "payment": ("payment", "paid", "bank", "transaction"),
        "inventory": ("stock", "store", "inventory", "material", "grn"),
        "logistics": ("transport", "vehicle", "freight", "logistic"),
    }
    KPI_ALIASES = {
        "revenue": ("revenue", "sales", "bill", "billing", "invoice", "amount", "total"),
        "cost": ("cost", "expense", "spend"),
        "profit": ("profit", "margin", "loss"),
        "quantity": ("qty", "quantity", "count", "units"),
        "status": ("status", "state", "stage"),
        "date": ("date", "time", "created", "updated", "month", "year"),
    }
    NOISY_TABLE = re.compile(r"(?:hist|history|attachment|log|notification)", re.I)

    def __init__(self):
        self._cache = {}

    def fingerprint(self, schema):
        return stable_schema_fingerprint(schema)

    def build(self, schema, *, source_type="database", row_counts=None, fingerprint=None):
        fingerprint = fingerprint or self.fingerprint(schema)
        key = f"{source_type}:{fingerprint}"
        if key in self._cache:
            return self._cache[key]

        entities = self._entities(schema)
        relationship_graph = relationship_discovery.build_graph(schema)
        joins = relationship_graph["edges"]
        important_tables = self._important_tables(schema, row_counts or {})
        unsupported = self._unsupported_kpis(entities)
        metrics = self._metrics(schema)
        dimensions = self._dimensions(schema)
        catalog = {
            "catalog_version": CATALOG_VERSION,
            "source_type": source_type,
            "database_fingerprint": fingerprint,
            "schema_fingerprint": fingerprint,
            "table_count": len(schema),
            "entities": entities,
            "aliases": self.ENTITY_ALIASES,
            "kpi_aliases": self.KPI_ALIASES,
            "metrics": metrics,
            "dimensions": dimensions,
            "date_fields": self._columns_by_marker(schema, ("date", "time", "created", "updated", "month", "year")),
            "status_fields": self._columns_by_marker(schema, ("status", "state", "stage")),
            "identifiers": self._columns_by_marker(schema, ("id", "code", "key")),
            "important_tables": important_tables[:20],
            "relationship_graph": relationship_graph,
            "known_joins": [join for join in joins if join.get("declared")][:50],
            "inferred_joins": [join for join in joins if not join.get("declared")][:50],
            "join_paths": joins[:50],
            "ambiguity": self._ambiguity(entities, metrics),
            "unsupported_kpis": unsupported,
            "summary": self._summary(source_type, schema, entities),
        }
        self._merge_verified_catalog(catalog, schema, fingerprint)
        self._cache[key] = catalog
        return catalog

    def overview(self, schema, *, source_type="database", row_counts=None, fingerprint=None):
        catalog = self.build(
            schema,
            source_type=source_type,
            row_counts=row_counts,
            fingerprint=fingerprint,
        )
        return {
            "source_type": catalog["source_type"],
            "catalog_version": catalog["catalog_version"],
            "database_fingerprint": catalog["database_fingerprint"],
            "schema_fingerprint": catalog["schema_fingerprint"],
            "table_count": catalog["table_count"],
            "detected_modules": list(catalog["entities"].keys())[:12],
            "important_tables": catalog["important_tables"][:12],
            "key_relationships": catalog["join_paths"][:12],
            "metrics": catalog["metrics"][:20],
            "dimensions": catalog["dimensions"][:20],
            "ambiguity": catalog["ambiguity"][:12],
            "unsupported_kpis": catalog["unsupported_kpis"],
            "short_schema_summary": catalog["summary"],
        }

    def analyze_question(self, question, schema, *, source_type="database", row_counts=None, fingerprint=None):
        catalog = self.build(
            schema,
            source_type=source_type,
            row_counts=row_counts,
            fingerprint=fingerprint,
        )
        words = self._expanded_words(question)
        candidates = []
        for entity, detail in catalog["entities"].items():
            entity_words = set(self.ENTITY_ALIASES.get(entity, (entity,)))
            score = len(words & entity_words) * 10
            matched_tables = []
            for table in detail["tables"]:
                table_words = self._expanded_words(table["table"])
                column_hits = [
                    column for column in table["columns"]
                    if words & self._expanded_words(column["name"])
                ]
                table_score = len(words & table_words) * 6 + len(column_hits) * 3
                if table_score:
                    matched_tables.append({
                        "table": table["table"],
                        "columns": [column["name"] for column in column_hits][:10],
                        "score": table_score,
                    })
                score += table_score
            if score:
                candidates.append({
                    "entity": entity,
                    "score": score,
                    "tables": sorted(matched_tables, key=lambda item: -item["score"])[:8],
                })
        if not candidates:
            generic_tables = []
            for table, info in schema.items():
                table_score = len(words & self._expanded_words(table)) * 6
                column_hits = []
                for column in info.get("columns", []):
                    name = self._column_name(column)
                    if words & self._expanded_words(name):
                        column_hits.append(name)
                        table_score += 3
                if table_score:
                    generic_tables.append({
                        "table": table,
                        "columns": column_hits[:10],
                        "score": table_score,
                    })
            if generic_tables:
                candidates.append({
                    "entity": "general",
                    "score": max(table["score"] for table in generic_tables),
                    "tables": sorted(generic_tables, key=lambda item: -item["score"])[:8],
                })
        return {
            "question": question,
            "candidates": sorted(candidates, key=lambda item: -item["score"])[:10],
            "missing_kpis": self._missing_kpis(question, catalog),
            "metrics": self._matching_metrics(question, catalog),
            "dimensions": self._matching_dimensions(question, catalog),
            "ambiguity": self._question_ambiguity(question, catalog),
            "schema_fingerprint": catalog["schema_fingerprint"],
            "catalog_version": catalog["catalog_version"],
        }

    def clear(self, fingerprint=None):
        if fingerprint is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if key.endswith(f":{fingerprint}"):
                self._cache.pop(key, None)

    def _entities(self, schema):
        entities = {}
        for entity, aliases in self.ENTITY_ALIASES.items():
            tables = []
            for table, info in schema.items():
                columns = [self._column(column) for column in info.get("columns", [])]
                text = " ".join([table, *[column["name"] for column in columns]]).lower()
                matches = [alias for alias in aliases if alias in text]
                if matches:
                    tables.append({
                        "table": table,
                        "columns": self._rank_columns(columns)[:16],
                        "matched_aliases": matches[:5],
                        "row_count": info.get("row_count"),
                    })
            if tables:
                entities[entity] = {
                    "aliases": aliases,
                    "tables": sorted(
                        tables,
                        key=lambda item: (
                            -len(item["matched_aliases"]),
                            self.NOISY_TABLE.search(item["table"]) is not None,
                            item["table"],
                        ),
                    )[:20],
                }
        return entities

    def _joins(self, schema):
        declared = []
        id_columns = {}
        primary_keys = {}
        for table, info in schema.items():
            for foreign_key in info.get("foreign_keys", []):
                column = foreign_key.get("column")
                ref_table = foreign_key.get("table")
                ref_column = foreign_key.get("references")
                if column and ref_table and ref_column:
                    declared.append({
                        "left_table": table,
                        "left_column": column,
                        "right_table": ref_table,
                        "right_column": ref_column,
                        "confidence": "high",
                        "evidence": "foreign_key",
                    })
            for column in info.get("columns", []):
                name = self._column_name(column)
                if column.get("primary_key") or column.get("COLUMN_KEY") == "PRI":
                    primary_keys.setdefault(name.lower(), []).append(table)
                if name.lower().endswith("_id") or name.lower() == "id":
                    id_columns.setdefault(name.lower(), []).append(table)

        inferred = []
        for column, tables in id_columns.items():
            if len(tables) < 2:
                continue
            evidence = "shared_id_column"
            confidence = "medium" if column in primary_keys else "low"
            inferred.append({
                "column": column,
                "tables": sorted(tables)[:10],
                "confidence": confidence,
                "evidence": evidence,
            })
        return declared + inferred

    def _important_tables(self, schema, row_counts):
        scored = []
        for table, info in schema.items():
            columns = info.get("columns", [])
            score = len(columns)
            lower = table.lower()
            if lower.endswith(("_master", "_mst", "_table")):
                score += 15
            if any(alias in lower for aliases in self.ENTITY_ALIASES.values() for alias in aliases):
                score += 12
            if self.NOISY_TABLE.search(lower):
                score -= 12
            score += min(20, int(row_counts.get(table, info.get("row_count") or 0) or 0) // 1000)
            scored.append({
                "table": table,
                "score": score,
                "row_count": row_counts.get(table, info.get("row_count")),
                "columns": [self._column_name(column) for column in columns[:12]],
            })
        return sorted(scored, key=lambda item: (-item["score"], item["table"]))

    def _unsupported_kpis(self, entities):
        available = " ".join(
            column["name"].lower()
            for entity in entities.values()
            for table in entity["tables"]
            for column in table["columns"]
        )
        unsupported = []
        if not any(word in available for word in ("cost", "expense", "margin", "profit", "loss")):
            unsupported.append("true_profit_loss")
        return unsupported

    def _missing_kpis(self, question, catalog):
        lower = question.lower()
        if any(word in lower for word in ("loss", "profit", "margin")):
            if "true_profit_loss" in catalog["unsupported_kpis"]:
                return ["true_profit_loss"]
        return []

    def _metrics(self, schema):
        candidates = []
        metric_markers = {
            "amount_value": ("amount", "total", "value", "price", "grandtotal", "grand_total", "subtotal", "balance", "gross", "net"),
            "billing": ("bill", "billing", "invoice"),
            "revenue": ("revenue", "sales", "amount", "total", "value"),
            "cost": ("cost", "expense", "spend", "price"),
            "quantity": ("qty", "quantity", "count", "units"),
            "payment": ("payment", "paid", "received", "receivable"),
        }
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = self._column_name(column)
                lowered = name.lower()
                for metric, markers in metric_markers.items():
                    matches = [marker for marker in markers if marker in lowered or marker in table.lower()]
                    if matches:
                        candidates.append({
                            "metric": metric,
                            "table": table,
                            "column": name,
                            "confidence": min(95, 45 + len(matches) * 15),
                            "evidence": matches,
                        })
        return sorted(candidates, key=lambda item: (-item["confidence"], item["metric"], item["table"]))[:80]

    def _dimensions(self, schema):
        candidates = []
        dimension_markers = {
            "project": ("project", "site", "work"),
            "client": ("client", "customer"),
            "vendor": ("vendor", "supplier"),
            "department": ("department", "dept", "division"),
            "employee": ("employee", "emp", "staff"),
            "product": ("product", "item", "material"),
            "date": ("date", "time", "month", "year"),
            "status": ("status", "state", "stage"),
        }
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = self._column_name(column)
                lowered = f"{table} {name}".lower()
                for dimension, markers in dimension_markers.items():
                    matches = [marker for marker in markers if marker in lowered]
                    if matches:
                        candidates.append({
                            "dimension": dimension,
                            "table": table,
                            "column": name,
                            "confidence": min(95, 45 + len(matches) * 15),
                            "evidence": matches,
                        })
        return sorted(candidates, key=lambda item: (-item["confidence"], item["dimension"], item["table"]))[:80]

    def _columns_by_marker(self, schema, markers):
        matches = []
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = self._column_name(column)
                lowered = name.lower()
                if any(marker in lowered for marker in markers):
                    matches.append({"table": table, "column": name})
        return matches[:80]

    def _ambiguity(self, entities, metrics):
        ambiguous = []
        for entity, detail in entities.items():
            if len(detail["tables"]) > 1:
                ambiguous.append({
                    "concept": entity,
                    "candidate_count": len(detail["tables"]),
                    "candidates": [table["table"] for table in detail["tables"][:8]],
                })
        by_metric = {}
        for metric in metrics:
            by_metric.setdefault(metric["metric"], set()).add((metric["table"], metric["column"]))
        for metric, candidates in by_metric.items():
            if len(candidates) > 3:
                ambiguous.append({
                    "concept": metric,
                    "candidate_count": len(candidates),
                    "candidates": [f"{table}.{column}" for table, column in sorted(candidates)[:8]],
                })
        return ambiguous

    def _matching_metrics(self, question, catalog):
        words = self._expanded_words(question)
        return [
            metric for metric in catalog["metrics"]
            if metric["metric"] in words or words & self._expanded_words(metric["column"])
        ][:10]

    def _matching_dimensions(self, question, catalog):
        words = self._expanded_words(question)
        return [
            dimension for dimension in catalog["dimensions"]
            if dimension["dimension"] in words or words & self._expanded_words(dimension["column"])
        ][:10]

    def _question_ambiguity(self, question, catalog):
        words = self._expanded_words(question)
        return [
            item for item in catalog["ambiguity"]
            if item["concept"] in words
        ][:6]

    def _summary(self, source_type, schema, entities):
        modules = ", ".join(list(entities.keys())[:8]) or "no clear business modules"
        return f"{source_type} database with {len(schema)} tables. Detected modules: {modules}."

    def _merge_verified_catalog(self, catalog, schema, fingerprint):
        verified = load_verified_semantic_catalog(
            fingerprint,
            schema=schema,
            strict=True,
        )
        if not verified:
            return
        filtered = {
            "business_concepts": self._verified_concepts(verified, schema),
            "entity_types": self._verified_entity_types(verified, schema),
            "business_formulas": self._verified_formulas(verified, schema),
            "business_definitions": verified.get(
                "business_definitions",
                [],
            ),
            "duplicate_risk_warnings": verified.get("duplicate_risk_warnings", []),
            "excluded_from_retrieval": verified.get("excluded_from_retrieval", []),
            "unresolved_definitions": verified.get("unresolved_definitions", []),
            "source_evidence": verified.get("source_evidence", []),
            "catalog_name": verified.get("catalog_name"),
            "catalog_version": verified.get("catalog_version"),
            "catalog_contract_version": verified.get("catalog_contract_version"),
            "erp_product": verified.get("erp_product"),
            "erp_schema_version": verified.get("erp_schema_version"),
            "authority": verified.get("authority"),
            "fallback_policy": verified.get("fallback_policy"),
            "validation": verified.get("validation"),
        }
        catalog["verified_semantic_catalog"] = filtered
        catalog["catalog_version"] = verified["catalog_version"]
        catalog["catalog_contract_version"] = verified["catalog_contract_version"]
        catalog["catalog_authority"] = verified["authority"]
        catalog["catalog_fallback_policy"] = verified["fallback_policy"]
        catalog["erp_product"] = verified["erp_product"]
        catalog["erp_schema_version"] = verified["erp_schema_version"]
        catalog["catalog_validation"] = verified["validation"]
        catalog["important_tables"] = self._merge_table_items(
            catalog.get("important_tables", []),
            [item for item in verified.get("important_tables", []) if table_exists(schema, item.get("table"))],
        )
        catalog["metrics"] = self._merge_column_items(
            catalog.get("metrics", []),
            [
                item for item in verified.get("metrics", [])
                if column_exists(schema, item.get("table"), item.get("column"))
            ],
            key_fields=("metric", "table", "column"),
        )
        catalog["dimensions"] = self._merge_column_items(
            catalog.get("dimensions", []),
            [
                item for item in verified.get("dimensions", [])
                if column_exists(schema, item.get("table"), item.get("column"))
            ],
            key_fields=("dimension", "table", "column"),
        )
        catalog["date_fields"] = self._merge_column_items(
            catalog.get("date_fields", []),
            [
                item for item in verified.get("date_fields", [])
                if column_exists(schema, item.get("table"), item.get("column"))
            ],
            key_fields=("table", "column"),
        )
        catalog["status_fields"] = self._merge_column_items(
            catalog.get("status_fields", []),
            [
                item for item in verified.get("status_fields", [])
                if column_exists(schema, item.get("table"), item.get("column"))
            ],
            key_fields=("table", "column"),
        )
        duplicate_risks = verified.get("duplicate_risk_warnings", [])
        verified_joins = [
            self._with_duplicate_risk(item, duplicate_risks)
            for item in verified.get("join_paths", [])
            if column_exists(schema, item.get("left_table"), item.get("left_column"))
            and column_exists(schema, item.get("right_table"), item.get("right_column"))
        ]
        catalog["known_joins"] = self._merge_join_items(catalog.get("known_joins", []), verified_joins)
        catalog["join_paths"] = self._merge_join_items(catalog.get("join_paths", []), verified_joins)
        catalog["verified_relationship_graph"] = relationship_discovery.build_graph(
            schema,
            verified_joins,
        )
        catalog["unsupported_kpis"] = sorted(set(
            catalog.get("unsupported_kpis", [])
            + [
                item["concept"]
                for item in filtered["unresolved_definitions"]
                if item.get("concept")
            ]
        ))
        catalog["summary"] = (
            f"{catalog['summary']} Verified ERP catalog adds "
            f"{len(filtered['business_concepts'])} business concept mappings, "
            f"{len(catalog['metrics'])} metric candidates, and "
            f"{len(verified_joins)} verified join paths."
        )

    def _verified_concepts(self, verified, schema):
        concepts = []
        for item in verified.get("business_concepts", []):
            table = item.get("preferred_table")
            column = item.get("preferred_column")
            display = item.get("display_column")
            if table and not table_exists(schema, table):
                continue
            if column and not column_exists(schema, table, column):
                continue
            if display and not column_exists(schema, table, display):
                continue
            concepts.append(item)
        return concepts

    def _verified_entity_types(self, verified, schema):
        result = []
        for item in verified.get("entity_types", []):
            identifier_columns = [
                value for value in item.get("identifier_columns", [])
                if self._qualified_column_exists(schema, value)
            ]
            display_columns = [
                value for value in item.get("display_columns", [])
                if self._qualified_column_exists(schema, value)
            ]
            master_tables = [
                table for table in item.get("master_tables", [])
                if table_exists(schema, table)
            ]
            if identifier_columns or display_columns or master_tables:
                result.append({
                    **item,
                    "identifier_columns": identifier_columns,
                    "display_columns": display_columns,
                    "master_tables": master_tables,
                })
        return result

    def _verified_formulas(self, verified, schema):
        formulas = []
        for item in verified.get("business_formulas", []):
            table = item.get("preferred_table")
            column = item.get("preferred_column")
            if table and column and not column_exists(schema, table, column):
                continue
            formulas.append(item)
        return formulas

    def _qualified_column_exists(self, schema, value):
        if "." not in str(value):
            return False
        table, column = value.split(".", 1)
        return column_exists(schema, table, column)

    def _merge_table_items(self, existing, verified):
        merged = {item.get("table"): item for item in existing if item.get("table")}
        for item in verified:
            merged[item["table"]] = {**merged.get(item["table"], {}), **item}
        return sorted(merged.values(), key=lambda item: (-float(item.get("score") or 0), item.get("table", "")))[:80]

    def _merge_column_items(self, existing, verified, key_fields):
        merged = {
            tuple(item.get(field) for field in key_fields): item
            for item in existing
            if all(item.get(field) is not None for field in key_fields)
        }
        for item in verified:
            key = tuple(item.get(field) for field in key_fields)
            merged[key] = {**merged.get(key, {}), **item}
        return sorted(
            merged.values(),
            key=lambda item: (-float(item.get("confidence") or 0), *(str(item.get(field, "")) for field in key_fields)),
        )[:160]

    def _merge_join_items(self, existing, verified):
        def columns(value):
            if value is None:
                return ()
            if isinstance(value, str):
                return (value,)
            return tuple(value)

        def key(item):
            return (
                item.get("left_table") or item.get("source_table"),
                columns(item.get("left_column") or item.get("source_columns")),
                item.get("right_table") or item.get("target_table"),
                columns(item.get("right_column") or item.get("target_columns")),
            )

        merged = {key(item): item for item in existing}
        for item in verified:
            merged[key(item)] = {**merged.get(key(item), {}), **item}
        return list(merged.values())[:120]

    def _with_duplicate_risk(self, join, duplicate_risks):
        result = dict(join)
        modules = result.get("applicable_business_modules") or result.get("applicable_module") or []
        result["applicable_business_modules"] = [modules] if isinstance(modules, str) else list(modules)
        result.setdefault("source_table", result.get("left_table"))
        result.setdefault("source_column", result.get("left_column"))
        result.setdefault("target_table", result.get("right_table"))
        result.setdefault("target_column", result.get("right_column"))
        result.setdefault("duplicate_amplification_risk", "low")
        result.setdefault("required_pre_aggregation", False)
        left = result.get("left_table")
        right = result.get("right_table")
        for warning in duplicate_risks:
            text = str(warning.get("relationship") or "").lower()
            if not left or not right:
                continue
            direct = f"{left} -> {right}".lower() in text
            reverse = f"{right} -> {left}".lower() in text
            if direct or reverse:
                result["duplicate_amplification_risk"] = "medium"
                result["required_pre_aggregation"] = True
                result["duplicate_risk_warning"] = warning.get("warning")
                result["duplicate_risk_evidence"] = warning.get("source_of_evidence")
        notes = str(result.get("ambiguity_notes") or "").lower()
        cardinality = str(result.get("cardinality") or "").lower()
        if "many-to-many" in cardinality or "many to many" in cardinality:
            result["duplicate_amplification_risk"] = "high"
            result["required_pre_aggregation"] = True
        elif "duplicate" in notes or "detail" in notes:
            result["duplicate_amplification_risk"] = max(
                result["duplicate_amplification_risk"],
                "medium",
                key={"low": 0, "medium": 1, "high": 2}.get,
            )
            result["required_pre_aggregation"] = True
        return result

    def _column(self, column):
        return {
            "name": self._column_name(column),
            "type": column.get("type") or column.get("data_type") or column.get("DATA_TYPE"),
            "primary_key": bool(column.get("primary_key") or column.get("COLUMN_KEY") == "PRI"),
            "not_null": bool(column.get("not_null")),
        }

    def _column_name(self, column):
        return column.get("name") or column.get("column_name") or column.get("COLUMN_NAME")

    def _rank_columns(self, columns):
        markers = ("id", "name", "date", "amount", "total", "status", "type", "qty", "price", "cost")
        return sorted(
            columns,
            key=lambda column: (
                -sum(1 for marker in markers if marker in column["name"].lower()),
                column["name"],
            ),
        )

    def _expanded_words(self, text):
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        expanded = set(words)
        for word in words:
            if word.endswith("ies") and len(word) > 3:
                expanded.add(f"{word[:-3]}y")
            if word.endswith("s") and len(word) > 3:
                expanded.add(word[:-1])
            if word.endswith("ing") and len(word) > 5:
                expanded.add(word[:-3])
        return expanded


business_catalog = BusinessCatalog()
