import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from schema_profiler import DatabaseProfile, SchemaProfiler


ERP_DISCOVERY_VERSION = "erp-discovery:v3"
DISCOVERY_DIR = Path(__file__).resolve().parent / "runtime" / "erp_discovery"


@dataclass
class ERPDiscoveryReport:
    database_id: str
    dialect: str
    schema_fingerprint: str
    profile_version: str
    discovery_version: str
    discovered_modules: list[str]
    important_tables_per_module: dict[str, list[dict[str, Any]]]
    table_classes: dict[str, list[dict[str, Any]]]
    likely_relationships: list[dict[str, Any]]
    low_confidence_relationships: list[dict[str, Any]]
    duplicate_risk_relationships: list[dict[str, Any]]
    ambiguous_mappings: list[dict[str, Any]]
    missing_business_definitions: list[str]
    excluded_from_retrieval: list[dict[str, Any]]
    column_inventory: dict[str, list[dict[str, Any]]]
    population_summary: dict[str, Any]
    generated_from: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ERPDiscovery:
    TECHNICAL_TABLE = re.compile(
        r"(?:hist|history|archive|audit|log|logs|attachment|attachments|tmp|temp|backup|notification|approval_hist)",
        re.IGNORECASE,
    )
    DETAIL_TABLE = re.compile(r"(?:dtl|detail|details|items|lines|boq|description|discription)$", re.IGNORECASE)
    HEADER_TABLE = re.compile(r"(?:hdr|header|master|mst|form|raising|note|order)$", re.IGNORECASE)
    LOOKUP_HINT = re.compile(r"(?:type|category|status|state|reason|unit|uom|group|stage|mode|terms?)", re.IGNORECASE)

    def __init__(self, discovery_dir: Path = DISCOVERY_DIR):
        self.discovery_dir = Path(discovery_dir)

    def get_or_build(
        self,
        *,
        profile: DatabaseProfile,
        catalog: dict | None = None,
    ) -> ERPDiscoveryReport:
        path = self._path_for(profile.schema_fingerprint)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    payload.get("schema_fingerprint") == profile.schema_fingerprint
                    and payload.get("profile_version") == profile.profile_version
                    and payload.get("discovery_version") == ERP_DISCOVERY_VERSION
                ):
                    return self.from_dict(payload)
            except (OSError, json.JSONDecodeError, TypeError):
                pass

        report = self.build(profile=profile, catalog=catalog or {})
        self.discovery_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
        return report

    def build(
        self,
        *,
        profile: DatabaseProfile,
        catalog: dict | None = None,
    ) -> ERPDiscoveryReport:
        catalog = catalog or {}
        tables = list(profile.tables)
        relationships = list(profile.relationships)
        table_classes = {
            "master_tables": [],
            "transaction_header_tables": [],
            "transaction_detail_tables": [],
            "lookup_tables": [],
            "mapping_bridge_tables": [],
            "history_archive_log_temp_tables": [],
            "populated_tables": [],
            "empty_tables": [],
        }
        column_inventory = {
            "primary_keys": [],
            "declared_foreign_keys": [],
            "entity_identifier_columns": [],
            "entity_display_name_columns": [],
            "numeric_metrics": [],
            "quantity_columns": [],
            "amount_columns": [],
            "date_columns": [],
            "status_columns": [],
        }

        for table in tables:
            entry = self._table_entry(table)
            if table.row_count is not None and table.row_count > 0:
                table_classes["populated_tables"].append(entry)
            if table.row_count == 0:
                table_classes["empty_tables"].append(entry)

            for class_name in self._classes_for(table):
                table_classes[class_name].append(entry)

            for column in table.columns:
                column_entry = self._column_entry(table.table_name, column)
                if column.primary_key:
                    column_inventory["primary_keys"].append(column_entry)
                if column.is_foreign_key:
                    column_inventory["declared_foreign_keys"].append(column_entry)
                if column.is_identifier_like or column.is_code_like:
                    column_inventory["entity_identifier_columns"].append(column_entry)
                if column.is_name_like:
                    column_inventory["entity_display_name_columns"].append(column_entry)
                if column.is_metric:
                    column_inventory["numeric_metrics"].append(column_entry)
                if column.is_quantity_like:
                    column_inventory["quantity_columns"].append(column_entry)
                if column.is_amount_like:
                    column_inventory["amount_columns"].append(column_entry)
                if column.is_date_like:
                    column_inventory["date_columns"].append(column_entry)
                if column.is_status_like:
                    column_inventory["status_columns"].append(column_entry)

        catalog_modules = list((catalog.get("entities") or {}).keys())
        discovered_modules = sorted(set(catalog_modules or profile.modules))
        important_tables_per_module = self._important_tables_per_module(tables, discovered_modules)
        likely_relationships = [self._relationship_entry(item) for item in relationships[:300]]
        low_confidence = [
            self._relationship_entry(item)
            for item in relationships
            if item.confidence_score < 0.7
        ][:100]
        duplicate_risks = self._duplicate_risks(relationships)
        ambiguous_mappings = self._ambiguous_mappings(tables, catalog)
        excluded = self._excluded_tables(tables)
        missing_definitions = self._missing_definitions(profile, catalog, ambiguous_mappings, low_confidence)

        population = {
            "table_count": profile.table_count,
            "populated_table_count": len(table_classes["populated_tables"]),
            "empty_table_count": len(table_classes["empty_tables"]),
            "populated_ratio": round(
                len(table_classes["populated_tables"]) / max(1, profile.table_count),
                4,
            ),
        }

        return ERPDiscoveryReport(
            database_id=profile.database_id,
            dialect=profile.dialect,
            schema_fingerprint=profile.schema_fingerprint,
            profile_version=profile.profile_version,
            discovery_version=ERP_DISCOVERY_VERSION,
            discovered_modules=discovered_modules,
            important_tables_per_module=important_tables_per_module,
            table_classes={key: self._top_items(value, 100) for key, value in table_classes.items()},
            likely_relationships=likely_relationships,
            low_confidence_relationships=low_confidence,
            duplicate_risk_relationships=duplicate_risks,
            ambiguous_mappings=ambiguous_mappings,
            missing_business_definitions=missing_definitions,
            excluded_from_retrieval=excluded,
            column_inventory={key: self._top_items(value, 200) for key, value in column_inventory.items()},
            population_summary=population,
            generated_from=[
                *profile.generated_from,
                "schema_profile",
                "business_catalog" if catalog else "no_business_catalog",
            ],
        )

    def from_dict(self, payload: dict[str, Any]) -> ERPDiscoveryReport:
        return ERPDiscoveryReport(**payload)

    def _classes_for(self, table) -> list[str]:
        name = table.table_name
        role = table.likely_role or ""
        classes = []
        if self.TECHNICAL_TABLE.search(name) or role == "history":
            classes.append("history_archive_log_temp_tables")
        if role == "bridge":
            classes.append("mapping_bridge_tables")
        if role in {"dimension", "reference"} and not self.TECHNICAL_TABLE.search(name):
            classes.append("master_tables")
        if role == "header" or (role == "transaction" and self.HEADER_TABLE.search(name)):
            classes.append("transaction_header_tables")
        if role == "detail" or (role == "transaction" and self.DETAIL_TABLE.search(name)):
            classes.append("transaction_detail_tables")
        if (
            role == "reference"
            or self.LOOKUP_HINT.search(name)
            or (
                table.row_count is not None
                and table.row_count <= 200
                and any(column.is_status_like or column.is_code_like for column in table.columns)
            )
        ):
            classes.append("lookup_tables")
        if role == "fact" and self.DETAIL_TABLE.search(name):
            classes.append("transaction_detail_tables")
        return list(dict.fromkeys(classes))

    def _table_entry(self, table) -> dict[str, Any]:
        return {
            "table": table.table_name,
            "module": table.module or table.likely_module,
            "role": table.likely_role,
            "row_count": table.row_count,
            "primary_keys": table.primary_keys,
            "important_columns": self._important_columns(table),
            "confidence": table.profile_confidence,
            "reasons": table.explanation[:5],
        }

    def _important_columns(self, table) -> list[str]:
        selected = []
        for column in table.columns:
            if (
                column.primary_key
                or column.is_foreign_key
                or column.is_name_like
                or column.is_amount_like
                or column.is_quantity_like
                or column.is_date_like
                or column.is_status_like
            ):
                selected.append(column.column_name)
        return selected[:16]

    def _column_entry(self, table_name, column) -> dict[str, Any]:
        return {
            "table": table_name,
            "column": column.column_name,
            "semantic_type": column.semantic_type,
            "data_type": column.data_type,
            "distinct_count": column.distinct_count,
            "sample_values": column.sample_values[:5],
            "confidence": column.profile_confidence,
            "evidence": column.evidence[:5],
        }

    def _relationship_entry(self, relationship) -> dict[str, Any]:
        return {
            "source_table": relationship.source_table,
            "source_columns": relationship.source_columns,
            "target_table": relationship.target_table,
            "target_columns": relationship.target_columns,
            "relationship_type": relationship.relationship_type,
            "declared": relationship.declared,
            "confidence_score": relationship.confidence_score,
            "evidence": relationship.evidence[:6],
        }

    def _important_tables_per_module(self, tables, discovered_modules) -> dict[str, list[dict[str, Any]]]:
        modules = {}
        for table in tables:
            module = self._report_module(table, discovered_modules)
            score = self._importance_score(table)
            modules.setdefault(module, []).append((score, table))
        return {
            module: [
                self._table_entry(table)
                for _score, table in sorted(items, key=lambda item: (-item[0], item[1].table_name))[:12]
            ]
            for module, items in sorted(modules.items())
        }

    def _report_module(self, table, discovered_modules) -> str:
        concepts = set(table.business_concepts or [])
        for module in discovered_modules:
            if module in concepts:
                return module
        return "other"

    def _importance_score(self, table) -> float:
        score = table.profile_confidence
        score += min(1.0, (table.row_count or 0) / 10000.0)
        score += 0.3 if table.primary_keys else 0
        score += 0.2 if table.relationship_hints else 0
        score += 0.25 if table.historical_success_count else 0
        if self.TECHNICAL_TABLE.search(table.table_name):
            score -= 0.5
        if table.row_count == 0:
            score -= 0.3
        return score

    def _duplicate_risks(self, relationships) -> list[dict[str, Any]]:
        by_source = {}
        by_column_name = {}
        for relationship in relationships:
            source_key = (relationship.source_table, tuple(relationship.source_columns))
            by_source.setdefault(source_key, set()).add((relationship.target_table, tuple(relationship.target_columns)))
            for column in relationship.source_columns:
                by_column_name.setdefault(column.lower(), set()).add((relationship.source_table, relationship.target_table))

        risks = []
        for (table, columns), targets in by_source.items():
            if len(targets) > 1:
                risks.append({
                    "risk_type": "same_source_column_multiple_targets",
                    "source_table": table,
                    "source_columns": list(columns),
                    "target_count": len(targets),
                    "targets": sorted([{"table": target[0], "columns": list(target[1])} for target in targets], key=lambda item: item["table"])[:10],
                })
        for column, pairs in by_column_name.items():
            if len(pairs) >= 4:
                risks.append({
                    "risk_type": "widely_reused_join_column_name",
                    "column": column,
                    "relationship_count": len(pairs),
                    "examples": sorted([{"source_table": pair[0], "target_table": pair[1]} for pair in pairs], key=lambda item: (item["source_table"], item["target_table"]))[:10],
                })
        return risks[:100]

    def _ambiguous_mappings(self, tables, catalog) -> list[dict[str, Any]]:
        mappings = []
        for concept, detail in (catalog.get("entities") or {}).items():
            concept_tables = [
                item.get("table")
                for item in detail.get("tables", [])
                if item.get("table")
            ]
            if len(concept_tables) > 8:
                mappings.append({
                    "concept": concept,
                    "reason": "many_tables_match_same_business_concept",
                    "table_count": len(concept_tables),
                    "tables": concept_tables[:20],
                })

        by_module = {}
        for table in tables:
            module = table.module or table.likely_module
            if module:
                by_module.setdefault(module, []).append(table)
        for module, module_tables in by_module.items():
            active = [table for table in module_tables if not self.TECHNICAL_TABLE.search(table.table_name) and table.row_count != 0]
            if len(active) > 20:
                mappings.append({
                    "concept": module,
                    "reason": "module_has_many_populated_candidate_tables",
                    "table_count": len(active),
                    "tables": [table.table_name for table in sorted(active, key=lambda item: item.table_name)[:20]],
                })
        return mappings[:100]

    def _missing_definitions(self, profile, catalog, ambiguous, low_confidence) -> list[str]:
        definitions = []
        if not any(item.declared for item in profile.relationships):
            definitions.append("No declared foreign keys were found; join paths rely on inferred relationships.")
        if low_confidence:
            definitions.append("Some relationships are low confidence and need business validation before KPI use.")
        if ambiguous:
            definitions.append("Some business concepts map to many possible tables and need preferred source definitions.")
        unsupported = catalog.get("unsupported_kpis") or []
        for item in unsupported[:10]:
            definitions.append(f"Unsupported or incomplete KPI: {item}")
        if not definitions:
            definitions.append("No major missing business definitions detected from available metadata.")
        return list(dict.fromkeys(definitions))

    def _excluded_tables(self, tables) -> list[dict[str, Any]]:
        excluded = []
        for table in tables:
            reasons = []
            if table.row_count == 0:
                reasons.append("empty_table")
            if self.TECHNICAL_TABLE.search(table.table_name):
                reasons.append("technical_history_log_attachment_or_temp_table")
            if table.likely_role == "history":
                reasons.append("history_role")
            if table.table_name.lower().startswith(("tmp", "temp")):
                reasons.append("temporary_table_name")
            if reasons:
                excluded.append({
                    "table": table.table_name,
                    "module": table.module or table.likely_module,
                    "row_count": table.row_count,
                    "reasons": reasons,
                })
        return sorted(excluded, key=lambda item: (0 if item["row_count"] == 0 else 1, item["table"]))[:300]

    def _top_items(self, values, limit):
        return sorted(
            values,
            key=lambda item: (
                -(item.get("row_count") or 0),
                -float(item.get("confidence") or 0),
                item.get("table", ""),
                item.get("column", ""),
            ),
        )[:limit]

    def _path_for(self, schema_fingerprint: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", schema_fingerprint)
        return self.discovery_dir / f"{safe}.json"


def build_discovery_from_schema(
    *,
    database_id: str,
    dialect: str,
    schema: dict,
    schema_fingerprint: str,
    row_counts: dict | None = None,
    catalog: dict | None = None,
    safe_samples: dict | None = None,
    historical_queries: list[dict] | None = None,
    profile_dir: Path | None = None,
    discovery_dir: Path | None = None,
) -> ERPDiscoveryReport:
    profiler = SchemaProfiler(profile_dir=profile_dir or SchemaProfiler().profile_dir)
    profile = profiler.get_or_build(
        database_id=database_id,
        dialect=dialect,
        schema=schema,
        schema_fingerprint=schema_fingerprint,
        row_counts=row_counts,
        catalog=catalog,
        safe_samples=safe_samples,
        historical_queries=historical_queries,
    )
    return ERPDiscovery(discovery_dir=discovery_dir or DISCOVERY_DIR).get_or_build(
        profile=profile,
        catalog=catalog,
    )


erp_discovery = ERPDiscovery()
