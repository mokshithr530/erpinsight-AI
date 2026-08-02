import math
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from schema_profiler import (
    ColumnProfile,
    DatabaseProfile,
    RelationshipProfile,
    SchemaProfiler,
    TableProfile,
)


SCHEMA_RETRIEVER_VERSION = "schema-retriever:v5"
PROFILE_DIR = Path(__file__).resolve().parent / "runtime" / "schema_profiles"


ForeignKeyProfile = RelationshipProfile


@dataclass(frozen=True)
class RetrievalWeights:
    semantic_similarity: float = 0.15
    schema_metadata: float = 0.09
    business_concepts: float = 0.10
    entity_matches: float = 0.14
    relationship_connectivity: float = 0.08
    metric_compatibility: float = 0.10
    dimension_compatibility: float = 0.08
    date_compatibility: float = 0.06
    status_compatibility: float = 0.05
    table_population: float = 0.07
    historical_usage: float = 0.08

    def merged(self, overrides: dict | None = None):
        values = asdict(self)
        for key, value in (overrides or {}).items():
            if key in values:
                values[key] = max(0.0, float(value))
        total = sum(values.values()) or 1.0
        return {key: value / total for key, value in values.items()}


@dataclass
class RankedTable:
    table: str
    score: float
    reasons: list[str]
    signal_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class RankedColumn:
    table: str
    column: str
    score: float
    roles: list[str]
    reasons: list[str]
    matched_requirements: list[str] = field(default_factory=list)


@dataclass
class RankedRelationshipPath:
    tables: list[str]
    score: float
    declared_edge_count: int
    inferred_edge_count: int
    reasons: list[str]
    warnings: list[str] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    duplicate_amplification_risk: str = "low"
    required_pre_aggregation: bool = False
    applicable_business_modules: list[str] = field(default_factory=list)


@dataclass
class SchemaRetrievalResult:
    # Existing fields remain stable for current adapters and prompt callers.
    ranked_tables: list[RankedTable]
    relevant_columns: dict[str, list[str]]
    bridge_tables: list[str]
    retrieval_confidence: float
    reasons: list[str]

    selected_tables: list[str] = field(default_factory=list)
    ranked_columns: dict[str, list[RankedColumn]] = field(default_factory=dict)
    relationship_paths: list[RankedRelationshipPath] = field(default_factory=list)
    unresolved_concepts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_constraints: dict = field(default_factory=dict)
    constraint_coverage: dict[str, dict] = field(default_factory=dict)
    fallback_required: bool = False
    retrieval_latency_ms: float = 0.0
    context_character_count: int = 0
    context_estimated_tokens: int = 0
    version: str = SCHEMA_RETRIEVER_VERSION

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class _Requirement:
    key: str
    role: str
    concept: str
    source: str
    raw: str


