import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from relationship_discovery import relationship_discovery


SCHEMA_PROFILER_VERSION = "schema-profiler:v4"
PROFILE_DIR = Path(__file__).resolve().parent / "runtime" / "database_profiles"


@dataclass
class RelationshipProfile:
    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]
    relationship_type: str
    declared: bool
    confidence_score: float
    evidence: list[str] = field(default_factory=list)
    cardinality: str = "unknown"
    verification_status: str = "unverified"
    duplicate_amplification_risk: str = "low"
    required_pre_aggregation: bool = False
    applicable_business_modules: list[str] = field(default_factory=list)

    @property
    def column(self):
        return self.source_columns[0] if self.source_columns else ""

    @property
    def references_table(self):
        return self.target_table

    @property
    def references_column(self):
        return self.target_columns[0] if self.target_columns else ""

    @property
    def confidence(self):
        return "high" if self.confidence_score >= 0.8 else "medium" if self.confidence_score >= 0.55 else "low"


@dataclass
class ColumnProfile:
    # Compatibility fields used by the existing retriever.
    column_name: str
    normalized_name: str
    data_type: str
    nullable: bool
    semantic_type: str | None
    business_terms: list[str] = field(default_factory=list)
    sample_values: list = field(default_factory=list)
    distinct_count: int | None = None
    date_storage_format: str | None = None
    primary_key: bool = False
    indexed: bool = False

    normalized_tokens: list[str] = field(default_factory=list)
    comment: str | None = None
    default_value: object | None = None
    ordinal_position: int | None = None
    semantic_role: str | None = None
    entity_type: str | None = None
    metric_type: str | None = None
    is_foreign_key: bool = False
    is_unique: bool = False
    is_identifier_like: bool = False
    is_name_like: bool = False
    is_status_like: bool = False
    is_date_like: bool = False
    is_amount_like: bool = False
    is_quantity_like: bool = False
    is_percentage_like: bool = False
    is_boolean_like: bool = False
    is_code_like: bool = False
    is_dimension: bool = False
    is_metric: bool = False
    null_fraction: float | None = None
    detected_date_format: str | None = None
    profile_confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class TableProfile:
    # Compatibility fields used by the existing retriever.
    table_name: str
    normalized_name: str
    module: str | None
    description: str | None
    columns: list[ColumnProfile]
    primary_keys: list[str]
    foreign_keys: list[RelationshipProfile]
    row_count: int | None
    sample_values: dict[str, list]
    business_terms: list[str]
    relationship_hints: list[str]
    profile_version: str = SCHEMA_PROFILER_VERSION

    normalized_tokens: list[str] = field(default_factory=list)
    likely_role: str | None = None
    likely_module: str | None = None
    unique_keys: list[list[str]] = field(default_factory=list)
    indexes: list[dict] = field(default_factory=list)
    is_empty: bool | None = None
    business_concepts: list[str] = field(default_factory=list)
    possible_entity_types: list[str] = field(default_factory=list)
    possible_metrics: list[str] = field(default_factory=list)
    possible_dimensions: list[str] = field(default_factory=list)
    sample_summary: dict[str, list[str]] = field(default_factory=dict)
    profile_confidence: float = 0.0
    explanation: list[str] = field(default_factory=list)
    historical_success_count: int = 0


@dataclass
class DatabaseProfile:
    database_id: str
    dialect: str
    schema_fingerprint: str
    modules: list[str]
    tables: list[TableProfile]
    relationships: list[RelationshipProfile]
    profile_version: str = SCHEMA_PROFILER_VERSION
    table_count: int = 0
    generated_from: list[str] = field(default_factory=list)
    historical_query_count: int = 0

    def to_dict(self):
        return asdict(self)


