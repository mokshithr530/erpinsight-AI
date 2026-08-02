import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from verified_sql_validator import verified_sql_validator


QUERY_PLAN_VERSION = "query-plan:v17"


@dataclass
class QueryPlan:
    original_question: str
    operation: str
    intent_category: str
    requested_outputs: list[str] = field(default_factory=list)
    base_entity: str | None = None
    metrics: list[str] = field(default_factory=list)
    aggregation: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    numeric_constraints: list[dict[str, Any]] = field(default_factory=list)
    resolved_entity_constraints: list[dict[str, Any]] = field(default_factory=list)
    date_constraints: list[dict[str, Any]] = field(default_factory=list)
    status_constraints: list[dict[str, Any]] = field(default_factory=list)
    grouping: list[str] = field(default_factory=list)
    ordering: list[dict[str, Any]] = field(default_factory=list)
    limit: int | None = None
    joins: list[dict[str, Any]] = field(default_factory=list)
    aggregation_grain: list[str] = field(default_factory=list)
    selected_tables: list[str] = field(default_factory=list)
    selected_columns: dict[str, list[str]] = field(default_factory=dict)
    generation_tables: list[str] = field(default_factory=list)
    generation_columns: dict[str, list[str]] = field(default_factory=dict)
    catalog_authoritative: bool = False
    catalog_version: str | None = None
    erp_schema_version: str | None = None
    bound_metrics: list[dict[str, Any]] = field(default_factory=list)
    bound_dimensions: list[dict[str, Any]] = field(default_factory=list)
    bound_date_fields: list[dict[str, Any]] = field(default_factory=list)
    bound_status_fields: list[dict[str, Any]] = field(default_factory=list)
    bound_formulas: list[dict[str, Any]] = field(default_factory=list)
    bound_business_definitions: list[dict[str, Any]] = field(default_factory=list)
    approved_relationships: list[dict[str, Any]] = field(default_factory=list)
    clarification_required: bool = False
    clarification_questions: list[dict[str, Any]] = field(default_factory=list)
    binding_confidence_score: int | None = None
    unresolved_concepts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self):
        payload = asdict(self)
        payload["version"] = QUERY_PLAN_VERSION
        return payload


