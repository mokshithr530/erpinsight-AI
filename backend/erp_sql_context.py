import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from database_fingerprint import sha256_text, stable_json


ERP_SQL_CONTEXT_VERSION = "erp-sql-context:v2"


@dataclass
class ERPSQLContext:
    prompt: str
    canonical_ddl: str
    selected_tables: list[str]
    selected_columns: dict[str, list[str]]
    cache_key: str
    character_count: int
    version: str = ERP_SQL_CONTEXT_VERSION

    def to_dict(self):
        return {
            "prompt": self.prompt,
            "canonical_ddl": self.canonical_ddl,
            "selected_tables": self.selected_tables,
            "selected_columns": self.selected_columns,
            "cache_key": self.cache_key,
            "character_count": self.character_count,
            "version": self.version,
        }


class ERPSQLContextBuilder:
    def __init__(self, max_cache_entries=256):
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._cache = OrderedDict()

    def build(
        self,
        *,
        schema: dict[str, Any],
        query_plan,
        semantic_binding: dict[str, Any],
        catalog: dict[str, Any],
        schema_fingerprint: str,
    ) -> ERPSQLContext | None:
        if not semantic_binding.get("authoritative"):
            return None
        if semantic_binding.get("clarification_required"):
            return None

        tables = self._selected_tables(query_plan, semantic_binding, schema)
        if not tables:
            return None
        columns = self._selected_columns(
            query_plan,
            semantic_binding,
            schema,
            tables,
        )
        identity = {
            "version": ERP_SQL_CONTEXT_VERSION,
            "schema_fingerprint": schema_fingerprint,
            "catalog_version": semantic_binding.get("catalog_version"),
            "erp_schema_version": semantic_binding.get("erp_schema_version"),
            "plan": self._compact_plan(query_plan),
            "tables": tables,
            "columns": columns,
        }
        cache_key = sha256_text(stable_json(identity))
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        ddl = self._canonical_ddl(schema, tables, columns)
        prompt = self._prompt(
            query_plan=query_plan,
            semantic_binding=semantic_binding,
            catalog=catalog,
            canonical_ddl=ddl,
        )
        context = ERPSQLContext(
            prompt=prompt,
            canonical_ddl=ddl,
            selected_tables=tables,
            selected_columns=columns,
            cache_key=cache_key,
            character_count=len(prompt),
        )
        self._cache[cache_key] = context
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)
        return context

    def clear(self):
        self._cache.clear()

    def _selected_tables(self, plan, binding, schema):
        required = [
            *(binding.get("required_tables") or []),
            *[
                item.get("table")
                for item in plan.resolved_entity_constraints
                if isinstance(item, dict)
            ],
        ]
        for relationship in binding.get("approved_relationships") or []:
            required.extend([
                relationship.get("left_table"),
                relationship.get("right_table"),
            ])
        return self._unique([
            table
            for table in required
            if table in schema
        ])

    def _selected_columns(self, plan, binding, schema, tables):
        selected = {
            table: list((binding.get("required_columns") or {}).get(table, []))
            for table in tables
        }
        for entity in plan.resolved_entity_constraints:
            self._append(selected, entity.get("table"), entity.get("column"))
            self._append(
                selected,
                entity.get("table"),
                entity.get("primary_key_column"),
            )
        for relationship in binding.get("approved_relationships") or []:
            self._append(
                selected,
                relationship.get("left_table"),
                relationship.get("left_column"),
            )
            self._append(
                selected,
                relationship.get("right_table"),
                relationship.get("right_column"),
            )

        for table in tables:
            info = schema[table]
            for column in info.get("columns", []):
                if column.get("primary_key"):
                    self._append(selected, table, self._column_name(column))
            for foreign_key in info.get("foreign_keys", []):
                target = (
                    foreign_key.get("table")
                    or foreign_key.get("references_table")
                )
                if target in tables:
                    self._append(
                        selected,
                        table,
                        foreign_key.get("column"),
                    )
                    self._append(
                        selected,
                        target,
                        foreign_key.get("references")
                        or foreign_key.get("references_column"),
                    )

        result = {}
        for table in tables:
            available = {
                self._column_name(column)
                for column in schema[table].get("columns", [])
            }
            result[table] = [
                column
                for column in self._unique(selected.get(table, []))
                if column in available
            ]
        return result

    def _canonical_ddl(self, schema, tables, selected_columns):
        blocks = []
        for table in tables:
            info = schema[table]
            by_name = {
                self._column_name(column): column
                for column in info.get("columns", [])
            }
            names = selected_columns.get(table, [])
            lines = [
                self._column_ddl(by_name[name])
                for name in names
                if name in by_name
            ]
            primary_keys = [
                name
                for name in names
                if name in by_name and by_name[name].get("primary_key")
            ]
            if primary_keys:
                lines.append(
                    "  PRIMARY KEY ("
                    + ", ".join(self._quote(name) for name in primary_keys)
                    + ")"
                )
            for index in info.get("indexes", []):
                index_columns = index.get("columns") or []
                if (
                    index.get("unique")
                    and index.get("name") != "PRIMARY"
                    and index_columns
                    and all(column in names for column in index_columns)
                ):
                    lines.append(
                        f"  UNIQUE KEY {self._quote(index.get('name') or 'unique_key')} ("
                        + ", ".join(self._quote(name) for name in index_columns)
                        + ")"
                    )
            for foreign_key in info.get("foreign_keys", []):
                target_table = (
                    foreign_key.get("table")
                    or foreign_key.get("references_table")
                )
                source_column = foreign_key.get("column")
                target_column = (
                    foreign_key.get("references")
                    or foreign_key.get("references_column")
                )
                if (
                    target_table in tables
                    and source_column in names
                    and target_column in selected_columns.get(target_table, [])
                ):
                    constraint = foreign_key.get("name")
                    prefix = (
                        f"  CONSTRAINT {self._quote(constraint)} "
                        if constraint
                        else "  "
                    )
                    lines.append(
                        f"{prefix}FOREIGN KEY ({self._quote(source_column)}) "
                        f"REFERENCES {self._quote(target_table)} "
                        f"({self._quote(target_column)})"
                    )
            if not lines:
                continue
            comment = self._comment(info.get("comment"))
            suffix = f" COMMENT={self._literal(comment)}" if comment else ""
            blocks.append(
                f"CREATE TABLE {self._quote(table)} (\n"
                + ",\n".join(lines)
                + f"\n){suffix};"
            )
        return "\n\n".join(blocks)

    def _column_ddl(self, column):
        name = self._column_name(column)
        data_type = (
            column.get("column_type")
            or column.get("data_type")
            or column.get("type")
            or "TEXT"
        )
        parts = [f"  {self._quote(name)} {data_type}"]
        if column.get("not_null") or column.get("nullable") is False:
            parts.append("NOT NULL")
        if column.get("generated"):
            parts.append("/* GENERATED */")
        comment = self._comment(column.get("comment"))
        if comment:
            parts.append(f"COMMENT {self._literal(comment)}")
        return " ".join(parts)

    def _prompt(self, *, query_plan, semantic_binding, catalog, canonical_ddl):
        compact_plan = self._compact_plan(query_plan)
        business_definitions = {
            "verified_definitions": [
                self._pick(
                    item,
                    "concept_id",
                    "metric_definition",
                    "formula",
                    "aggregation",
                    "data_grain",
                    "date_field",
                    "date_storage_format",
                    "status_field",
                    "valid_status_values",
                    "status_value_meanings_verified",
                    "null_handling",
                    "approved_joins",
                    "duplicate_risk_rules",
                    "expected_output",
                    "reference_sql",
                    "ambiguity_notes",
                )
                for item in (
                    semantic_binding.get("bound_business_definitions") or []
                )
            ],
            "metrics": [
                self._pick(
                    item,
                    "metric",
                    "table",
                    "column",
                    "default_aggregation",
                    "aggregation_grain",
                    "ambiguity_notes",
                )
                for item in semantic_binding.get("bound_metrics") or []
            ],
            "dimensions": [
                self._pick(
                    item,
                    "dimension",
                    "table",
                    "column",
                )
                for item in semantic_binding.get("bound_dimensions") or []
            ],
            "date_fields": [
                self._pick(item, "semantic", "table", "column")
                for item in semantic_binding.get("bound_date_fields") or []
            ],
            "status_fields": [
                self._pick(
                    item,
                    "table",
                    "column",
                    "semantics",
                    "valid_values",
                    "value_meanings_verified",
                    "requested_values",
                )
                for item in semantic_binding.get("bound_status_fields") or []
            ],
            "formulas": [
                self._pick(
                    item,
                    "formula",
                    "definition",
                    "preferred_table",
                    "preferred_column",
                )
                for item in semantic_binding.get("bound_formulas") or []
            ],
        }
        relationships = [
            self._pick(
                item,
                "left_table",
                "left_column",
                "right_table",
                "right_column",
                "relationship_type",
                "cardinality",
                "required_pre_aggregation",
                "pre_aggregation_requirements",
                "duplicate_amplification_risk",
                "join_amplification",
                "distinct_key_overlap",
                "orphan_left_rows",
                "ambiguity_notes",
            )
            for item in semantic_binding.get("approved_relationships") or []
        ]
        duplicate_rules = [
            self._pick(
                item,
                "header_table",
                "detail_table",
                "risk",
                "rule",
                "required_pre_aggregation",
                "ambiguity_notes",
                "relationship",
                "warning",
                "preferred_table",
                "preferred_column",
            )
            for item in (
                (catalog.get("verified_semantic_catalog") or {}).get(
                    "duplicate_risk_warnings",
                    [],
                )
            )
            if self._warning_applies(item, query_plan.selected_tables)
        ]
        verified_example = self._verified_example(catalog, compact_plan)

        sections = [
            "AUTHORITATIVE ERP SQL CONTEXT",
            f"Context version: {ERP_SQL_CONTEXT_VERSION}",
            f"Catalog version: {semantic_binding.get('catalog_version')}",
            f"ERP schema version: {semantic_binding.get('erp_schema_version')}",
            "",
            "STRUCTURED QUERY PLAN",
            self._json(compact_plan),
            "",
            "VERIFIED BUSINESS DEFINITIONS",
            self._json(business_definitions),
            "",
            "APPROVED RELATIONSHIPS",
            self._json(relationships),
            "",
            "AGGREGATION AND DUPLICATE-SAFETY RULES",
            self._json({
                "aggregation_grain": query_plan.aggregation_grain,
                "duplicate_risks": duplicate_rules,
            }),
            "",
            "CANONICAL PLAN-SCOPED MYSQL DDL",
            canonical_ddl,
        ]
        if verified_example:
            sections.extend([
                "",
                "VERIFIED EXAMPLE",
                self._json(verified_example),
            ])
        sections.extend([
            "",
            "GENERATION BOUNDARY",
            "- The verified business definitions are authoritative.",
            "- DDL describes physical schema only; do not infer business meaning from DDL.",
            "- Use only the tables and columns shown in the canonical DDL.",
            "- Use only approved relationships. Never invent a join.",
            "- Preserve every filter, grouping, ordering, aggregation, and limit in the plan.",
            "- Respect aggregation grain and pre-aggregate whenever a duplicate-risk rule requires it.",
        ])
        return "\n".join(sections)

    def _compact_plan(self, plan):
        return {
            "operation": plan.operation,
            "requested_outputs": plan.requested_outputs,
            "base_entity": plan.base_entity,
            "metrics": plan.metrics,
            "aggregation": plan.aggregation,
            "dimensions": plan.dimensions,
            "filters": plan.filters,
            "resolved_entity_constraints": plan.resolved_entity_constraints,
            "date_constraints": plan.date_constraints,
            "status_constraints": plan.status_constraints,
            "grouping": plan.grouping,
            "ordering": plan.ordering,
            "limit": plan.limit,
            "aggregation_grain": plan.aggregation_grain,
            "unresolved_concepts": plan.unresolved_concepts,
        }

    def _verified_example(self, catalog, compact_plan):
        examples = (
            (catalog.get("verified_semantic_catalog") or {}).get(
                "verified_examples",
                [],
            )
        )
        operation = compact_plan.get("operation")
        metrics = set(compact_plan.get("metrics") or [])
        for example in examples:
            if (
                example.get("verification_status") == "verified"
                and (
                    example.get("operation") == operation
                    or metrics & set(example.get("metrics") or [])
                )
            ):
                return self._pick(
                    example,
                    "question",
                    "operation",
                    "metrics",
                    "sql",
                )
        return None

    def _warning_applies(self, warning, selected_tables):
        referenced = {
            warning.get("header_table"),
            warning.get("detail_table"),
            warning.get("left_table"),
            warning.get("right_table"),
        } - {None}
        return not referenced or bool(referenced & set(selected_tables))

    def _append(self, mapping, table, column):
        if not table or not column or table not in mapping:
            return
        if column not in mapping[table]:
            mapping[table].append(column)

    def _pick(self, item, *keys):
        return {
            key: item.get(key)
            for key in keys
            if item.get(key) not in (None, "", [], {})
        }

    def _column_name(self, column):
        return (
            column.get("column_name")
            or column.get("name")
            or column.get("COLUMN_NAME")
            or ""
        )

    def _quote(self, value):
        return "`" + str(value).replace("`", "``") + "`"

    def _literal(self, value):
        return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

    def _comment(self, value):
        text = " ".join(str(value or "").split())
        return text[:240]

    def _json(self, value):
        return json.dumps(
            value,
            indent=2,
            sort_keys=True,
            default=str,
        )

    def _unique(self, values):
        result = []
        seen = set()
        for value in values:
            if value in (None, ""):
                continue
            key = str(value).lower()
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result


erp_sql_context_builder = ERPSQLContextBuilder()