class SchemaProfiler:
    IDENTIFIER_TOKENS = {"id", "identifier", "key", "pk", "uuid", "guid"}
    NAME_TOKENS = {"name", "nm", "title", "label", "description", "desc"}
    STATUS_TOKENS = {"status", "st", "state", "stage", "condition", "approval", "enabled", "active"}
    DATE_TOKENS = {"date", "dt", "time", "tm", "timestamp", "created", "updated", "modified", "posted", "joined", "start", "end", "year", "month", "day", "at", "on"}
    AMOUNT_TOKENS = {"amount", "amt", "value", "price", "cost", "revenue", "income", "expense", "balance", "total", "gross", "net", "tax", "salary", "wage", "charge", "budget"}
    QUANTITY_TOKENS = {"quantity", "qty", "count", "units", "volume", "hours", "weight", "stock", "usage", "consumption"}
    PERCENT_TOKENS = {"percent", "percentage", "pct", "ratio", "margin", "rate"}
    CODE_TOKENS = {"code", "cd", "number", "num", "no", "ref", "reference", "serial", "sku", "barcode"}
    CATEGORY_TOKENS = {"type", "category", "group", "class", "region", "location", "department", "team", "owner"}
    SENSITIVE_TOKENS = {"password", "passwd", "secret", "token", "apikey", "api_key", "credential", "ssn", "pan", "email", "phone", "mobile", "address"}
    WEAK_PREFIXES = {"tbl", "tab", "t", "m", "trn", "txn"}
    WEAK_SUFFIXES = {"mst", "master", "hdr", "header", "dtl", "detail", "det", "txn", "trn", "hist", "history", "archive", "log"}
    NUMERIC_TYPES = re.compile(r"\b(?:tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|number|float|double|real|money)\b", re.I)
    TEXT_TYPES = re.compile(r"(?:char|text|string|enum|clob)", re.I)
    DATE_TYPES = re.compile(r"(?:date|time|timestamp)", re.I)
    BOOLEAN_TYPES = re.compile(r"\b(?:bool|boolean|bit)\b|tinyint\s*\(\s*1\s*\)", re.I)

    def __init__(self, profile_dir: Path = PROFILE_DIR, sample_limit: int = 5):
        self.profile_dir = Path(profile_dir)
        self.sample_limit = max(1, min(int(sample_limit), 10))
        self._cache = {}

    def get_or_build(
        self,
        *,
        database_id: str,
        dialect: str,
        schema: dict,
        schema_fingerprint: str,
        row_counts: dict | None = None,
        catalog: dict | None = None,
        safe_samples: dict | None = None,
        historical_queries: list[dict] | None = None,
    ) -> DatabaseProfile:
        cache_key = f"{dialect}:{schema_fingerprint}"
        verified_history = self._verified_history(historical_queries or [])
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if self._evidence_is_current(cached, safe_samples, historical_queries, verified_history):
                return cached
            self._cache.pop(cache_key, None)
        path = self._path_for(cache_key)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("profile_version") == SCHEMA_PROFILER_VERSION and payload.get("schema_fingerprint") == schema_fingerprint:
                    profile = self.profile_from_dict(payload)
                    if self._evidence_is_current(profile, safe_samples, historical_queries, verified_history):
                        self._cache[cache_key] = profile
                        return profile
            except (OSError, json.JSONDecodeError, TypeError, KeyError):
                pass
        profile = self.build(
            database_id=database_id,
            dialect=dialect,
            schema=schema,
            schema_fingerprint=schema_fingerprint,
            row_counts=row_counts,
            catalog=catalog,
            safe_samples=safe_samples,
            historical_queries=historical_queries,
        )
        if len(schema) <= 500:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(profile.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
        self._cache[cache_key] = profile
        return profile

    def build(
        self,
        *,
        database_id: str,
        dialect: str,
        schema: dict,
        schema_fingerprint: str,
        row_counts: dict | None = None,
        catalog: dict | None = None,
        safe_samples: dict | None = None,
        historical_queries: list[dict] | None = None,
    ) -> DatabaseProfile:
        row_counts = row_counts or {}
        catalog = catalog or {}
        safe_samples = safe_samples or {}
        history = self._verified_history(historical_queries or [])
        normalized_schema = self._normalized_relationship_schema(schema)
        relationships = self._relationship_profiles(normalized_schema, catalog)
        relationships_by_table = self._relationships_by_table(relationships)
        catalog_evidence = self._catalog_evidence(catalog)
        history_counts = self._history_counts(history)
        tables = []
        modules = set()
        for table_name, table_info in schema.items():
            columns = []
            table_samples = safe_samples.get(table_name, table_info.get("sample_values", {})) or {}
            foreign_columns = {
                column
                for relationship in relationships_by_table.get(table_name, [])
                for column in relationship.source_columns
                if relationship.source_table == table_name
            }
            indexes = self._indexes(table_info)
            indexed_columns = {column for index in indexes for column in index.get("columns", [])}
            unique_columns = {column for index in indexes if index.get("unique") for column in index.get("columns", [])}
            primary_keys = []
            for ordinal, raw_column in enumerate(table_info.get("columns", []), start=1):
                name = self._column_name(raw_column)
                sample_info = self._sample_info(table_samples.get(name), raw_column)
                column = self._profile_column(
                    table_name,
                    raw_column,
                    ordinal=ordinal,
                    samples=sample_info["values"],
                    distinct_count=sample_info["distinct_count"],
                    null_fraction=sample_info["null_fraction"],
                    is_foreign_key=name in foreign_columns,
                    is_indexed=name in indexed_columns or bool(raw_column.get("indexed")),
                    is_unique=name in unique_columns or bool(raw_column.get("unique")),
                )
                columns.append(column)
                if column.primary_key:
                    primary_keys.append(name)

            row_count = row_counts.get(table_name, table_info.get("row_count"))
            concepts = self._table_concepts(table_name, table_info, columns, catalog_evidence.get(table_name, set()))
            role, role_evidence = self._table_role(table_name, columns, relationships_by_table.get(table_name, []), row_count)
            module = self._module(table_name, concepts, catalog_evidence.get(table_name, set()))
            if module:
                modules.add(module)
            unique_keys = [index["columns"] for index in indexes if index.get("unique")]
            table_relationships = relationships_by_table.get(table_name, [])
            relationship_hints = [self._relationship_text(item) for item in table_relationships[:12]]
            possible_metrics = sorted({column.metric_type or column.column_name for column in columns if column.is_metric})
            possible_dimensions = sorted({column.column_name for column in columns if column.is_dimension})
            possible_entities = sorted({column.entity_type for column in columns if column.entity_type})
            explanation = self._table_explanation(
                role=role,
                role_evidence=role_evidence,
                columns=columns,
                row_count=row_count,
                relationships=table_relationships,
                concepts=concepts,
                history_count=history_counts.get(table_name, 0),
            )
            confidence = self._table_confidence(columns, row_count, table_relationships, explanation)
            summary = {
                column.column_name: [str(value) for value in column.sample_values]
                for column in columns
                if column.sample_values
            }
            tables.append(TableProfile(
                table_name=table_name,
                normalized_name=" ".join(self.tokenize_identifier(table_name)),
                module=module,
                description=self._description(table_info),
                columns=columns,
                primary_keys=primary_keys,
                foreign_keys=[item for item in table_relationships if item.source_table == table_name],
                row_count=int(row_count) if row_count is not None else None,
                sample_values=summary,
                business_terms=concepts,
                relationship_hints=relationship_hints,
                normalized_tokens=self.tokenize_identifier(table_name),
                likely_role=role,
                likely_module=module,
                unique_keys=unique_keys,
                indexes=indexes,
                is_empty=(int(row_count) == 0) if row_count is not None else None,
                business_concepts=concepts,
                possible_entity_types=possible_entities,
                possible_metrics=possible_metrics,
                possible_dimensions=possible_dimensions,
                sample_summary=summary,
                profile_confidence=confidence,
                explanation=explanation,
                historical_success_count=history_counts.get(table_name, 0),
            ))
        generated_from = ["database_metadata", "identifier_names"]
        if row_counts:
            generated_from.append("row_counts")
        if catalog:
            generated_from.append("business_catalog")
        if safe_samples:
            generated_from.append("safe_samples")
        if history:
            generated_from.append("verified_query_history")
        return DatabaseProfile(
            database_id=database_id,
            dialect=dialect,
            schema_fingerprint=schema_fingerprint,
            modules=sorted(modules),
            tables=tables,
            relationships=relationships,
            table_count=len(tables),
            generated_from=generated_from,
            historical_query_count=len(history),
        )

    def _evidence_is_current(self, profile, safe_samples, historical_queries, verified_history):
        if safe_samples and "safe_samples" not in profile.generated_from:
            return False
        if historical_queries is not None and profile.historical_query_count != len(verified_history):
            return False
        return True

    def tokenize_identifier(self, value: str) -> list[str]:
        text = str(value or "")
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
        text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
        tokens = [token.lower() for token in re.findall(r"[A-Za-z]+|\d+", text)]
        if len(tokens) > 1 and tokens[0] in self.WEAK_PREFIXES:
            tokens = tokens[1:]
        return tokens

    def _profile_column(self, table_name, raw, *, ordinal, samples, distinct_count, null_fraction, is_foreign_key, is_indexed, is_unique):
        name = self._column_name(raw)
        tokens = self.tokenize_identifier(name)
        comment_tokens = self.tokenize_identifier(raw.get("comment") or raw.get("COLUMN_COMMENT") or "")
        token_set = set(tokens) | set(comment_tokens)
        data_type = str(raw.get("data_type") or raw.get("type") or raw.get("DATA_TYPE") or "")
        primary_key = bool(raw.get("primary_key") or raw.get("COLUMN_KEY") == "PRI")
        nullable = not bool(raw.get("not_null") or raw.get("IS_NULLABLE") == "NO")
        sample_strings = self._safe_sample_values(name, samples)
        sample_flags = self._sample_flags(sample_strings)
        is_boolean = bool(self.BOOLEAN_TYPES.search(data_type)) or sample_flags["boolean"]
        is_date = bool(self.DATE_TYPES.search(data_type)) or bool(token_set & self.DATE_TOKENS) or sample_flags["date"]
        is_identifier = primary_key or is_foreign_key or bool(token_set & self.IDENTIFIER_TOKENS)
        is_code = bool(token_set & self.CODE_TOKENS) or sample_flags["code"]
        is_name = bool(token_set & self.NAME_TOKENS)
        is_status = bool(token_set & self.STATUS_TOKENS) or (sample_flags["status"] and not is_name and not is_code)
        numeric = bool(self.NUMERIC_TYPES.search(data_type))
        is_amount = numeric and bool(token_set & self.AMOUNT_TOKENS)
        is_quantity = numeric and bool(token_set & self.QUANTITY_TOKENS)
        is_percentage = numeric and (bool(token_set & self.PERCENT_TOKENS) or sample_flags["percentage"])
        is_metric = is_amount or is_quantity or is_percentage or (numeric and not is_identifier and not is_boolean)
        is_dimension = is_name or is_code or is_status or is_date or is_boolean or bool(token_set & self.CATEGORY_TOKENS)
        semantic_type = self._semantic_type(
            is_identifier, is_name, is_status, is_date, is_amount, is_quantity, is_percentage, is_boolean, is_code
        )
        metric_type = "amount" if is_amount else "quantity" if is_quantity else "percentage" if is_percentage else "numeric_measure" if is_metric else None
        table_tokens = [token for token in self.tokenize_identifier(table_name) if token not in self.WEAK_SUFFIXES]
        entity_type = "_".join(table_tokens[:3]) if (is_name or is_code or primary_key) and table_tokens else None
        evidence = []
        for condition, reason in (
            (primary_key, "primary_key_metadata"), (is_foreign_key, "foreign_key_metadata"),
            (is_indexed, "index_metadata"), (bool(raw.get("comment") or raw.get("COLUMN_COMMENT")), "column_comment"),
            (bool(samples), "safe_sample_evidence"), (bool(token_set), "identifier_name_tokens"),
            (bool(data_type), "database_data_type"),
        ):
            if condition:
                evidence.append(reason)
        confidence = min(0.98, 0.45 + 0.08 * len(evidence) + (0.12 if primary_key or is_foreign_key else 0))
        business_terms = sorted(token_set | ({semantic_type} if semantic_type else set()))
        detected_date = self._date_format(sample_strings) if is_date else None
        return ColumnProfile(
            column_name=name,
            normalized_name=" ".join(tokens),
            data_type=data_type,
            nullable=nullable,
            semantic_type=semantic_type,
            business_terms=business_terms,
            sample_values=sample_strings,
            distinct_count=distinct_count,
            date_storage_format=detected_date or ("native_date_or_datetime" if self.DATE_TYPES.search(data_type) else None),
            primary_key=primary_key,
            indexed=is_indexed or primary_key,
            normalized_tokens=tokens,
            comment=raw.get("comment") or raw.get("COLUMN_COMMENT"),
            default_value=raw.get("default_value", raw.get("COLUMN_DEFAULT")),
            ordinal_position=raw.get("ordinal_position", raw.get("ORDINAL_POSITION", ordinal)),
            semantic_role="metric" if is_metric else "dimension" if is_dimension else "attribute",
            entity_type=entity_type,
            metric_type=metric_type,
            is_foreign_key=is_foreign_key,
            is_unique=is_unique or primary_key,
            is_identifier_like=is_identifier,
            is_name_like=is_name,
            is_status_like=is_status,
            is_date_like=is_date,
            is_amount_like=is_amount,
            is_quantity_like=is_quantity,
            is_percentage_like=is_percentage,
            is_boolean_like=is_boolean,
            is_code_like=is_code,
            is_dimension=is_dimension,
            is_metric=is_metric,
            null_fraction=null_fraction,
            detected_date_format=detected_date,
            profile_confidence=round(confidence, 2),
            evidence=evidence,
        )

    def _semantic_type(self, identifier, name, status, date_like, amount, quantity, percentage, boolean, code):
        for condition, value in (
            (identifier, "identifier"), (name, "name"), (status, "status"), (date_like, "date"),
            (amount, "amount"), (quantity, "quantity"), (percentage, "percentage"),
            (boolean, "boolean"), (code, "code"),
        ):
            if condition:
                return value
        return None

    def _table_role(self, table_name, columns, relationships, row_count):
        tokens = set(self.tokenize_identifier(table_name))
        suffix = next((token for token in tokens if token in self.WEAK_SUFFIXES), None)
        metrics = sum(column.is_metric for column in columns)
        dates = sum(column.is_date_like for column in columns)
        names = sum(column.is_name_like or column.is_code_like for column in columns)
        outgoing = sum(item.source_table == table_name for item in relationships)
        incoming = sum(item.target_table == table_name for item in relationships)
        evidence = []
        if outgoing >= 2 and len(columns) <= 8:
            evidence.append("connects_multiple_entities")
            return "bridge", evidence
        if metrics and dates and outgoing:
            evidence.extend(["contains_metrics", "contains_event_date", "references_other_entities"])
            return "transaction", evidence
        if names and incoming:
            evidence.extend(["descriptive_columns", "referenced_by_other_tables"])
            return "dimension", evidence
        if suffix in {"hist", "history", "archive", "log"}:
            evidence.append("weak_history_naming_hint")
            return "history", evidence
        if suffix in {"hdr", "header"}:
            evidence.append("weak_header_naming_hint")
            return "header", evidence
        if suffix in {"dtl", "detail", "det"}:
            evidence.append("weak_detail_naming_hint")
            return "detail", evidence
        if suffix in {"mst", "master"} and names:
            evidence.extend(["weak_master_naming_hint", "descriptive_columns"])
            return "dimension", evidence
        if metrics:
            evidence.append("contains_numeric_measures")
            return "fact", evidence
        if names:
            evidence.append("contains_descriptive_attributes")
            return "reference", evidence
        evidence.append("metadata_available_but_role_is_general")
        return "general", evidence

    def _table_explanation(self, *, role, role_evidence, columns, row_count, relationships, concepts, history_count):
        explanation = [f"classified as {role}: {', '.join(role_evidence)}"]
        if row_count is not None:
            explanation.append(f"row count metadata: {int(row_count)}")
        semantic = sorted({column.semantic_type for column in columns if column.semantic_type})
        if semantic:
            explanation.append(f"detected column roles: {', '.join(semantic)}")
        if relationships:
            explanation.append(f"relationship evidence: {len(relationships)} connected edge(s)")
        if concepts:
            explanation.append(f"business concepts: {', '.join(concepts[:8])}")
        if history_count:
            explanation.append(f"used by {history_count} verified successful query record(s)")
        return explanation

    def _table_confidence(self, columns, row_count, relationships, explanation):
        score = 0.45
        if columns:
            score += 0.15
        if row_count is not None:
            score += 0.08
        if relationships:
            score += 0.1
        if any(column.comment for column in columns):
            score += 0.05
        if any(column.sample_values for column in columns):
            score += 0.07
        if len(explanation) > 2:
            score += 0.05
        return round(min(0.98, score), 2)

    def _relationship_profiles(self, schema, catalog=None):
        verified_relationships = (catalog or {}).get("join_paths") or []
        if len(schema) > 300:
            return self._large_schema_relationships(schema, verified_relationships)
        profiles = []
        for edge in relationship_discovery.build_graph(schema, verified_relationships=verified_relationships).get("edges", []):
            profiles.append(RelationshipProfile(
                source_table=edge["left_table"],
                source_columns=list(edge.get("left_columns", [])),
                target_table=edge["right_table"],
                target_columns=list(edge.get("right_columns", [])),
                relationship_type=edge.get("join_type", "unknown"),
                declared=bool(edge.get("declared")),
                confidence_score=float(edge.get("confidence_score", 0)) / 100.0,
                evidence=list(edge.get("evidence", [])),
                cardinality=edge.get("cardinality", "unknown"),
                verification_status=edge.get("verification_status", "declared" if edge.get("declared") else "inferred"),
                duplicate_amplification_risk=edge.get("row_multiplication_risk", "low"),
                required_pre_aggregation=bool(edge.get("required_pre_aggregation")),
                applicable_business_modules=list(edge.get("applicable_business_modules") or []),
            ))
        return profiles

    def _large_schema_relationships(self, schema, verified_relationships=None):
        profiles = []
        verified_relationships = verified_relationships or []
        for edge in relationship_discovery.build_graph(schema, verified_relationships=verified_relationships).get("edges", []):
            if "verified_erp_catalog" not in edge.get("evidence", []):
                continue
            profiles.append(RelationshipProfile(
                edge["left_table"], list(edge.get("left_columns", [])),
                edge["right_table"], list(edge.get("right_columns", [])),
                edge.get("join_type", "verified_catalog"), False,
                float(edge.get("confidence_score", 0)) / 100.0,
                list(edge.get("evidence", [])),
                edge.get("cardinality", "unknown"),
                edge.get("verification_status", "verified"),
                edge.get("row_multiplication_risk", "low"),
                bool(edge.get("required_pre_aggregation")),
                list(edge.get("applicable_business_modules") or []),
            ))
        primary_owners = {}
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = self._column_name(column)
                if column.get("primary_key") or column.get("COLUMN_KEY") == "PRI":
                    primary_owners.setdefault(name.lower(), []).append((table, name))
            for foreign_key in info.get("foreign_keys", []):
                source = foreign_key.get("column")
                target_table = foreign_key.get("table")
                target_column = foreign_key.get("references")
                if source and target_table and target_column:
                    profiles.append(RelationshipProfile(
                        table, [source], target_table, [target_column], "foreign_key", True, 0.95,
                        ["declared_foreign_key"],
                        "many_to_one",
                        "declared",
                        "low",
                        False,
                        [],
                    ))
        existing = {(item.source_table, tuple(item.source_columns), item.target_table) for item in profiles}
        for table, info in schema.items():
            for column in info.get("columns", []):
                name = self._column_name(column)
                if not name.lower().endswith("_id") or column.get("primary_key"):
                    continue
                owners = [owner for owner in primary_owners.get(name.lower(), []) if owner[0] != table]
                if len(owners) != 1:
                    continue
                target_table, target_column = owners[0]
                key = (table, (name,), target_table)
                if key in existing:
                    continue
                profiles.append(RelationshipProfile(
                    table, [name], target_table, [target_column], "inferred", False, 0.65,
                    ["shared_identifier_name", "primary_key_evidence", "large_schema_bounded_inference"],
                    "unknown",
                    "inferred",
                    "medium",
                    True,
                    [],
                ))
        return profiles

    def _normalized_relationship_schema(self, schema):
        normalized = {}
        for table, info in schema.items():
            foreign_keys = []
            for foreign_key in info.get("foreign_keys", []):
                foreign_keys.append({
                    "column": foreign_key.get("column"),
                    "table": foreign_key.get("table") or foreign_key.get("references_table"),
                    "references": foreign_key.get("references") or foreign_key.get("references_column"),
                })
            normalized[table] = {**info, "foreign_keys": foreign_keys}
        return normalized

    def _relationships_by_table(self, relationships):
        result = {}
        for relationship in relationships:
            result.setdefault(relationship.source_table, []).append(relationship)
            result.setdefault(relationship.target_table, []).append(relationship)
        return result

    def _catalog_evidence(self, catalog):
        result = {}
        for concept, detail in catalog.get("entities", {}).items():
            for table in detail.get("tables", []):
                result.setdefault(table.get("table"), set()).add(concept)
        return result

    def _table_concepts(self, table_name, table_info, columns, catalog_terms):
        terms = set(self.tokenize_identifier(table_name)) | set(catalog_terms)
        terms.update(token for column in columns for token in column.business_terms)
        terms.update(self.tokenize_identifier(self._description(table_info) or ""))
        return sorted(term for term in terms if term not in self.WEAK_PREFIXES | self.WEAK_SUFFIXES)[:60]

    def _module(self, table_name, concepts, catalog_terms):
        if catalog_terms:
            return sorted(catalog_terms)[0]
        tokens = [token for token in self.tokenize_identifier(table_name) if token not in self.WEAK_PREFIXES | self.WEAK_SUFFIXES]
        if not tokens:
            tokens = [term for term in concepts if term]
        return tokens[0] if tokens else None

    def _indexes(self, table_info):
        indexes = []
        for index in table_info.get("indexes", []):
            columns = list(index.get("columns", []))
            if columns:
                indexes.append({"name": index.get("name"), "columns": columns, "unique": bool(index.get("unique"))})
        for constraint in table_info.get("constraints", []):
            if str(constraint.get("type", "")).lower() in {"unique", "primary key"}:
                columns = list(constraint.get("columns", []))
                if columns and not any(item["columns"] == columns for item in indexes):
                    indexes.append({"name": constraint.get("name"), "columns": columns, "unique": True})
        return indexes

    def _sample_info(self, value, raw_column):
        if isinstance(value, dict):
            return {
                "values": value.get("values", []),
                "distinct_count": value.get("distinct_count", raw_column.get("distinct_count")),
                "null_fraction": value.get("null_fraction", raw_column.get("null_fraction")),
            }
        return {
            "values": value or raw_column.get("sample_values", []),
            "distinct_count": raw_column.get("distinct_count"),
            "null_fraction": raw_column.get("null_fraction"),
        }

    def _safe_sample_values(self, column_name, values):
        if set(self.tokenize_identifier(column_name)) & self.SENSITIVE_TOKENS:
            return []
        result = []
        for value in list(values or [])[: self.sample_limit]:
            if value is None or isinstance(value, (dict, list, tuple, set, bytes, bytearray)):
                continue
            text = str(value)[:64]
            if text not in result:
                result.append(text)
        return result

    def _sample_flags(self, values):
        lowered = {value.strip().lower() for value in values if value.strip()}
        boolean = bool(lowered) and lowered <= {"0", "1", "true", "false", "yes", "no", "y", "n"}
        date_like = bool(values) and sum(bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}(?:[ t].*)?|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", value.strip())) for value in values) >= max(1, len(values) // 2)
        percentage = bool(values) and all(re.fullmatch(r"-?\d+(?:\.\d+)?%?", value.strip()) for value in values) and any("%" in value for value in values)
        code = bool(values) and sum(bool(re.fullmatch(r"[A-Za-z]{1,10}[-/.]?\d+[A-Za-z0-9/-]*", value.strip())) for value in values) >= max(1, len(values) // 2)
        status = 1 < len(lowered) <= 12 and all(len(value) <= 32 for value in lowered) and not date_like and not code
        return {"boolean": boolean, "date": date_like, "percentage": percentage, "code": code, "status": status}

    def _date_format(self, values):
        for value in values:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ t].*)?", value):
                return "YYYY-MM-DD"
            if re.fullmatch(r"\d{2}-\d{2}-\d{4}", value):
                return "DD-MM-YYYY"
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
                return "DD/MM/YYYY_or_MM/DD/YYYY"
        return None

    def _verified_history(self, records):
        return [
            record for record in records
            if record.get("validated") is True
            and record.get("execution_success") is True
            and float(record.get("confidence", 100)) >= 70
        ]

    def _history_counts(self, records):
        counts = {}
        for record in records:
            tables = list(record.get("selected_tables", []))
            if not tables:
                tables = re.findall(r"\b(?:from|join)\s+[`\[]?([A-Za-z_][\w.]*)", str(record.get("sql", "")), re.I)
            for table in set(tables):
                counts[table] = counts.get(table, 0) + 1
        return counts

    def _description(self, table_info):
        return table_info.get("description") or table_info.get("comment") or table_info.get("TABLE_COMMENT")

    def _column_name(self, column):
        return column.get("column_name") or column.get("name") or column.get("COLUMN_NAME") or ""

    def _relationship_text(self, relationship):
        safety = ""
        if relationship.duplicate_amplification_risk != "low" or relationship.required_pre_aggregation:
            safety = (
                f", duplicate_risk={relationship.duplicate_amplification_risk}, "
                f"pre_aggregate={relationship.required_pre_aggregation}"
            )
        return (
            f"{relationship.source_table}.{','.join(relationship.source_columns)} -> "
            f"{relationship.target_table}.{','.join(relationship.target_columns)} "
            f"({relationship.relationship_type}, confidence={relationship.confidence_score:.2f}, "
            f"cardinality={relationship.cardinality}{safety})"
        )

    def _path_for(self, key):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return self.profile_dir / f"{safe}.json"

    def profile_from_dict(self, payload):
        return DatabaseProfile(
            database_id=payload["database_id"],
            dialect=payload["dialect"],
            schema_fingerprint=payload["schema_fingerprint"],
            modules=payload.get("modules", []),
            tables=[self.table_from_dict(item) for item in payload.get("tables", [])],
            relationships=[RelationshipProfile(**item) for item in payload.get("relationships", [])],
            profile_version=payload.get("profile_version", SCHEMA_PROFILER_VERSION),
            table_count=payload.get("table_count", len(payload.get("tables", []))),
            generated_from=payload.get("generated_from", []),
            historical_query_count=payload.get("historical_query_count", 0),
        )

    def table_from_dict(self, value):
        return TableProfile(
            **{
                **value,
                "columns": [ColumnProfile(**column) for column in value.get("columns", [])],
                "foreign_keys": [RelationshipProfile(**item) for item in value.get("foreign_keys", [])],
            }
        )


schema_profiler = SchemaProfiler()