class QueryPlanGuard:
    AGGREGATION_SQL = {
        "count": "count",
        "distinct_count": "count",
        "sum": "sum",
        "total": "sum",
        "average": "avg",
        "avg": "avg",
        "minimum": "min",
        "min": "min",
        "maximum": "max",
        "max": "max",
    }

    def build(
        self,
        *,
        question,
        semantic_plan,
        entity_resolution,
        retrieval,
        semantic_binding=None,
    ):
        semantic_binding = semantic_binding or {}
        contract = semantic_plan.get("question_contract") or {}
        outputs = self._unique([
            *(semantic_plan.get("requested_output") or []),
            *(contract.get("required_outputs") or []),
        ])
        parsed_dimensions = self._unique([
            *(semantic_plan.get("dimensions") or []),
            *(contract.get("required_dimensions") or []),
        ])
        bound_dimensions = list(semantic_binding.get("bound_dimensions") or [])
        bound_dimension_requests = {
            str(item.get("requested_concept") or "").lower()
            for item in bound_dimensions
            if isinstance(item, dict)
        }
        dimensions = self._unique([
            *[
                item.get("bound_concept") or item.get("dimension")
                for item in bound_dimensions
                if isinstance(item, dict)
            ],
            *[
                item for item in parsed_dimensions
                if item.lower() not in bound_dimension_requests
            ],
        ])
        grouping = self._unique([
            *(semantic_plan.get("grouping_hints") or []),
            *(contract.get("required_grouping") or []),
        ])
        parsed_metrics = self._unique([
            *(semantic_plan.get("metrics") or []),
            *(contract.get("required_metrics") or []),
        ])
        bound_metrics = list(semantic_binding.get("bound_metrics") or [])
        bound_metric_requests = {
            str(item.get("requested_concept") or "").lower()
            for item in bound_metrics
            if isinstance(item, dict)
        }
        metrics = self._unique([
            *[
                item.get("bound_concept") or item.get("metric")
                for item in bound_metrics
                if isinstance(item, dict)
            ],
            *[
                item for item in parsed_metrics
                if item.lower() not in bound_metric_requests
            ],
        ])
        explicit_aggregations = self._unique([
            *(semantic_plan.get("aggregations") or []),
            *[
                item.get("function")
                for item in semantic_plan.get("aggregation_specs") or []
                if isinstance(item, dict) and item.get("function")
            ],
        ])
        aggregations = explicit_aggregations or self._unique([
            *[
                item.get("default_aggregation")
                for item in bound_metrics
                if isinstance(item, dict) and item.get("default_aggregation")
            ],
        ])
        aggregations = self._normalize_aggregations(aggregations)
        operation = semantic_plan.get("operation") or "unknown"
        inventory_grouped_stock = (
            any(
                str(item.get("metric") or item.get("bound_concept") or "").lower() == "current_stock"
                for item in bound_metrics
                if isinstance(item, dict)
            )
            and bool(bound_dimensions)
            and str(semantic_plan.get("operation") or "").lower() not in {"count", "existence_check"}
            and "count" not in {
                str(item).strip().lower()
                for item in aggregations
                if item
            }
            and re.search(
                r"\bmaterials?\b|\bby\s+(?:material|store|warehouse)s?\b",
                str(question or "").lower(),
            )
        )
        if inventory_grouped_stock and not dimensions:
            dimensions = self._unique([
                item.get("bound_concept") or item.get("dimension")
                for item in bound_dimensions
                if isinstance(item, dict)
            ])
        if inventory_grouped_stock and not grouping:
            grouping = list(dimensions)
        if (
            not grouping
            and dimensions
            and bound_metrics
            and operation in {"ranking", "top_n", "bottom_n", "distribution", "breakdown", "comparison"}
        ):
            grouping = list(dimensions)
        if set(aggregations) and set(aggregations) <= {"min", "max"}:
            operation = "aggregation"
            grouping = []
            outputs = []
            dimensions = []
            semantic_plan = {**semantic_plan, "limit": None}
            contract = {**contract, "required_limit": None}
        scalar_without_grouping = (
            operation
            in {
                "count",
                "sum",
                "average",
                "aggregation",
                "existence_check",
            }
            and not grouping
            and not inventory_grouped_stock
        )
        base_entity = (dimensions or outputs or [None])[0]
        if scalar_without_grouping:
            dimension_names = {
                str(item).strip().lower()
                for item in dimensions
                if item
            }
            outputs = [
                item
                for item in outputs
                if str(item).strip().lower() not in dimension_names
            ]
            dimensions = []
        if operation in {"count", "existence_check"} and "count" in {
            str(item).lower()
            for item in semantic_plan.get("aggregations") or []
        }:
            aggregations = ["count"]
        ordering = [
            item for item in semantic_plan.get("ordering") or []
            if isinstance(item, dict)
        ]
        if re.search(r"\b(?:bottom|lowest|least)\b", str(question or "").lower()):
            ordering = [
                {**item, "direction": "asc"}
                for item in ordering
            ]
            semantic_plan = {**semantic_plan, "sort_direction": "asc"}
        if semantic_plan.get("sort_direction"):
            ordering.append({
                "target_concept": (semantic_plan.get("sort_fields") or [None])[0],
                "direction": semantic_plan.get("sort_direction"),
            })
        if set(aggregations) and set(aggregations) <= {"min", "max"}:
            ordering = []
        entity_constraints = self._entity_constraints(entity_resolution)
        unresolved = [
            item.get("mention") or item.get("original_text") or item.get("text")
            for item in entity_resolution.get("unresolved", [])
            if isinstance(item, dict)
        ]
        retrieval_warnings = retrieval.get("warnings") or []
        if retrieval.get("unresolved_concepts"):
            unresolved.extend(retrieval["unresolved_concepts"])
        unresolved.extend(
            item.get("concept")
            for item in semantic_binding.get("unresolved_concepts") or []
            if isinstance(item, dict) and item.get("concept")
        )
        selected_tables = retrieval.get("selected_tables") or retrieval.get("tables") or []
        if not selected_tables:
            selected_tables = [
                item.get("table")
                for item in retrieval.get("ranked_tables", [])
                if isinstance(item, dict) and item.get("table")
            ]
        selected_tables = self._unique([
            *(semantic_binding.get("required_tables") or []),
            *selected_tables,
        ])
        authoritative_table_scope = self._authoritative_table_scope(
            semantic_binding,
            entity_constraints,
        )
        if authoritative_table_scope:
            selected_tables = self._unique([
                *selected_tables,
                *[
                    item.get("table")
                    for item in entity_constraints
                    if item.get("table") in authoritative_table_scope
                ],
            ])
            selected_tables = [
                table
                for table in selected_tables
                if table in authoritative_table_scope
            ]
        if selected_tables:
            selected_table_set = set(selected_tables)
            entity_constraints = [
                item
                for item in entity_constraints
                if item.get("table") in selected_table_set
            ]
        selected_columns = self._merge_columns(
            semantic_binding.get("required_columns") or {},
            retrieval.get("relevant_columns") or retrieval.get("columns") or {},
        )
        selected_columns = self._augment_verified_columns(
            selected_columns,
            question=question,
            operation=operation,
            entity_resolution=entity_resolution,
            semantic_binding=semantic_binding,
        )
        approved_relationships = list(
            semantic_binding.get("approved_relationships") or []
        )
        aggregation_grain = self._unique([
            *(semantic_binding.get("aggregation_grain") or []),
            *grouping,
            *dimensions,
            *(
                [entity_constraints[0]["entity_type"]]
                if entity_constraints
                else []
            ),
        ])
        return QueryPlan(
            original_question=question,
            operation=operation,
            intent_category=semantic_plan.get("intent_category") or "unknown",
            requested_outputs=outputs,
            base_entity=base_entity,
            metrics=metrics,
            aggregation=aggregations,
            dimensions=dimensions,
            filters=semantic_plan.get("filters") or [],
            numeric_constraints=semantic_plan.get("numeric_constraints") or [],
            resolved_entity_constraints=entity_constraints,
            date_constraints=semantic_plan.get("date_constraints") or [],
            status_constraints=semantic_plan.get("status_constraints") or [],
            grouping=grouping,
            ordering=ordering,
            limit=semantic_plan.get("limit") or contract.get("required_limit"),
            joins=approved_relationships or retrieval.get("relationship_paths") or [],
            aggregation_grain=aggregation_grain,
            selected_tables=selected_tables,
            selected_columns=selected_columns,
            generation_tables=self._generation_tables(
                semantic_binding,
                selected_tables,
                authoritative_table_scope,
            ),
            generation_columns=selected_columns,
            catalog_authoritative=bool(semantic_binding.get("authoritative")),
            catalog_version=semantic_binding.get("catalog_version"),
            erp_schema_version=semantic_binding.get("erp_schema_version"),
            bound_metrics=bound_metrics,
            bound_dimensions=bound_dimensions,
            bound_date_fields=list(semantic_binding.get("bound_date_fields") or []),
            bound_status_fields=list(semantic_binding.get("bound_status_fields") or []),
            bound_formulas=list(semantic_binding.get("bound_formulas") or []),
            bound_business_definitions=list(
                semantic_binding.get("bound_business_definitions") or []
            ),
            approved_relationships=approved_relationships,
            clarification_required=bool(semantic_binding.get("clarification_required")),
            clarification_questions=list(
                semantic_binding.get("clarification_questions") or []
            ),
            binding_confidence_score=semantic_binding.get("confidence_score"),
            unresolved_concepts=self._unique([str(item) for item in unresolved if item]),
            warnings=self._unique([
                *retrieval_warnings,
                *(semantic_binding.get("warnings") or []),
            ]),
        )

    def validate_plan(self, plan):
        errors = []
        warnings = list(plan.warnings)
        if plan.unresolved_concepts:
            warnings.append("Unresolved concepts: " + ", ".join(plan.unresolved_concepts[:6]))
        if (plan.metrics or plan.requested_outputs) and not plan.selected_tables:
            errors.append("Query plan has requested concepts but no selected tables.")
        if plan.limit is not None and plan.limit <= 0:
            errors.append("Query plan contains an invalid limit.")
        if plan.catalog_authoritative and plan.clarification_required:
            errors.append("Verified ERP query plan requires clarification.")
        if (
            plan.catalog_authoritative
            and plan.metrics
            and not plan.bound_metrics
            and not self._allows_verified_identity_count(plan)
        ):
            errors.append("Requested metric is not bound to a verified ERP definition.")
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def _allows_verified_identity_count(self, plan):
        if plan.operation not in {"count", "existence_check", "breakdown"} and "count" not in {
            str(item).lower() for item in plan.aggregation
        }:
            return False
        dimension_names = {
            str(item.get("dimension") or item.get("bound_concept") or "").lower()
            for item in plan.bound_dimensions
            if isinstance(item, dict)
        }
        tables = set(plan.generation_tables or plan.selected_tables)
        return bool(
            dimension_names & {"material_issue_line", "store_issue", "transaction_type"}
            and tables & {"store_material_usage_quantity", "store_usage_quantity_master"}
        )

    def validate_sql(self, sql, plan, schema=None):
        sql_clean = str(sql or "")
        sql_lower = sql_clean.lower()
        errors = []
        warnings = []

        if not sql_clean.strip():
            return {"valid": False, "errors": ["No SQL query was generated."], "warnings": []}

        self._check_entity_filters(sql_lower, plan, errors)
        self._check_date_filters(sql_lower, plan, errors)
        self._check_status_filters(sql_lower, plan, errors)
        self._check_aggregation(sql_lower, plan, errors)
        self._check_grouping(sql_lower, plan, errors)
        self._check_ordering(sql_lower, plan, errors)
        self._check_limit(sql_lower, plan, errors)
        self._check_joins(sql_lower, errors)
        self._check_retrieved_context(sql_clean, plan, warnings)
        verified_result = verified_sql_validator.validate(
            sql_clean,
            plan,
            schema=schema,
        )
        errors.extend(verified_result["errors"])
        warnings.extend(verified_result["warnings"])

        return {
            "valid": not errors,
            "errors": self._unique(errors),
            "warnings": self._unique(warnings),
            "analysis": verified_result["analysis"],
        }

    def prompt(self, plan):
        return (
            "Required query plan contract. The SQL must preserve every explicit user constraint:\n"
            f"{self._stable_json(plan.to_dict())}\n\n"
            "If SQL validation says this contract was not preserved, repair the SQL instead of dropping the constraint.\n"
        )

    def _entity_constraints(self, resolution):
        constraints = []
        resolution_items = resolution.get("entities") or resolution.get("matches") or []
        for item in resolution_items:
            table = item.get("table")
            column = item.get("column")
            value = item.get("value") or item.get("matched_value") or item.get("entity")
            if not table or not column or value in (None, ""):
                continue
            constraints.append({
                "original_text": item.get("mention") or item.get("original_text") or value,
                "entity_type": item.get("entity_type") or item.get("resolved_type"),
                "table": table,
                "column": column,
                "value": str(value),
                "primary_key_column": item.get("primary_key_column"),
                "primary_key_value": item.get("primary_key_value") or item.get("primary_key"),
                "confidence": item.get("confidence"),
            })
        return self._dedupe_dicts(constraints)

    def _authoritative_table_scope(self, semantic_binding, entity_constraints):
        if not semantic_binding.get("authoritative"):
            return None
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in semantic_binding.get("bound_metrics") or []
            if isinstance(item, dict)
        }
        verified_runtime_modules = {"billing", "purchase_orders", "projects", "stores", "inventory", "vendors"}
        po_modules = {"purchase_orders"}
        project_modules = {"projects"}
        store_modules = {"stores"}
        inventory_modules = {"inventory"}
        vendor_modules = {"vendors"}
        po_metric_tables = {
            item.get("table")
            for item in semantic_binding.get("bound_metrics") or []
            if isinstance(item, dict)
            and str(item.get("applicable_module") or "").lower() in po_modules
        }
        po_context = bool(po_metric_tables) or any(
            str(item.get("applicable_module") or "").lower() in po_modules
            for key in (
                "bound_dimensions",
                "bound_date_fields",
                "bound_status_fields",
            )
            for item in semantic_binding.get(key) or []
            if isinstance(item, dict)
        )
        project_context = bool(metric_modules & project_modules) or any(
            str(item.get("applicable_module") or "").lower() in project_modules
            for key in (
                "bound_dimensions",
                "bound_date_fields",
                "bound_status_fields",
            )
            for item in semantic_binding.get(key) or []
            if isinstance(item, dict)
        )
        store_context = bool(metric_modules & store_modules) or any(
            str(item.get("applicable_module") or "").lower() in store_modules
            for key in (
                "bound_dimensions",
                "bound_date_fields",
                "bound_status_fields",
            )
            for item in semantic_binding.get(key) or []
            if isinstance(item, dict)
        )
        inventory_context = bool(metric_modules & inventory_modules) or any(
            str(item.get("applicable_module") or "").lower() in inventory_modules
            for key in (
                "bound_dimensions",
                "bound_date_fields",
                "bound_status_fields",
            )
            for item in semantic_binding.get(key) or []
            if isinstance(item, dict)
        )
        vendor_context = bool(metric_modules & vendor_modules) or any(
            str(item.get("applicable_module") or "").lower() in vendor_modules
            for key in (
                "bound_dimensions",
                "bound_date_fields",
                "bound_status_fields",
            )
            for item in semantic_binding.get(key) or []
            if isinstance(item, dict)
        ) or any(
            str(item.get("entity_type") or "").lower() == "vendor"
            for item in entity_constraints
            if isinstance(item, dict)
        )
        if not (metric_modules & verified_runtime_modules) and not any([po_context, project_context, store_context, inventory_context, vendor_context]):
            return None
        tables = set()
        for key in (
            "bound_metrics",
            "bound_dimensions",
            "bound_date_fields",
            "bound_status_fields",
        ):
            for item in semantic_binding.get(key) or []:
                if isinstance(item, dict):
                    table = item.get("table") or item.get("preferred_table")
                    if table:
                        tables.add(table)
        for relationship in semantic_binding.get("approved_relationships") or []:
            if isinstance(relationship, dict):
                for key in ("left_table", "right_table"):
                    if relationship.get(key):
                        tables.add(relationship[key])
        if "billing" in metric_modules and any(
            str(item.get("entity_type") or "").lower() == "project"
            for item in entity_constraints
            if isinstance(item, dict)
        ):
            tables.add("project_mst")
        if po_context:
            for table in ("po_form", "po_items", "project_mst"):
                if table in tables:
                    continue
                if any(
                    str(item.get("table") or "").lower() == table
                    for item in entity_constraints
                    if isinstance(item, dict)
                ):
                    tables.add(table)
        if project_context:
            tables.add("project_mst")
        if store_context:
            tables.add("store_material_usage_quantity")
            has_project_context = project_context or any(
                str(item.get("table") or "").lower() == "project_mst"
                or str(item.get("dimension") or item.get("bound_concept") or "").lower() == "project"
                for item in [
                    *(semantic_binding.get("bound_dimensions") or []),
                    *(entity_constraints or []),
                ]
                if isinstance(item, dict)
            )
            needs_store_master = any(
                str(item.get("table") or "").lower() == "store_usage_quantity_master"
                for item in [
                    *(semantic_binding.get("bound_dimensions") or []),
                    *(semantic_binding.get("bound_date_fields") or []),
                    *(semantic_binding.get("bound_status_fields") or []),
                ]
                if isinstance(item, dict)
            ) or any(
                str(item.get("entity_type") or "").lower() in {"project", "store_issue"}
                for item in entity_constraints
                if isinstance(item, dict)
            ) or has_project_context
            if needs_store_master:
                tables.add("store_usage_quantity_master")
            for table in ("project_mst", "sub_material"):
                if table in tables:
                    continue
                if any(
                    str(item.get("table") or "").lower() == table
                    for item in [
                        *(semantic_binding.get("bound_dimensions") or []),
                        *(entity_constraints or []),
                    ]
                    if isinstance(item, dict)
                ):
                    tables.add(table)
        if inventory_context:
            tables.add("inventory_master")
            for table in ("sub_material",):
                if table in tables:
                    continue
                if any(
                    str(item.get("table") or "").lower() == table
                    for item in [
                        *(semantic_binding.get("bound_dimensions") or []),
                        *(entity_constraints or []),
                    ]
                    if isinstance(item, dict)
                ):
                    tables.add(table)
        if vendor_context:
            for table in ("vendor_mst", "vendor_branch_details", "vendor_types"):
                if table in tables:
                    continue
                if table == "vendor_mst" and not po_context:
                    tables.add(table)
                    continue
                if any(
                    str(item.get("table") or "").lower() == table
                    for item in [
                        *(semantic_binding.get("bound_dimensions") or []),
                        *(semantic_binding.get("bound_date_fields") or []),
                        *(semantic_binding.get("bound_status_fields") or []),
                        *(entity_constraints or []),
                    ]
                    if isinstance(item, dict)
                ):
                    tables.add(table)
        return tables or None

    def _generation_tables(self, semantic_binding, selected_tables, authoritative_table_scope):
        generation_tables = self._unique(semantic_binding.get("required_tables") or [])
        if authoritative_table_scope:
            generation_tables = [
                table
                for table in generation_tables
                if table in authoritative_table_scope
            ]
            generation_tables = self._unique([
                *generation_tables,
                *[
                    table for table in selected_tables
                    if table in authoritative_table_scope
                ],
            ])
            if not generation_tables:
                generation_tables = list(selected_tables)
        return generation_tables

    def _check_entity_filters(self, sql_lower, plan, errors):
        if not plan.resolved_entity_constraints:
            return
        has_where = bool(re.search(r"\bwhere\b", sql_lower))
        if not has_where:
            errors.append("SQL is missing WHERE filters for resolved entity constraints.")
            return
        for entity in plan.resolved_entity_constraints:
            values = [
                entity.get("value"),
                entity.get("primary_key_value"),
                entity.get("original_text"),
            ]
            normalized_values = [self._normalize_literal(value) for value in values if value not in (None, "")]
            if normalized_values and not any(value in sql_lower for value in normalized_values):
                errors.append(f"SQL is missing resolved entity filter for {entity.get('original_text')}.")

    def _check_date_filters(self, sql_lower, plan, errors):
        if not plan.date_constraints:
            return
        has_date_predicate = any(
            marker in sql_lower
            for marker in ("curdate()", "date_sub", "str_to_date", " between ", ">=", "<=", " year(", " month(")
        )
        years = [
            str(item.get("value"))
            for item in plan.date_constraints
            if isinstance(item, dict) and re.fullmatch(r"\d{4}", str(item.get("value") or ""))
        ]
        if not has_date_predicate and not any(year in sql_lower for year in years):
            errors.append("SQL is missing the requested date constraint.")

    def _check_status_filters(self, sql_lower, plan, errors):
        if not plan.status_constraints:
            return
        values = [
            str(item.get("normalized_value") or item.get("raw_value") or item.get("value") or "").lower()
            for item in plan.status_constraints
            if isinstance(item, dict)
        ]
        if values and not any(value and value in sql_lower for value in values):
            errors.append("SQL is missing the requested status constraint.")

    def _check_aggregation(self, sql_lower, plan, errors):
        required = set()
        for item in plan.aggregation:
            mapped = self.AGGREGATION_SQL.get(str(item).lower())
            if mapped:
                required.add(mapped)
        if plan.operation == "count":
            required.add("count")
        for function in sorted(required):
            if not re.search(rf"\b{re.escape(function)}\s*\(", sql_lower):
                if function == "sum" and self._allows_count_based_ranking(sql_lower, plan):
                    continue
                if function == "sum" and self._allows_row_level_metric_ranking(sql_lower, plan):
                    continue
                errors.append(f"SQL is missing required {function.upper()} aggregation.")

    def _check_grouping(self, sql_lower, plan, errors):
        if (plan.grouping or (plan.dimensions and plan.metrics)) and self._has_aggregate(sql_lower):
            if not re.search(r"\bgroup\s+by\b", sql_lower):
                errors.append("SQL is missing GROUP BY for requested grouping/dimensions.")

    def _check_ordering(self, sql_lower, plan, errors):
        if plan.ordering or plan.operation in {"ranking", "top_n", "bottom_n"}:
            if not re.search(r"\border\s+by\b", sql_lower):
                errors.append("SQL is missing ORDER BY for requested ranking/ordering.")
                return
        directions = {str(item.get("direction") or "").lower() for item in plan.ordering if isinstance(item, dict)}
        if (
            plan.operation in {"ranking", "top_n"}
            and "asc" not in directions
            and not re.search(r"\b(?:bottom|lowest|least)\b", str(plan.original_question or "").lower())
        ):
            directions.add("desc")
        if plan.operation == "bottom_n":
            directions.add("asc")
        if "desc" in directions and not re.search(r"\border\s+by[\s\S]+\bdesc\b", sql_lower):
            errors.append("SQL is missing DESC ordering.")
        if "asc" in directions and not re.search(r"\border\s+by[\s\S]+\basc\b", sql_lower):
            errors.append("SQL is missing ASC ordering.")

    def _check_limit(self, sql_lower, plan, errors):
        if plan.limit is None:
            return
        match = re.search(r"\blimit\s+(\d+)\b", sql_lower)
        if not match:
            errors.append(f"SQL is missing LIMIT {plan.limit}.")
            return
        actual = int(match.group(1))
        if self._allows_small_list_for_implicit_superlative(actual, plan):
            return
        if self._has_explicit_limit(plan) and actual != int(plan.limit):
            errors.append(
                f"SQL LIMIT {actual} does not match requested limit {plan.limit}."
            )
        elif actual > int(plan.limit):
            errors.append(
                f"SQL LIMIT {actual} exceeds requested limit {plan.limit}."
            )

    def _check_joins(self, sql_lower, errors):
        for match in re.finditer(r"\bjoin\b", sql_lower):
            prefix = sql_lower[max(0, match.start() - 12): match.start()]
            if re.search(r"\bcross\s+$", prefix):
                continue
            segment = sql_lower[match.start(): match.start() + 240]
            if re.match(r"\bjoin\s*\(", segment):
                continue
            next_join = re.search(r"\bjoin\b", segment[5:])
            if next_join:
                segment = segment[: next_join.start() + 5]
            if not re.search(r"\b(on|using)\b", segment):
                errors.append("SQL contains a JOIN without an ON/USING condition.")
                return

    def _check_retrieved_context(self, sql, plan, warnings):
        allowed_tables = set(plan.selected_tables)
        if not allowed_tables:
            return
        referenced = {
            table for table in re.findall(
                r"\b(?:from|join)\s+`?([A-Za-z_][\w]*)`?",
                sql,
                flags=re.IGNORECASE,
            )
        }
        outside = sorted(referenced - allowed_tables)
        if outside:
            warnings.append("SQL references table(s) outside retrieved context: " + ", ".join(outside[:6]))

    def _has_aggregate(self, sql_lower):
        return bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", sql_lower))

    def _allows_row_level_metric_ranking(self, sql_lower, plan):
        if plan.operation not in {"ranking", "top_n", "bottom_n"}:
            return False
        if re.search(r"\bjoin\b", sql_lower) or re.search(r"\bgroup\s+by\b", sql_lower):
            return False
        if not re.search(r"\border\s+by\b", sql_lower) or not re.search(r"\blimit\s+\d+\b", sql_lower):
            return False
        metric_terms = set()
        for metric in plan.metrics:
            metric_terms.update(re.findall(r"[a-z0-9]+", str(metric).lower()))
        for columns in plan.selected_columns.values():
            for column in columns:
                column_lower = str(column).lower()
                if re.search(r"(?:value|amount|price|cost|total|balance|qty|quantity|rate)", column_lower):
                    metric_terms.add(column_lower)
                    metric_terms.update(re.findall(r"[a-z0-9]+", column_lower))
        metric_terms -= {"total", "sum", "highest", "lowest", "top", "bottom"}
        return any(
            (len(term) >= 4 and term in sql_lower)
            or (len(term) >= 4 and term.replace("_", " ") in sql_lower.replace("_", " "))
            for term in metric_terms
        )

    def _allows_count_based_ranking(self, sql_lower, plan):
        if plan.operation not in {"ranking", "top_n", "bottom_n"}:
            return False
        if not re.search(r"\bcount\s*\(", sql_lower):
            return False
        text = " ".join([
            plan.original_question.lower(),
            " ".join(str(item).lower() for item in plan.dimensions),
            " ".join(str(item).lower() for item in plan.requested_outputs),
        ])
        return bool(re.search(r"\b(employees?|vendors?|customers?|clients?|projects?|orders?|invoices?|bills?|materials?)\b", text))

    def _allows_small_list_for_implicit_superlative(self, actual_limit, plan):
        if int(plan.limit or 0) != 1 or actual_limit > 10:
            return False
        question = plan.original_question.lower()
        if re.search(r"\b(?:top|first|limit)\s+1\b", question):
            return False
        return bool(re.search(r"\bmost\b|\bhighest\b|\blargest\b|\bbiggest\b", question))

    def _has_explicit_limit(self, plan):
        question = str(plan.original_question or "").lower()
        return bool(
            re.search(
                r"\b(?:top|bottom|first|last|limit|show|list)\s+"
                r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
                question,
            )
        )

    def _normalize_literal(self, value):
        text = str(value).strip().lower()
        text = text.replace("'", "\\'")
        return text

    def _normalize_aggregations(self, values):
        mapped = []
        aliases = {
            "average": "avg",
            "minimum": "min",
            "maximum": "max",
            "total": "sum",
        }
        for value in values:
            normalized = aliases.get(str(value).strip().lower(), str(value).strip().lower())
            if normalized and normalized not in mapped:
                mapped.append(normalized)
        if "avg" in mapped:
            return ["avg"]
        if "min" in mapped or "max" in mapped:
            result = []
            if "min" in mapped:
                result.append("min")
            if "max" in mapped:
                result.append("max")
            return result
        if "count" in mapped:
            return ["count"]
        return mapped

    def _unique(self, values):
        result = []
        seen = set()
        for value in values:
            if value in (None, "", [], {}):
                continue
            text = str(value)
            key = text.lower()
            if key not in seen:
                seen.add(key)
                result.append(text)
        return result

    def _dedupe_dicts(self, values):
        result = []
        seen = set()
        for value in values:
            key = tuple(sorted((key, str(item)) for key, item in value.items()))
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    def _merge_columns(self, *mappings):
        result = {}
        for mapping in mappings:
            for table, columns in mapping.items():
                result.setdefault(table, [])
                for column in columns:
                    if column not in result[table]:
                        result[table].append(column)
        return result

    def _augment_verified_columns(
        self,
        selected_columns,
        *,
        question,
        operation,
        entity_resolution,
        semantic_binding,
    ):
        if not semantic_binding.get("authoritative"):
            return selected_columns
        result = {table: list(columns) for table, columns in selected_columns.items()}
        question_lower = str(question or "").lower()
        entity_tables = {
            item.get("table")
            for item in [
                *(entity_resolution.get("entities") or []),
                *(entity_resolution.get("matches") or []),
            ]
            if item.get("table")
        }
        defaults = {}
        if "project_mst" in entity_tables and (
            "detail" in question_lower
            or operation in {"lookup", "relationship_lookup"}
        ):
            defaults["project_mst"] = [
                "project_id",
                "project_name",
                "client_name",
                "project_value",
                "project_start_date",
                "project_end_date",
                "status",
            ]
        if re.search(r"\bprojects?\b|\bproject\s+value\b|\bsite\b", question_lower):
            defaults["project_mst"] = [
                "project_id",
                "project_name",
                "client_name",
                "project_value",
                "project_state",
                "project_location",
                "project_start_date",
                "project_end_date",
                "status",
            ]
        if re.search(r"\bmaterial\b|\bissued\b|\bissue\b|\bstores?\b|\bconsumption\b|\brequested\b|\bquantity\b", question_lower):
            defaults["store_material_usage_quantity"] = [
                "store_material_usage_quantity_id",
                "store_usage_quantity_master_id",
                "sub_material_id",
                "requested_quantity",
                "issued_quantity",
                "base_charge",
                "transactiontype",
            ]
            defaults["store_usage_quantity_master"] = [
                "store_usage_quantity_master_id",
                "project_id",
                "issued_date",
                "transactiontype",
                "cancel_status",
            ]
            defaults["sub_material"] = [
                "sub_material_id",
                "sub_material_name",
                "uom",
            ]
            defaults.setdefault("project_mst", [
                "project_id",
                "project_name",
                "client_name",
            ])
        if re.search(r"\bcurrent\s+stock\b|\bavailable\s+stock\b|\bstock\s+quantity\b|\bstocked\s+materials?\b|\bout\s+of\s+stock\b|\bzero\s+stock\b|\bnegative\s+stock\b|\binventory\s+balance\b", question_lower):
            defaults["inventory_master"] = [
                "inventory_master_id",
                "sub_store_id",
                "material_id",
                "sub_material_id",
                "available_quantity",
                "total_quanity",
                "status",
            ]
            defaults["sub_material"] = [
                "sub_material_id",
                "sub_material_name",
                "uom",
                "submaterialcode",
                "item_code",
            ]
        if re.search(r"\bpurchase\s+orders?\b|\bpo\b", question_lower):
            defaults["po_form"] = [
                "po_form_id",
                "po_id",
                "po_no",
                "vendor_name",
                "sub_total",
                "total_value",
                "date",
                "po_validity_date",
            ]
            if re.search(r"\bitems?\b|\bline\b|\bstatus\b|\bproject\b|\bsite\b", question_lower):
                defaults["po_items"] = [
                    "po_item_id",
                    "po_id",
                    "po_project_id",
                    "qty",
                    "total_price",
                    "status",
                    "is_po_item_status",
                ]
        if re.search(r"\bvendors?\b|\bsuppliers?\b", question_lower):
            defaults["vendor_mst"] = [
                "vendor_id",
                "vendor_code",
                "vendor_name",
                "vendor_address",
                "contact_person_name",
                "contact_no",
                "email",
                "vendor_gst_no",
                "vendor_types_id",
            ]
            if re.search(r"\bpurchase\s+orders?\b|\border\s+count\b|\bpo\b", question_lower):
                defaults["po_form"] = [
                    "po_form_id",
                    "po_no",
                    "vendor_name",
                    "total_value",
                    "date",
                ]
            if re.search(r"\bstate\b|\bcity\b|\bcountry\b|\blocation\b", question_lower):
                defaults["vendor_branch_details"] = [
                    "vendor_branch_details_id",
                    "vendor_id",
                    "state",
                    "contact_person",
                    "contact_number",
                    "gst_no",
                ]
            if re.search(r"\bcategory\b|\btype\b|\bmsme\b", question_lower):
                defaults["vendor_types"] = [
                    "vendor_types_id",
                    "vendor_type",
                ]
        if "billing" in question_lower or "bill" in question_lower:
            defaults["client_bill_raising"] = [
                "billing_id",
                "ra_bill_number",
                "project_id",
                "bill_date",
                "gross_bill_value",
                "bill_amount",
                "billing_status",
            ]
        for table, columns in defaults.items():
            result.setdefault(table, [])
            for column in columns:
                if column not in result[table]:
                    result[table].append(column)
        return result

    def _stable_json(self, value):
        return json.dumps(value, indent=2, sort_keys=True, default=str)


query_plan_guard = QueryPlanGuard()