class SchemaRetriever:
    """Generic, deterministic retrieval over cached database profiles."""

    STOP_WORDS = {
        "a", "an", "and", "are", "as", "at", "be", "between", "by", "for",
        "from", "give", "how", "in", "is", "me", "of", "on", "or", "show",
        "tell", "that", "the", "there", "to", "what", "which", "who", "with",
    }
    ROLE_THRESHOLDS = {
        "metric": 0.34,
        "date": 0.32,
        "status": 0.27,
        "entity": 0.50,
        "output": 0.36,
        "dimension": 0.36,
        "grouping": 0.36,
        "ordering": 0.30,
        "filter": 0.32,
    }
    COLUMN_WEIGHTS = {
        "lexical": 0.20,
        "question": 0.14,
        "role": 0.20,
        "catalog": 0.12,
        "entity": 0.18,
        "metadata": 0.06,
        "table_context": 0.10,
    }
    CONCEPT_EXPANSIONS = {
        "amount_value": {
            "amount", "value", "total", "gross", "net", "price", "cost",
            "grand", "subtotal", "balance", "final", "rate",
        },
        "location": {"location", "state", "city", "country", "region", "territory"},
        "purchase_order": {"purchase", "order", "po", "workorder", "work", "indent"},
        "material": {"material", "item", "product", "submaterial", "stock"},
        "vendor": {"vendor", "supplier", "contractor", "party"},
        "client": {"client", "customer", "party"},
        "billing": {"billing", "bill", "invoice", "gross", "raising", "amount", "value"},
        "project": {"project", "site", "work", "job"},
    }

    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        *,
        weights: dict | None = None,
        column_limit: int = 12,
        max_bridge_tables: int = 4,
        relationship_threshold: float = 0.42,
    ):
        self.profile_dir = Path(profile_dir)
        self.weights = RetrievalWeights().merged(weights)
        self.column_limit = max(4, min(int(column_limit), 24))
        self.max_bridge_tables = max(0, min(int(max_bridge_tables), 8))
        self.relationship_threshold = max(0.0, min(float(relationship_threshold), 1.0))
        self._profile_cache: dict[str, dict[str, TableProfile]] = {}
        self._database_profile_cache: dict[str, DatabaseProfile] = {}
        self.profiler = SchemaProfiler(profile_dir=self.profile_dir / "database_profiles")

    def profiles_for(
        self,
        *,
        schema: dict,
        row_counts: dict,
        catalog: dict,
        schema_fingerprint: str,
        safe_samples: dict | None = None,
        historical_queries: list[dict] | None = None,
    ) -> dict[str, TableProfile]:
        database_profile = self.profiler.get_or_build(
            database_id=schema_fingerprint,
            dialect=catalog.get("source_type", "database"),
            schema=schema,
            schema_fingerprint=schema_fingerprint,
            row_counts=row_counts,
            catalog=catalog,
            safe_samples=safe_samples,
            historical_queries=historical_queries,
        )
        profiles = {profile.table_name: profile for profile in database_profile.tables}
        self._profile_cache[schema_fingerprint] = profiles
        self._database_profile_cache[schema_fingerprint] = database_profile
        return profiles

    def retrieve(
        self,
        *,
        question: str,
        schema: dict,
        row_counts: dict,
        catalog: dict,
        schema_fingerprint: str,
        semantic_plan: dict | None = None,
        entity_resolution: dict | None = None,
        candidate_limit: int = 10,
        safe_samples: dict | None = None,
        historical_queries: list[dict] | None = None,
    ) -> SchemaRetrievalResult:
        started = time.perf_counter()
        profiles = self.profiles_for(
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=schema_fingerprint,
            safe_samples=safe_samples,
            historical_queries=historical_queries,
        )
        semantic_plan = semantic_plan or {}
        entity_resolution = entity_resolution or {}
        requirements = self._requirements(semantic_plan, entity_resolution)
        required_constraints = self._required_constraints(semantic_plan)
        question_terms = self._terms(question)
        resolved_columns, resolved_tables = self._resolved_locations(entity_resolution)
        relationships = self._database_profile_cache[schema_fingerprint].relationships
        connectivity = self._connectivity(relationships)

        requirement_columns = self._rank_requirement_columns(
            requirements=requirements,
            profiles=profiles,
            question_terms=question_terms,
            catalog=catalog,
            resolved_columns=resolved_columns,
        )
        ranked_tables = []
        table_requirement_scores = {}
        for table, profile in profiles.items():
            per_requirement = {
                requirement.key: self._best_column_score(requirement_columns, requirement.key, table)
                for requirement in requirements
            }
            table_requirement_scores[table] = per_requirement
            score, reasons, signals = self._score_table(
                profile,
                requirements=requirements,
                question_terms=question_terms,
                per_requirement=per_requirement,
                resolved_tables=resolved_tables,
                connectivity=connectivity.get(table, 0),
                catalog=catalog,
            )
            if score > 0.08 or table in resolved_tables:
                ranked_tables.append(RankedTable(
                    table=table,
                    score=round(score, 3),
                    reasons=reasons,
                    signal_scores={key: round(value, 3) for key, value in signals.items()},
                ))
        ranked_tables.sort(key=lambda item: (-item.score, item.table))
        candidate_limit = max(1, min(int(candidate_limit), 20))
        candidates = ranked_tables[:candidate_limit]
        candidate_names = {item.table for item in candidates}
        for table in sorted(resolved_tables):
            if table in profiles and table not in candidate_names:
                source = next(item for item in ranked_tables if item.table == table)
                candidates.append(source)
                candidate_names.add(table)

        core_tables, mapped_requirements = self._select_minimum_tables(
            requirements=requirements,
            candidates=candidates,
            table_requirement_scores=table_requirement_scores,
            resolved_tables=resolved_tables,
            candidate_limit=candidate_limit,
        )
        analytic_operations = {
            "aggregation", "average", "bottom_n", "comparison", "count", "distribution",
            "maximum", "minimum", "ranking", "sum", "top_n", "trend",
        }
        top_candidate = candidates[0].table if candidates else None
        if (
            top_candidate
            and top_candidate not in core_tables
            and (
                semantic_plan.get("operation") in analytic_operations
                or set(semantic_plan.get("sub_operations") or []) & analytic_operations
            )
            and any(column.is_metric for column in profiles[top_candidate].columns)
        ):
            core_tables.insert(0, top_candidate)
        relationship_paths, bridge_tables, relationship_unresolved, relationship_warnings = self._relationship_paths(
            core_tables,
            relationships,
            candidates,
        )
        selected_tables = list(dict.fromkeys([*core_tables, *bridge_tables]))
        if not selected_tables and candidates:
            selected_tables = [candidates[0].table]

        relevant_columns, ranked_columns, coverage, unresolved, column_warnings = self._select_columns(
            requirements=requirements,
            selected_tables=selected_tables,
            bridge_tables=set(bridge_tables),
            profiles=profiles,
            requirement_columns=requirement_columns,
            relationships=relationship_paths,
            pre_mapped=mapped_requirements,
            catalog=catalog,
        )
        unresolved = list(dict.fromkeys([*unresolved, *relationship_unresolved]))
        warnings = list(dict.fromkeys([*column_warnings, *relationship_warnings]))
        confidence, confidence_reasons = self._confidence(
            requirements=requirements,
            coverage=coverage,
            selected_tables=selected_tables,
            candidates=candidates,
            relationship_paths=relationship_paths,
            unresolved=unresolved,
            warnings=warnings,
        )
        reasons = [
            f"mapped {sum(item.get('mapped', False) for item in coverage.values())} of {len(requirements)} schema requirements",
            f"selected {len(selected_tables)} table(s) from {len(schema)} profiled table(s)",
            "table, column, and relationship evidence ranked independently",
            *confidence_reasons,
        ]
        result = SchemaRetrievalResult(
            ranked_tables=candidates,
            relevant_columns=relevant_columns,
            bridge_tables=bridge_tables,
            retrieval_confidence=confidence,
            reasons=list(dict.fromkeys(reasons)),
            selected_tables=selected_tables,
            ranked_columns=ranked_columns,
            relationship_paths=relationship_paths,
            unresolved_concepts=unresolved,
            warnings=warnings,
            required_constraints=required_constraints,
            constraint_coverage=coverage,
            fallback_required=confidence < 0.55 or bool(unresolved),
        )
        prompt = self.compact_prompt(result, profiles)
        result.context_character_count = len(prompt)
        result.context_estimated_tokens = max(1, math.ceil(len(prompt) / 4))
        result.retrieval_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    def compact_prompt(self, result: SchemaRetrievalResult, profiles: dict[str, TableProfile]) -> str:
        lines = [
            "Retrieved compact schema context:",
            f"- confidence: {result.retrieval_confidence}",
            f"- tables: {len(result.selected_tables or result.relevant_columns)}",
        ]
        if result.required_constraints:
            lines.append("- Preserve every constraint; unresolved items follow.")
        if result.unresolved_concepts:
            lines.append(f"- UNRESOLVED (do not substitute or guess): {', '.join(result.unresolved_concepts)}")
        for warning in result.warnings[:4]:
            lines.append(f"- Warning: {warning}")
        selected = result.selected_tables or list(result.relevant_columns)
        ranked_lookup = {item.table: item for item in result.ranked_tables}
        lines.append("Schema:")
        for table in selected:
            profile = profiles.get(table)
            if not profile:
                continue
            ranked = ranked_lookup.get(table)
            row_note = f"~{profile.row_count:,} rows" if profile.row_count is not None else "row count unknown"
            score_note = f", score={ranked.score}" if ranked else ", bridge"
            reason_text = "; ".join((ranked.reasons if ranked else ["required relationship bridge"])[:4])
            lines.append(f"- {table} ({row_note}{score_note})")
            lines.append(f"  reasons: {reason_text}")
            support_columns = []
            for column in result.ranked_columns.get(table, []):
                if set(column.roles) <= {"context", "identifier", "reference"}:
                    support_columns.append(column.column)
                    continue
                profile_column = next((item for item in profile.columns if item.column_name == column.column), None)
                data_type = profile_column.data_type if profile_column else "unknown"
                flags = ",".join(column.roles)
                reasons = "; ".join(column.reasons[:3])
                lines.append(f"  - {column.column} {data_type} [{flags}]: {reasons}")
            if support_columns:
                lines.append(f"  support columns: {', '.join(support_columns[:6])}")
        if result.relationship_paths:
            lines.append("Join paths:")
            for path in result.relationship_paths:
                lines.append(
                    f"- {' -> '.join(path.tables)} (score={path.score}, "
                    f"fk={path.declared_edge_count}, inf={path.inferred_edge_count}, "
                    f"risk={path.duplicate_amplification_risk}, preagg={path.required_pre_aggregation})"
                )
                if path.required_pre_aggregation:
                    lines.append(
                        "  safety: pre-aggregate facts before joins; do not sum repeated header amounts."
                    )
                for edge in path.edges:
                    safety = ""
                    if edge.get("required_pre_aggregation") or edge.get("duplicate_amplification_risk") != "low":
                        safety = (
                            f", risk={edge.get('duplicate_amplification_risk')}, "
                            f"preagg={edge.get('required_pre_aggregation')}"
                        )
                    lines.append(
                        f"  - {edge['left_table']}.{','.join(edge['left_columns'])} = "
                        f"{edge['right_table']}.{','.join(edge['right_columns'])} "
                        f"({'declared' if edge['declared'] else edge.get('verification_status', 'inferred')}, "
                        f"conf={edge['confidence_score']}, card={edge.get('cardinality', 'unknown')}{safety})"
                    )
        return "\n".join(lines)

    def _requirements(self, plan: dict, entity_resolution: dict) -> list[_Requirement]:
        requirements = []
        seen = set()

        def add(role, value, source, raw=None):
            if isinstance(value, dict):
                raw_value = value.get("raw_text") or value.get("raw_value") or value.get("field_concept")
                raw = raw if raw is not None else raw_value
                value = value.get("normalized_text") or value.get("field_concept") or raw_value
            concept = self._normalize(value)
            if not concept or concept in {"none", "unknown", "specified"}:
                return
            marker = (role, concept)
            if marker in seen:
                return
            seen.add(marker)
            requirements.append(_Requirement(
                key=f"{role}:{concept}",
                role=role,
                concept=concept,
                source=source,
                raw=str(raw if raw is not None else value),
            ))

        for item in plan.get("requested_outputs") or plan.get("requested_output") or []:
            add("output", item, "parsed_output")
        for item in plan.get("metric_terms") or plan.get("metrics") or []:
            add("metric", item, "parsed_metric")
        for item in plan.get("dimension_terms") or plan.get("dimensions") or []:
            add("dimension", item, "parsed_dimension")
        for item in plan.get("filters") or []:
            if item.get("field_concept"):
                add("filter", item["field_concept"], "parsed_filter", item)
        date_target = self._date_target_concept(plan)
        for item in plan.get("date_constraints") or []:
            add("date", item.get("field_concept") or date_target, "parsed_date", item)
        for item in plan.get("status_constraints") or []:
            add("status", item.get("field_concept") or item.get("value") or item.get("normalized_value") or "status", "parsed_status", item)
        for item in plan.get("grouping_hints") or []:
            add("grouping", item, "parsed_grouping")
        for item in plan.get("ordering") or []:
            if item.get("target_concept"):
                add("ordering", item["target_concept"], "parsed_ordering", item)
        for item in plan.get("sort_fields") or []:
            add("ordering", item, "parsed_ordering")

        contract = plan.get("question_contract") or {}
        contract_roles = {
            "required_outputs": "output",
            "required_metrics": "metric",
            "required_dimensions": "dimension",
            "required_grouping": "grouping",
        }
        for key, role in contract_roles.items():
            for value in contract.get(key) or []:
                add(role, value, f"contract:{key}")
        for value in contract.get("required_ordering") or []:
            target = str(value).rsplit(" ", 1)[0]
            if target and target != "requested metric":
                add("ordering", target, "contract:required_ordering", value)
        for value in contract.get("required_date_constraints") or []:
            add("date", date_target, "contract:required_date_constraints", value)
        for value in contract.get("required_statuses") or []:
            add("status", value, "contract:required_statuses")
        for value in contract.get("required_filters") or []:
            field = re.split(r"\s*(?:=|!=|>=|<=|>|<|:|\bequals\b|\bis\b|\bbetween\b)\s*", str(value), maxsplit=1, flags=re.IGNORECASE)[0]
            if field and not self._looks_like_value(field):
                add("filter", field, "contract:required_filters", value)

        resolved_values = set()
        for item in [*(entity_resolution.get("matches") or []), *(entity_resolution.get("entities") or [])]:
            value = item.get("value") or item.get("matched_value") or item.get("mention")
            if value:
                resolved_values.add(self._normalize(value))
        for value in plan.get("candidate_entity_mentions") or []:
            normalized = self._normalize(value)
            add("entity", value, "parsed_entity" if normalized in resolved_values else "unresolved_entity")
        return requirements

    def _required_constraints(self, plan):
        contract = dict(plan.get("question_contract") or {})
        return {
            "operation": plan.get("operation"),
            "sub_operations": list(plan.get("sub_operations") or []),
            "sort_direction": plan.get("sort_direction"),
            "limit": plan.get("limit"),
            "offset": plan.get("offset"),
            "date_constraints": list(plan.get("date_constraints") or []),
            "status_constraints": list(plan.get("status_constraints") or []),
            "numeric_constraints": list(plan.get("numeric_constraints") or []),
            "question_contract": contract,
        }

    def _rank_requirement_columns(self, *, requirements, profiles, question_terms, catalog, resolved_columns):
        ranked = {}
        searchable_profiles = profiles
        if len(profiles) > 500:
            table_terms_by_name = {}
            frequencies = {}
            for table, profile in profiles.items():
                terms = set(profile.business_terms) | self._terms(profile.normalized_name)
                table_terms_by_name[table] = terms
                for term in terms:
                    frequencies[term] = frequencies.get(term, 0) + 1
            rare_question_terms = {
                term for term in question_terms
                if frequencies.get(term, 0) and frequencies.get(term, 0) <= max(25, len(profiles) * 0.05)
            }
            anchored = {
                table for table, terms in table_terms_by_name.items()
                if rare_question_terms & terms
            }
            anchored.update(table for table, _column in resolved_columns)
            anchored.update(self._verified_catalog_tables(catalog, question_terms))
            if anchored:
                searchable_profiles = {
                    table: profiles[table]
                    for table in sorted(anchored)
                    if table in profiles
                }
        for requirement in requirements:
            candidates = []
            for table, profile in searchable_profiles.items():
                for column in profile.columns:
                    score, reasons = self._score_column(
                        requirement,
                        table=table,
                        profile=profile,
                        column=column,
                        question_terms=question_terms,
                        catalog=catalog,
                        resolved_columns=resolved_columns,
                    )
                    if score > 0.08:
                        candidates.append((score, table, column.column_name, reasons))
            ranked[requirement.key] = sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))
        return ranked

    def _verified_catalog_tables(self, catalog, question_terms):
        tables = set()
        for section in ("metrics", "dimensions", "date_fields", "status_fields", "important_tables"):
            for item in catalog.get(section, []):
                table = item.get("table")
                if not table or not str(item.get("verification_status", "")).startswith("verified"):
                    continue
                item_terms = self._terms(" ".join(str(value) for value in item.values()))
                if self._similarity(question_terms, item_terms):
                    tables.add(table)
        return tables

    def _score_column(self, requirement, *, table, profile, column, question_terms, catalog, resolved_columns):
        raw_context = "" if requirement.raw.lstrip().startswith("{") else requirement.raw
        concept_terms = self._expanded_concept_terms(requirement.concept, raw_context)
        column_terms = set(column.business_terms) | self._terms(column.normalized_name) | self._terms(column.comment or "")
        table_terms = set(profile.business_terms) | self._terms(profile.normalized_name) | self._terms(profile.description or "")
        lexical = self._similarity(concept_terms, column_terms)
        direct_expansion_hits = self.CONCEPT_EXPANSIONS.get(requirement.concept, set()) & column_terms
        if direct_expansion_hits:
            lexical = max(lexical, 0.78)
        question = min(1.0, self._similarity(question_terms, column_terms) * 1.25)
        role = self._role_compatibility(requirement.role, column)
        catalog_score = self._catalog_column_score(catalog, requirement, table, column.column_name)
        if requirement.role in {"metric", "date", "status"} and role == 0 and catalog_score == 0:
            return 0.0, []
        if role == 0 and catalog_score >= 0.88:
            role = 0.65
        entity = 1.0 if (table, column.column_name) in resolved_columns else 0.55 if table in {item[0] for item in resolved_columns} and requirement.role == "entity" else 0.0
        metadata = min(1.0, column.profile_confidence)
        table_context = self._similarity(concept_terms, table_terms)
        signals = {
            "lexical": lexical,
            "question": question,
            "role": role,
            "catalog": catalog_score,
            "entity": entity,
            "metadata": metadata,
            "table_context": table_context,
        }
        score = sum(self.COLUMN_WEIGHTS[key] * value for key, value in signals.items())
        if (
            requirement.role in {"output", "dimension", "grouping"}
            and column.is_identifier_like
            and not column.is_name_like
            and not column.is_code_like
        ):
            score = min(score, 0.35)
        if requirement.role == "grouping" and requirement.concept in {"location", "date", "status", "category"}:
            if column.is_name_like and lexical < 0.35 and catalog_score < 0.45:
                score = min(score, 0.33)
            if lexical >= 0.50:
                score = min(1.0, score + 0.18)
        reasons = []
        labels = {
            "lexical": "concept matches column metadata",
            "question": "column terms occur in the question",
            "role": f"column is compatible with {requirement.role} role",
            "catalog": "business catalog supports this column",
            "entity": "resolved entity is located in this column",
            "metadata": "column profile has strong metadata evidence",
            "table_context": "parent table matches the required concept",
        }
        for key, value in sorted(signals.items(), key=lambda item: (-item[1], item[0])):
            if value >= 0.45:
                reasons.append(labels[key])
        return min(1.0, score), reasons

    def _expanded_concept_terms(self, concept, raw_context=""):
        terms = self._terms(f"{concept} {raw_context}")
        normalized = self._normalize(concept)
        terms.update(self.CONCEPT_EXPANSIONS.get(normalized, set()))
        for token in list(terms):
            terms.update(self.CONCEPT_EXPANSIONS.get(token, set()))
        return terms

    def _score_table(self, profile, *, requirements, question_terms, per_requirement, resolved_tables, connectivity, catalog):
        table_terms = set(profile.business_terms) | self._terms(profile.normalized_name) | self._terms(profile.description or "")
        requirement_terms = set().union(*(self._terms(item.concept) for item in requirements)) if requirements else set()
        requirement_similarity = self._similarity(requirement_terms, table_terms)
        question_similarity = self._similarity(question_terms, table_terms)
        semantic = requirement_similarity * 0.60 + question_similarity * 0.40
        schema_metadata = min(1.0, profile.profile_confidence)
        business = self._catalog_table_score(catalog, profile.table_name, requirement_terms)
        if business >= 1.0:
            semantic = max(semantic, 0.85)
        entity = 1.0 if profile.table_name in resolved_tables else 0.0
        relationship = min(1.0, connectivity / 5.0)
        metric = self._role_requirement_score(requirements, per_requirement, "metric")
        dimension = self._role_requirement_score(requirements, per_requirement, {"dimension", "output", "grouping"})
        date = self._role_requirement_score(requirements, per_requirement, "date")
        status = self._role_requirement_score(requirements, per_requirement, "status")
        if profile.row_count is None:
            population = 0.4
        elif int(profile.row_count) <= 0:
            population = 0.0
        else:
            population = min(1.0, 0.35 + math.log10(int(profile.row_count) + 1) / 8)
        historical = min(1.0, math.log2(profile.historical_success_count + 1) / 5)
        signals = {
            "semantic_similarity": semantic,
            "schema_metadata": schema_metadata,
            "business_concepts": business,
            "entity_matches": entity,
            "relationship_connectivity": relationship,
            "metric_compatibility": metric,
            "dimension_compatibility": dimension,
            "date_compatibility": date,
            "status_compatibility": status,
            "table_population": population,
            "historical_usage": historical,
        }
        score = sum(self.weights[key] * value for key, value in signals.items())
        reasons = []
        reason_labels = {
            "semantic_similarity": "semantic requirement match",
            "schema_metadata": "strong schema metadata",
            "business_concepts": "business concept evidence",
            "entity_matches": "contains resolved entity value",
            "relationship_connectivity": "connected in relationship graph",
            "metric_compatibility": "contains compatible metric",
            "dimension_compatibility": "contains compatible dimension/output",
            "date_compatibility": "contains compatible date field",
            "status_compatibility": "contains compatible status field",
            "table_population": "non-empty table",
            "historical_usage": "verified successful query history",
        }
        for key, value in sorted(signals.items(), key=lambda item: (-item[1], item[0])):
            if value >= 0.45:
                reasons.append(reason_labels[key])
        if profile.row_count is not None and int(profile.row_count) == 0:
            score = max(0.0, score - 0.12)
            reasons.append("empty table penalty")
        return min(1.0, score), reasons[:10], signals

    def _select_minimum_tables(self, *, requirements, candidates, table_requirement_scores, resolved_tables, candidate_limit):
        candidate_names = [item.table for item in candidates]
        selected = [table for table in candidate_names if table in resolved_tables]
        mapped = set()
        for requirement in requirements:
            if requirement.role == "entity" and any(table in resolved_tables for table in selected):
                mapped.add(requirement.key)
        uncovered = [item for item in requirements if item.key not in mapped and item.role != "entity"]
        while uncovered and len(selected) < candidate_limit:
            choices = []
            for table in candidate_names:
                if table in selected:
                    continue
                gains = []
                for requirement in uncovered:
                    score = table_requirement_scores.get(table, {}).get(requirement.key, 0)
                    if score >= self.ROLE_THRESHOLDS.get(requirement.role, 0.30):
                        gains.append(score)
                if gains:
                    table_score = next(item.score for item in candidates if item.table == table)
                    choices.append((sum(gains) + table_score * 0.2, len(gains), table))
            if not choices:
                break
            _gain, _count, chosen = max(choices, key=lambda item: (item[0], item[1], -candidate_names.index(item[2])))
            selected.append(chosen)
            newly_mapped = []
            for requirement in uncovered:
                score = table_requirement_scores.get(chosen, {}).get(requirement.key, 0)
                if score >= self.ROLE_THRESHOLDS.get(requirement.role, 0.30):
                    mapped.add(requirement.key)
                    newly_mapped.append(requirement.key)
            uncovered = [item for item in uncovered if item.key not in newly_mapped]
        if not selected and candidates:
            selected = [candidates[0].table]
        return selected, mapped

    def _relationship_paths(self, core_tables, relationships, candidates):
        if len(core_tables) < 2:
            return [], [], [], []
        candidate_scores = {item.table: item.score for item in candidates}
        anchor = max(core_tables, key=lambda table: (candidate_scores.get(table, 0), -core_tables.index(table)))
        paths = []
        bridges = []
        unresolved = []
        warnings = []
        connected = {anchor}
        for target in core_tables:
            if target in connected:
                continue
            path = self._best_path(connected, target, relationships)
            if not path or path.score < self.relationship_threshold:
                unresolved.append(f"relationship:{anchor}->{target}")
                warnings.append(f"No sufficiently reliable relationship path connects {anchor} and {target}.")
                continue
            required_bridges = [
                table for table in path.tables[1:-1]
                if table not in core_tables and table not in bridges
            ]
            if len(bridges) + len(required_bridges) > self.max_bridge_tables:
                unresolved.append(f"relationship:{anchor}->{target}")
                warnings.append(f"Relationship path from {anchor} to {target} exceeds the bridge-table limit.")
                continue
            paths.append(path)
            warnings.extend(path.warnings)
            connected.update(path.tables)
            bridges.extend(required_bridges)
        return paths, bridges, unresolved, warnings

    def _best_path(self, starts, target, relationships, max_hops=3):
        adjacency = {}
        for relationship in relationships:
            adjacency.setdefault(relationship.source_table, []).append((relationship, relationship.target_table))
            adjacency.setdefault(relationship.target_table, []).append((relationship, relationship.source_table))
        queue = deque((start, [start], [], {start}) for start in sorted(starts))
        candidates = []
        while queue:
            table, tables, edges, seen = queue.popleft()
            if len(edges) >= max_hops:
                continue
            for edge, next_table in adjacency.get(table, []):
                if next_table in seen:
                    continue
                next_tables = [*tables, next_table]
                next_edges = [*edges, edge]
                if next_table == target:
                    candidates.append(self._path_profile(next_tables, next_edges))
                    continue
                queue.append((next_table, next_tables, next_edges, {*seen, next_table}))
        return max(candidates, key=lambda item: (item.score, -len(item.tables), item.tables)) if candidates else None

    def _path_profile(self, tables, edges):
        declared = sum(edge.declared for edge in edges)
        inferred = len(edges) - declared
        minimum = min(edge.confidence_score for edge in edges)
        score = minimum - max(0, len(edges) - 1) * 0.07 - inferred * 0.04
        risk_order = {"low": 0, "medium": 1, "high": 2}
        risk = "low"
        for edge in edges:
            edge_risk = getattr(edge, "duplicate_amplification_risk", "low") or "low"
            if risk_order.get(edge_risk, 0) > risk_order.get(risk, 0):
                risk = edge_risk
        if risk == "medium":
            score -= 0.05
        elif risk == "high":
            score -= 0.14
        reasons = ["declared foreign-key path" if not inferred else "path includes inferred relationship evidence"]
        if len(edges) > 1:
            reasons.append(f"{len(edges)}-hop path required to connect selected concepts")
        warnings = []
        if inferred:
            warnings.append("Join path contains inferred relationships; aggregation grain must be validated.")
        if risk != "low":
            warnings.append(f"Join path has {risk} duplicate-amplification risk; aggregate before joining when totals are requested.")
        if any(self._uses_display_join_columns(edge) and not edge.declared and edge.verification_status != "verified" for edge in edges):
            score -= 0.25
            warnings.append("Join path uses display-like columns without verification; prefer identifier joins.")
        required_pre_aggregation = any(getattr(edge, "required_pre_aggregation", False) for edge in edges)
        modules = sorted({
            module
            for edge in edges
            for module in getattr(edge, "applicable_business_modules", [])
            if module
        })
        edge_payload = []
        for left, right, edge in zip(tables, tables[1:], edges):
            if edge.source_table == left and edge.target_table == right:
                left_columns = list(edge.source_columns)
                right_columns = list(edge.target_columns)
            else:
                left_columns = list(edge.target_columns)
                right_columns = list(edge.source_columns)
            edge_payload.append({
                "left_table": left,
                "left_columns": left_columns,
                "right_table": right,
                "right_columns": right_columns,
                "declared": edge.declared,
                "confidence_score": round(edge.confidence_score, 3),
                "evidence": list(edge.evidence),
                "cardinality": edge.cardinality,
                "verification_status": edge.verification_status,
                "duplicate_amplification_risk": edge.duplicate_amplification_risk,
                "required_pre_aggregation": edge.required_pre_aggregation,
                "applicable_business_modules": list(edge.applicable_business_modules),
            })
        return RankedRelationshipPath(
            tables=tables,
            score=round(max(0.0, min(1.0, score)), 3),
            declared_edge_count=declared,
            inferred_edge_count=inferred,
            reasons=reasons,
            warnings=warnings,
            edges=edge_payload,
            duplicate_amplification_risk=risk,
            required_pre_aggregation=required_pre_aggregation,
            applicable_business_modules=modules,
        )

    def _uses_display_join_columns(self, edge):
        display_terms = {"name", "title", "description", "desc", "label"}
        columns = [*edge.source_columns, *edge.target_columns]
        return any(display_terms & self._terms(column) for column in columns)

    def _select_columns(
        self,
        *,
        requirements,
        selected_tables,
        bridge_tables,
        profiles,
        requirement_columns,
        relationships,
        pre_mapped,
        catalog,
    ):
        selected = {table: {} for table in selected_tables}
        coverage = {}
        unresolved = []
        warnings = []
        for requirement in requirements:
            candidates = [
                item for item in requirement_columns.get(requirement.key, [])
                if item[1] in selected_tables
            ]
            threshold = self.ROLE_THRESHOLDS.get(requirement.role, 0.30)
            if requirement.role == "entity" and requirement.source == "unresolved_entity":
                coverage[requirement.key] = {"mapped": False, "reason": "entity value was not resolved to a schema location"}
                unresolved.append(requirement.key)
                continue
            if not candidates or candidates[0][0] < threshold:
                coverage[requirement.key] = {"mapped": False, "reason": "no compatible column met the retrieval threshold"}
                unresolved.append(requirement.key)
                continue
            top = candidates[0]
            if self._ambiguous_role(requirement.role, candidates):
                coverage[requirement.key] = {
                    "mapped": False,
                    "reason": "multiple columns are equally plausible",
                    "candidates": [f"{item[1]}.{item[2]}" for item in candidates[:3]],
                }
                unresolved.append(requirement.key)
                warnings.append(f"Ambiguous {requirement.role} mapping for '{requirement.concept}'.")
                continue
            score, table, column, reasons = top
            entry = selected[table].setdefault(column, {"score": score, "roles": set(), "reasons": [], "requirements": []})
            entry["score"] = max(entry["score"], score)
            entry["roles"].add(requirement.role)
            entry["reasons"].extend(reasons)
            entry["requirements"].append(requirement.key)
            coverage[requirement.key] = {
                "mapped": True,
                "table": table,
                "column": column,
                "score": round(score, 3),
            }

        mapped_by_concept = {}
        for requirement in requirements:
            item = coverage.get(requirement.key, {})
            if item.get("mapped"):
                mapped_by_concept.setdefault(requirement.concept, item)
        for requirement in requirements:
            item = coverage.get(requirement.key, {})
            donor = mapped_by_concept.get(requirement.concept)
            if item.get("mapped") or not donor or requirement.role == "entity":
                continue
            table = donor["table"]
            column_name = donor["column"]
            column_profile = next(
                (column for column in profiles[table].columns if column.column_name == column_name),
                None,
            )
            if not column_profile or self._role_compatibility(requirement.role, column_profile) <= 0:
                continue
            entry = selected[table].setdefault(column_name, {"score": donor["score"], "roles": set(), "reasons": [], "requirements": []})
            entry["roles"].add(requirement.role)
            entry["reasons"].append("same parsed concept already grounded to this column")
            entry["requirements"].append(requirement.key)
            coverage[requirement.key] = {**donor, "mapped": True, "inherited": True}
            if requirement.key in unresolved:
                unresolved.remove(requirement.key)

        join_columns = self._path_join_columns(relationships)
        for table, columns in join_columns.items():
            if table not in selected:
                continue
            for column in columns:
                entry = selected[table].setdefault(column, {"score": 0.9, "roles": set(), "reasons": [], "requirements": []})
                entry["roles"].add("join")
                entry["reasons"].append("required by selected relationship path")

        for table in selected_tables:
            profile = profiles[table]
            self._add_supporting_columns(
                selected[table],
                profile,
                requirements=requirements,
            )
            self._add_verified_catalog_columns(
                selected[table],
                table=table,
                catalog=catalog,
            )
            if table in bridge_tables and not selected[table]:
                for column in profile.primary_keys[:2]:
                    selected[table][column] = {
                        "score": 0.7,
                        "roles": {"join"},
                        "reasons": ["bridge-table key"],
                        "requirements": [],
                    }
            if not selected[table]:
                orientation = self._orientation_columns(profile)
                for column in orientation[:3]:
                    selected[table][column.column_name] = {
                        "score": 0.25,
                        "roles": {"orientation"},
                        "reasons": ["minimal table orientation metadata"],
                        "requirements": [],
                    }
            if selected[table] and not any("display" in item["roles"] or "output" in item["roles"] for item in selected[table].values()):
                display = next((column for column in profile.columns if column.is_name_like), None)
                if display and display.column_name not in selected[table] and len(selected[table]) < self.column_limit:
                    selected[table][display.column_name] = {
                        "score": 0.32,
                        "roles": {"display"},
                        "reasons": ["display-name column for manager-readable output"],
                        "requirements": [],
                    }

        ranked_columns = {}
        relevant_columns = {}
        for table, columns in selected.items():
            ranked = []
            for column, detail in columns.items():
                ranked.append(RankedColumn(
                    table=table,
                    column=column,
                    score=round(detail["score"], 3),
                    roles=sorted(detail["roles"]),
                    reasons=list(dict.fromkeys(detail["reasons"]))[:6],
                    matched_requirements=list(dict.fromkeys(detail["requirements"])),
                ))
            ranked.sort(key=lambda item: (-item.score, item.column))
            ranked = ranked[: self.column_limit]
            ranked_columns[table] = ranked
            relevant_columns[table] = [item.column for item in ranked]
        return relevant_columns, ranked_columns, coverage, unresolved, warnings

    def _add_verified_catalog_columns(self, selected_columns, *, table, catalog):
        verified = catalog.get("verified_semantic_catalog") or {}
        if not verified or not selected_columns:
            return
        allowed = set()
        for item in catalog.get("important_tables", []):
            if item.get("table") == table and str(item.get("verification_status", "")).startswith("verified"):
                allowed.update(str(column) for column in item.get("columns", []) if column)
        for section in ("metrics", "dimensions", "date_fields", "status_fields"):
            for item in catalog.get(section, []):
                if item.get("table") != table:
                    continue
                if str(item.get("verification_status", "")).startswith("verified") and item.get("column"):
                    allowed.add(str(item["column"]))
        if not allowed:
            return
        capacity = max(0, self.column_limit - len(selected_columns))
        if capacity <= 0:
            return
        for column in sorted(allowed):
            if capacity <= 0:
                break
            if column in selected_columns:
                continue
            selected_columns[column] = {
                "score": 0.42,
                "roles": {"verified"},
                "reasons": ["verified semantic catalog column for selected table"],
                "requirements": [],
            }
            capacity -= 1

    def _add_supporting_columns(self, selected_columns, profile, *, requirements):
        if not selected_columns:
            return
        capacity = max(0, self.column_limit - len(selected_columns))
        if capacity <= 0:
            return
        required_roles = {requirement.role for requirement in requirements}
        wants_dates = "date" in required_roles
        has_selected_date = any(
            "date" in detail["roles"]
            for detail in selected_columns.values()
        )
        wants_lookup_context = bool(required_roles & {"output", "dimension", "grouping", "entity"})

        candidates = []
        for column_name in profile.primary_keys[:2]:
            candidates.append((0.34, column_name, "identifier", "primary key for stable row identity"))
        for column in profile.columns:
            if column.column_name in selected_columns:
                continue
            terms = self._terms(column.column_name)
            sensitive_or_contact = bool(
                terms & {
                    "aadhar", "account", "bank", "contact", "email", "esi", "fax",
                    "gst", "ifsc", "mobile", "pan", "passport", "pf", "phone",
                    "res", "tax",
                }
            )
            business_reference = bool(
                terms & {"bill", "grn", "indent", "invoice", "order", "po", "ref", "workorder"}
            )
            if not sensitive_or_contact and business_reference and (
                column.is_code_like or
                column.is_identifier_like
                or terms & {"no", "number", "ref", "code"}
            ):
                candidates.append((0.33, column.column_name, "reference", "business reference/code for display and filtering"))
            elif wants_dates and not has_selected_date and column.is_date_like:
                candidates.append((0.32, column.column_name, "date", "date column needed for requested date constraint"))
            elif wants_lookup_context and column.is_date_like:
                candidates.append((0.24, column.column_name, "context", "useful date context for manager-readable output"))

        added = 0
        for score, column_name, role, reason in sorted(candidates, key=lambda item: (-item[0], item[1])):
            if capacity <= 0 or added >= 4:
                break
            if column_name in selected_columns:
                continue
            selected_columns[column_name] = {
                "score": score,
                "roles": {role},
                "reasons": [reason],
                "requirements": [],
            }
            capacity -= 1
            added += 1

    def _confidence(self, *, requirements, coverage, selected_tables, candidates, relationship_paths, unresolved, warnings):
        if requirements:
            coverage_score = sum(item.get("mapped", False) for item in coverage.values()) / len(requirements)
        else:
            coverage_score = 0.6 if selected_tables else 0.0
        selected_scores = [item.score for item in candidates if item.table in selected_tables]
        table_score = sum(selected_scores) / len(selected_scores) if selected_scores else 0.0
        relationship_score = sum(path.score for path in relationship_paths) / len(relationship_paths) if relationship_paths else (1.0 if len(selected_tables) <= 1 else 0.35)
        margin = 0.5
        if len(candidates) >= 2:
            margin = min(1.0, max(0.0, (candidates[0].score - candidates[1].score) * 5 + 0.35))
        confidence = coverage_score * 0.48 + table_score * 0.25 + relationship_score * 0.17 + margin * 0.10
        confidence -= min(0.25, len(unresolved) * 0.06)
        confidence -= min(0.12, len(warnings) * 0.03)
        confidence = max(0.0, min(0.96, confidence))
        if unresolved:
            confidence = min(confidence, 0.62)
        reasons = [f"required concept coverage={coverage_score:.2f}", f"selected-table evidence={table_score:.2f}"]
        if relationship_paths:
            reasons.append(f"relationship-path evidence={relationship_score:.2f}")
        if unresolved:
            reasons.append("confidence capped because required concepts remain unresolved")
        return round(confidence, 2), reasons

    def _role_compatibility(self, role, column: ColumnProfile):
        if role == "metric":
            return 1.0 if column.is_metric else 0.0
        if role == "date":
            return 1.0 if column.is_date_like else 0.0
        if role == "status":
            return 1.0 if column.is_status_like else 0.0
        if role in {"output", "dimension", "grouping"}:
            if column.is_name_like:
                return 1.0
            if column.is_dimension or column.is_code_like:
                return 0.82
            if column.is_identifier_like:
                return 0.35
            return 0.0
        if role == "ordering":
            return 0.9 if column.is_metric else 0.75 if column.is_dimension else 0.0
        if role == "entity":
            return 0.9 if column.is_name_like or column.is_code_like else 0.65 if column.is_identifier_like else 0.0
        if role == "filter":
            return 0.85 if column.is_dimension or column.is_identifier_like else 0.55
        return 0.4

    def _catalog_column_score(self, catalog, requirement, table, column):
        score = 0.0
        concept_terms = self._terms(requirement.concept)
        sections = []
        if requirement.role == "metric":
            sections.append(catalog.get("metrics", []))
        if requirement.role in {"dimension", "output", "grouping", "filter", "entity"}:
            sections.append(catalog.get("dimensions", []))
        if requirement.role == "date":
            sections.append(catalog.get("date_fields", []))
        if requirement.role == "status":
            sections.append(catalog.get("status_fields", []))
        for section in sections:
            for item in section:
                if item.get("table") != table or item.get("column") != column:
                    continue
                item_terms = self._terms(" ".join(str(value) for value in item.values()))
                similarity = self._similarity(concept_terms, item_terms)
                if item.get("verification_status", "").startswith("verified") and similarity:
                    score = max(score, 0.92 + similarity * 0.08)
                elif item.get("preferred_table") == table and item.get("preferred_column") == column and similarity:
                    score = max(score, 0.88 + similarity * 0.12)
                else:
                    score = max(score, 0.65 + similarity * 0.35)
        return min(1.0, score)

    def _catalog_table_score(self, catalog, table, requirement_terms):
        terms = set()
        covered_tables = set()
        for section in ("metrics", "dimensions", "important_tables"):
            for item in catalog.get(section, []):
                if not isinstance(item, dict) or not item.get("table"):
                    continue
                covered_tables.add(item["table"])
                if item.get("table") == table:
                    item_terms = self._terms(" ".join(str(value) for value in item.values()))
                    if item.get("verification_status", "").startswith("verified"):
                        if self._similarity(requirement_terms, item_terms):
                            return 1.0
                    terms.update(item_terms)
        for concept, detail in (catalog.get("entities") or {}).items():
            entity_tables = {item.get("table") for item in detail.get("tables", []) if item.get("table")}
            covered_tables.update(entity_tables)
            if table in entity_tables:
                terms.update(self._terms(concept))
        coverage = min(1.0, len(covered_tables) / max(1, int(catalog.get("table_count") or len(covered_tables) or 1)))
        return self._similarity(requirement_terms, terms) * coverage

    def _resolved_locations(self, resolution):
        columns = set()
        tables = set()
        for item in [*(resolution.get("matches") or []), *(resolution.get("entities") or [])]:
            confidence = float(item.get("confidence", 100) or 0)
            table = item.get("table")
            column = item.get("column")
            if table and confidence >= 70:
                tables.add(table)
                if column:
                    columns.add((table, column))
        return columns, tables

    def _connectivity(self, relationships):
        connectivity = {}
        for item in relationships:
            connectivity[item.source_table] = connectivity.get(item.source_table, 0) + 1
            connectivity[item.target_table] = connectivity.get(item.target_table, 0) + 1
        return connectivity

    def _role_requirement_score(self, requirements, scores, roles):
        roles = {roles} if isinstance(roles, str) else set(roles)
        values = [scores.get(item.key, 0) for item in requirements if item.role in roles]
        return max(values, default=0.0)

    def _best_column_score(self, ranked, requirement_key, table):
        return max((item[0] for item in ranked.get(requirement_key, []) if item[1] == table), default=0.0)

    def _ambiguous_role(self, role, candidates):
        if role not in {"date", "status"} or len(candidates) < 2:
            return False
        first, second = candidates[0], candidates[1]
        if first[1] != second[1]:
            return False
        return first[2] != second[2] and abs(first[0] - second[0]) < 0.02

    def _path_join_columns(self, paths):
        result = {}
        for path in paths:
            for edge in path.edges:
                result.setdefault(edge["left_table"], set()).update(edge["left_columns"])
                result.setdefault(edge["right_table"], set()).update(edge["right_columns"])
        return result

    def _orientation_columns(self, profile):
        preferred = [column for column in profile.columns if column.primary_key or column.is_name_like or column.is_code_like]
        return preferred or profile.columns[:3]

    def _terms(self, value):
        text = str(value or "")
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
        text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
        text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
        words = [word.lower() for word in re.findall(r"[A-Za-z0-9]+", text)]
        result = set()
        for word in words:
            if word in self.STOP_WORDS:
                continue
            result.add(word)
            if word.endswith("ies") and len(word) > 4:
                result.add(f"{word[:-3]}y")
            elif word.endswith("s") and len(word) > 3:
                result.add(word[:-1])
        return result

    def _similarity(self, left, right):
        left = set(left)
        right = set(right)
        if not left or not right:
            return 0.0
        exact = len(left & right)
        partial = 0
        for first in left - right:
            if len(first) < 4:
                continue
            if any(len(second) >= 4 and (first.startswith(second) or second.startswith(first)) for second in right - left):
                partial += 1
        return min(1.0, (exact + partial * 0.55) / max(1, len(left)))

    def _date_target_concept(self, plan):
        question = self._normalize(plan.get("original_question") or plan.get("normalized_question") or "")
        excluded = set()
        for key in ("metrics", "dimensions", "requested_output", "grouping_hints"):
            for value in plan.get(key) or []:
                excluded.update(self._terms(value))
        excluded.update({"amount", "count", "metric", "quantity", "total", "value"})
        match = re.search(
            r"\b([a-z][a-z0-9_]*)\s+(?:in|during|for|between)\s+(?:19|20)\d{2}\b",
            question,
        )
        if match:
            qualifier = match.group(1)
            if qualifier not in self.STOP_WORDS and qualifier not in excluded:
                return f"{qualifier} date"
        return "date"

    def _looks_like_value(self, value):
        text = self._normalize(value)
        return bool(re.fullmatch(r"\d+(?:\.\d+)?|true|false|yes|no", text))

    def _normalize(self, value):
        return re.sub(r"\s+", " ", re.sub(r"[_\-./]+", " ", str(value or "").lower())).strip()

    def _stable_constraint_text(self, value):
        if isinstance(value, dict):
            return "{" + ", ".join(f"{key}: {self._stable_constraint_text(value[key])}" for key in sorted(value) if value[key] not in (None, [], {})) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(self._stable_constraint_text(item) for item in value) + "]"
        return str(value)

    def _prompt_constraint_text(self, constraints):
        contract = constraints.get("question_contract") or {}
        compact = {
            "operation": constraints.get("operation"),
            "sub_operations": constraints.get("sub_operations"),
            "limit": constraints.get("limit"),
            "sort_direction": constraints.get("sort_direction"),
            **{key: value for key, value in contract.items() if value not in (None, [], {})},
        }
        return self._stable_constraint_text(compact)

    def _build_profiles(self, *, schema: dict, row_counts: dict, catalog: dict) -> dict[str, TableProfile]:
        profile = self.profiler.build(
            database_id=catalog.get("database_fingerprint", "uncached"),
            dialect=catalog.get("source_type", "database"),
            schema=schema,
            schema_fingerprint=catalog.get("schema_fingerprint", "uncached"),
            row_counts=row_counts,
            catalog=catalog,
        )
        return {table.table_name: table for table in profile.tables}

    def _column_profile(self, column: dict) -> ColumnProfile:
        schema = {"temporary": {"columns": [column], "foreign_keys": []}}
        profile = self.profiler.build(
            database_id="temporary",
            dialect="database",
            schema=schema,
            schema_fingerprint="temporary",
        )
        return profile.tables[0].columns[0]

    def _profile_from_dict(self, value: dict) -> TableProfile:
        return self.profiler.table_from_dict(value)


schema_retriever = SchemaRetriever()
