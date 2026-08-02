from collections import deque
from dataclasses import asdict, dataclass, field
import re
from typing import Any


ERP_SEMANTIC_BINDER_VERSION = "erp-semantic-binder:v18"


@dataclass
class SemanticBinding:
    question: str
    authoritative: bool
    catalog_version: str | None
    erp_schema_version: str | None
    bound_metrics: list[dict[str, Any]] = field(default_factory=list)
    bound_dimensions: list[dict[str, Any]] = field(default_factory=list)
    bound_date_fields: list[dict[str, Any]] = field(default_factory=list)
    bound_status_fields: list[dict[str, Any]] = field(default_factory=list)
    bound_formulas: list[dict[str, Any]] = field(default_factory=list)
    bound_business_definitions: list[dict[str, Any]] = field(default_factory=list)
    approved_relationships: list[dict[str, Any]] = field(default_factory=list)
    entity_locations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    required_tables: list[str] = field(default_factory=list)
    required_columns: dict[str, list[str]] = field(default_factory=dict)
    aggregation_grain: list[str] = field(default_factory=list)
    unresolved_concepts: list[dict[str, Any]] = field(default_factory=list)
    clarification_required: bool = False
    clarification_questions: list[dict[str, Any]] = field(default_factory=list)
    fallback_required: bool = False
    confidence_score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version: str = ERP_SEMANTIC_BINDER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ERPSemanticBinder:
    CONCEPT_ALIASES = {
        "customer": "client",
        "customers": "client",
        "headcount": "employee_count",
        "material_issues": "material_consumption",
        "material_receipts": "received_quantity",
        "stock": "current_stock",
        "available_stock": "current_stock",
    }

    def bind(
        self,
        *,
        question: str,
        semantic_plan: dict[str, Any],
        catalog: dict[str, Any],
        entity_resolution: dict[str, Any] | None = None,
    ) -> SemanticBinding:
        verified = catalog.get("verified_semantic_catalog") or {}
        validation = catalog.get("catalog_validation") or verified.get("validation") or {}
        authoritative = bool(
            verified
            and validation.get("valid")
            and validation.get("compatible")
            and catalog.get("catalog_authority") == "verified_erp_primary"
        )
        result = SemanticBinding(
            question=question,
            authoritative=authoritative,
            catalog_version=catalog.get("catalog_version"),
            erp_schema_version=catalog.get("erp_schema_version"),
        )
        if not authoritative:
            result.fallback_required = True
            result.warnings.append("No compatible authoritative ERP catalog is loaded.")
            return result

        metrics, unresolved_metrics = self._bind_metrics(
            question,
            semantic_plan,
            catalog,
        )
        dimensions, unresolved_dimensions = self._bind_dimensions(
            semantic_plan,
            catalog,
        )
        dimensions = self._prune_context_dimensions(
            question,
            semantic_plan,
            metrics,
            dimensions,
            entity_resolution or {},
        )
        result.bound_metrics = metrics
        result.bound_dimensions = dimensions
        result.entity_locations = self._entity_locations(catalog)
        result.bound_formulas = self._bind_formulas(metrics, verified)
        result.bound_business_definitions = self._bind_business_definitions(
            semantic_plan,
            metrics,
            dimensions,
            verified,
        )
        result.bound_date_fields = self._bind_dates(
            semantic_plan,
            metrics,
            dimensions,
            catalog,
        )
        result.bound_status_fields = self._bind_statuses(
            semantic_plan,
            metrics,
            dimensions,
            catalog,
        )

        resolution_items = self._relevant_resolution_items(
            metrics,
            dimensions,
            entity_resolution or {},
        )
        required_tables = self._required_tables(
            metrics,
            dimensions,
            result.bound_date_fields,
            result.bound_status_fields,
            resolution_items,
        )
        relationships, relationship_unresolved = self._bind_relationships(
            required_tables,
            catalog,
        )
        result.approved_relationships = relationships
        required_tables.extend(self._relationship_tables(relationships))
        result.required_tables = self._unique(required_tables)
        result.required_columns = self._required_columns(
            metrics,
            dimensions,
            result.bound_date_fields,
            result.bound_status_fields,
            relationships,
            resolution_items,
        )
        result.aggregation_grain = self._aggregation_grain(
            semantic_plan,
            metrics,
            dimensions,
        )
        result.unresolved_concepts = self._dedupe_unresolved([
            *unresolved_metrics,
            *unresolved_dimensions,
            *relationship_unresolved,
            *self._catalog_unresolved(question, semantic_plan, verified),
        ])
        result.clarification_questions = self._clarification_questions(
            semantic_plan,
            result,
        )
        result.clarification_required = bool(result.clarification_questions)
        result.fallback_required = bool(result.unresolved_concepts)
        result.confidence_score, result.reasons = self._binding_confidence(result)
        return result

    def _bind_metrics(self, question, semantic_plan, catalog):
        requested = self._unique([
            *(semantic_plan.get("metrics") or []),
            *((semantic_plan.get("question_contract") or {}).get("required_metrics") or []),
        ])
        verified_metrics = [
            item
            for item in catalog.get("metrics", [])
            if str(item.get("verification_status") or "").startswith("verified")
        ]
        if not requested and semantic_plan.get("operation") not in {"count", "existence_check"}:
            requested = self._infer_metric_requests_from_question(
                question,
                verified_metrics,
            )
        question_lower = str(question or "").lower()
        if re.search(r"\b(?:stock|inventory)\s+value\b|\bvalue\s+of\s+(?:stock|inventory)\b", question_lower):
            requested = [
                "stock_value",
                *[
                    item for item in requested
                    if item not in {"stock", "current_stock", "amount_value", "rate"}
                ],
            ]
        elif re.search(r"\b(?:current|available)?\s*stock\b|\binventory\s+balance\b|\bavailable\s+inventory\s+quantity\b|\bcurrent\s+inventory\s+quantity\b|\bstocked\s+materials?\b|\bout\s+of\s+stock\b", question_lower):
            requested = [
                "current_stock",
                *[
                    item for item in requested
                    if item not in {"stock", "quantity", "inventory_quantity"}
                ],
            ]
        count_style_store_question = (
            (
                semantic_plan.get("operation") in {"count", "existence_check"}
                or "count" in {str(item).lower() for item in semantic_plan.get("aggregations") or []}
            )
            and re.search(r"\b(?:material\s+issue\s+lines?|store\s+issues?|material\s+issues?)\b", question_lower)
        )
        if count_style_store_question:
            requested = []
        if re.search(r"\bprojects?\s+by\s+status\b|\bprojects?\b.*\bstatus\b", question_lower):
            requested = []
        if re.search(r"\b(?:po|purchase order)\s+items?\b", question_lower) and re.search(r"\b(?:total\s+price|value|amount)\b", question_lower):
            requested = [
                "purchase_order_item_value",
                *[
                    item for item in requested
                    if item not in {"material_price", "rate", "amount_value", "purchase_value"}
                ],
            ]
        elif re.search(r"\b(?:po|purchase order)\s+items?\b", question_lower) and re.search(r"\b(?:qty|quantity)\b", question_lower):
            requested = [
                "ordered_quantity",
                *[
                    item for item in requested
                    if item not in {"quantity"}
                ],
            ]
        elif (
            semantic_plan.get("operation") not in {"count", "existence_check"}
            and not count_style_store_question
            and re.search(r"\b(?:material|store|issued|issue|consumption|requested)\b", question_lower)
        ):
            if "pricing" in question_lower and not re.search(r"\b(?:value|cost|amount)\b", question_lower):
                requested = [
                    "material_consumption",
                    "material_price",
                    *[
                        item for item in requested
                        if item not in {"quantity", "amount_value", "rate"}
                    ],
                ]
            elif "value" in question_lower or "pricing" in question_lower or "price" in question_lower or "cost" in question_lower:
                requested = [
                    "material_issue_value",
                    *[
                        item for item in requested
                        if item not in {"amount_value", "material_price", "rate"}
                    ],
                ]
            elif "requested" in question_lower:
                requested = [
                    "requested_quantity",
                    *[
                        item for item in requested
                        if item not in {"quantity"}
                    ],
                ]
            elif any(word in question_lower for word in ["issued", "issue", "consumption", "used"]):
                requested = [
                    "material_consumption",
                    *[
                        item for item in requested
                        if item not in {"quantity"}
                    ],
                ]
        elif re.search(r"\bproject\s+values?\b|\bprojects?\s+(?:by|based on|value|listed|available)\b", question_lower):
            requested = [
                "project_value",
                *[
                    item for item in requested
                    if item not in {"amount_value"}
                ],
            ]
        bound = []
        unresolved = []
        for requested_concept in requested:
            concept = self._canonical(requested_concept)
            if concept == "ordered_quantity" and "requested_quantity" in {
                self._canonical(value) for value in requested
            } and not re.search(r"\b(?:po|purchase order)\b", question_lower):
                continue
            candidates = [
                item for item in verified_metrics
                if concept in self._metric_names(item)
            ]
            candidates = self._rank_metric_candidates(
                question,
                candidates,
                semantic_plan,
                requested_concept,
            )
            if not candidates:
                unresolved.append({
                    "concept": requested_concept,
                    "role": "metric",
                    "essential": True,
                    "reason": "No verified ERP metric mapping exists.",
                })
                continue
            best = dict(candidates[0])
            best["requested_concept"] = requested_concept
            best["bound_concept"] = best.get("metric")
            best["authoritative"] = True
            bound.append(best)
        bound = self._ensure_explicit_cross_metrics(
            question_lower,
            bound,
            verified_metrics,
        )
        return self._dedupe_mappings(bound, ("metric", "table", "column")), unresolved

    def _ensure_explicit_cross_metrics(self, question_lower, bound, verified_metrics):
        required = []
        record_count_question = bool(
            re.search(r"\b(?:how many|count|available)\b", question_lower)
            and re.search(r"\b(?:lines?|items?|records?|entries?)\b", question_lower)
            and not re.search(r"\b(?:value|amount|total|sum|average|minimum|maximum|highest|lowest)\b", question_lower)
        )
        if re.search(
            r"\b(?:po|purchase(?:\s+order)?|procurement)\s+(?:value|amount|spend|spent|cost|total)\b|"
            r"\b(?:procured|purchased)\s+(?:value|amount|spend|cost|total)\b",
            question_lower,
        ) or (
            re.search(r"\b(?:po|purchase orders?)\b", question_lower)
            and re.search(r"\b(project\s+summary|summary|billing|material|usage|compare|vs|project[- ]wise|by\s+project)\b", question_lower)
        ):
            required.append("purchase_value")
        if (
            not record_count_question
            and re.search(
                r"\b(?:billing|billed|bill\s+value|bill\s+amount|gross\s+billing|invoice\s+amount|revenue\s+booked)\b",
                question_lower,
            )
        ):
            required.append("billing")
        if re.search(r"\b(?:project|contract|work\s*order|order\s+book)\s+(?:value|amount|worth)\b", question_lower):
            required.append("project_value")
        if re.search(
            r"\bmaterial\s+issue\s+value\b|\bissued\s+material\s+value\b|"
            r"\bmaterial(?:s)?\s+(?:cost|value|amount|spend|spent)\b|"
            r"\b(?:used|consumed|issued)\s+material\s+(?:cost|value|amount|spend)\b",
            question_lower,
        ):
            required.append("material_issue_value")
        if re.search(
            r"\bmaterials?\s+(?:issued|used|consumed|utili[sz]ed)\b|"
            r"\bmaterial\s+(?:usage|consumption|issued|utili[sz]ation)\b",
            question_lower,
        ):
            required.append("material_consumption")
        if (
            re.search(r"\b(?:summary|overview|dashboard|snapshot)\b", question_lower)
            and "project" in question_lower
            and len(set(required) & {"billing", "purchase_value", "material_consumption", "material_issue_value"}) >= 2
        ):
            required.append("project_value")
        existing = {
            self._canonical(item.get("metric") or item.get("bound_concept"))
            for item in bound
            if isinstance(item, dict)
        }
        for concept in required:
            if concept in existing:
                continue
            candidates = [
                item for item in verified_metrics
                if concept in self._metric_names(item)
            ]
            if not candidates:
                continue
            best = dict(sorted(candidates, key=lambda item: -self._mapping_confidence(item))[0])
            best["requested_concept"] = concept
            best["bound_concept"] = best.get("metric")
            best["authoritative"] = True
            bound.append(best)
            existing.add(concept)
        return bound

    def _infer_metric_requests_from_question(self, question, verified_metrics):
        question_text = f" {str(question or '').lower().replace('-', ' ')} "
        matches = []
        for item in verified_metrics:
            phrases = {
                str(item.get("metric") or "").replace("_", " "),
                str(item.get("column") or "").replace("_", " "),
                str(item.get("preferred_column") or "").replace("_", " "),
            }
            phrases.update(
                str(value).replace("_", " ")
                for value in item.get("semantic_aliases", [])
            )
            phrases.discard("")
            best_length = 0
            for phrase in phrases:
                phrase = phrase.strip().lower()
                if len(phrase) < 4:
                    continue
                if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", question_text):
                    best_length = max(best_length, len(phrase))
            if best_length:
                matches.append((
                    best_length,
                    self._mapping_confidence(item),
                    item.get("metric"),
                ))
        return self._unique([
            metric
            for _length, _confidence, metric in sorted(
                matches,
                reverse=True,
            )
            if metric
        ])[:3]

    def _bind_dimensions(self, semantic_plan, catalog):
        requested = self._unique([
            *(semantic_plan.get("dimensions") or []),
            *(semantic_plan.get("requested_output") or []),
            *((semantic_plan.get("question_contract") or {}).get("required_dimensions") or []),
            *((semantic_plan.get("question_contract") or {}).get("required_outputs") or []),
        ])
        ignored = {
            "database",
            "date",
            "detail",
            "details",
            "information",
            "overview",
            "record",
            "records",
            "type",
        }
        verified_dimensions = [
            item
            for item in catalog.get("dimensions", [])
            if str(item.get("verification_status") or "").startswith("verified")
        ]
        bound = []
        unresolved = []
        for requested_concept in requested:
            concept = self._canonical(requested_concept)
            question_lower = str(semantic_plan.get("original_question") or "").lower()
            if concept == "type" and "transaction" in question_lower:
                concept = "transaction_type"
            if concept in ignored:
                continue
            candidates = [
                item for item in verified_dimensions
                if concept in self._dimension_names(item)
            ]
            candidates.sort(
                key=lambda item: (
                    -self._mapping_confidence(item),
                    item.get("table", ""),
                    item.get("column", ""),
                )
            )
            if not candidates:
                unresolved.append({
                    "concept": requested_concept,
                    "role": "dimension",
                    "essential": self._dimension_is_essential(
                        requested_concept,
                        semantic_plan,
                    ),
                    "reason": "No verified ERP dimension mapping exists.",
                })
                continue
            candidates = self._rank_dimension_candidates(
                candidates,
                semantic_plan,
                requested_concept,
            )
            best = dict(candidates[0])
            best["requested_concept"] = requested_concept
            best["bound_concept"] = best.get("dimension")
            best["authoritative"] = True
            bound.append(best)
        return self._dedupe_mappings(bound, ("dimension", "table", "column")), unresolved

    def _rank_dimension_candidates(self, candidates, semantic_plan, requested_concept):
        question_lower = str(semantic_plan.get("original_question") or "").lower()
        metric_modules = {
            self._canonical(value)
            for value in semantic_plan.get("metrics") or []
        }
        requested = self._canonical(requested_concept)
        grouping = {
            self._canonical(value)
            for value in semantic_plan.get("grouping_hints") or []
        }
        requested_dimensions = {
            self._canonical(value)
            for value in [
                *(semantic_plan.get("dimensions") or []),
                *(semantic_plan.get("requested_output") or []),
            ]
        }
        po_context = bool(
            metric_modules & {
                "purchase_value",
                "purchase_order_subtotal",
                "purchase_order_item_value",
                "ordered_quantity",
                "vendor_purchase_order_count",
            }
            or re.search(
                r"\b(?:purchase\s+orders?|po|purchase\s+value|procurement|order\s+count)\b",
                question_lower,
            )
        )

        def score(item):
            module = str(item.get("applicable_module") or "").lower()
            dimension = self._canonical(item.get("dimension"))
            return (
                40 if requested == "vendor" and po_context and module == "purchase_orders" else 0,
                40 if "vendor" in requested_dimensions and requested in {"location", "state", "category", "vendor_category"} and module == "vendors" else 0,
                20 if requested == dimension else 0,
                18 if requested == "status" and dimension.endswith("status") else 0,
                24 if requested == "status" and re.search(r"\bprojects?\b", question_lower) and module == "projects" else 0,
                14 if "billing" in metric_modules and module == "billing" else 0,
                14 if po_context and module == "purchase_orders" else 0,
                14 if metric_modules & {"project_value"} and module == "projects" else 0,
                14 if metric_modules & {"material_consumption", "requested_quantity", "material_price", "material_issue_value"} and module in {"stores", "materials", "projects"} else 0,
                10 if requested == "invoice" and module == "billing" else 0,
                10 if requested in {"purchase_order", "purchase_order_item", "project", "status", "month"} and module == "purchase_orders" else 0,
                10 if requested == "vendor" and not po_context and module == "vendors" else 0,
                10 if requested in {"project", "client", "location", "state", "status", "project_status", "month"} and module == "projects" else 0,
                12 if metric_modules & {"current_stock"} and module in {"inventory", "stores", "materials"} else 0,
                10 if requested in {"project", "material", "store_issue", "transaction_type", "month"} and module in {"stores", "materials", "projects"} else 0,
                8 if requested in grouping else 0,
                self._mapping_confidence(item),
            )

        return sorted(
            candidates,
            key=lambda item: (
                *(-value for value in score(item)),
                item.get("table", ""),
                item.get("column", ""),
            ),
        )

    def _prune_context_dimensions(self, question, semantic_plan, metrics, dimensions, entity_resolution=None):
        if not dimensions:
            return dimensions
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in metrics
        }
        metric_names = {
            self._canonical(item.get("metric") or item.get("bound_concept"))
            for item in metrics
        }
        dimension_names = {
            self._canonical(item.get("dimension"))
            for item in dimensions
        }
        requested_outputs = {
            self._canonical(value)
            for value in semantic_plan.get("requested_output") or []
        }
        question_lower = str(question or "").lower()
        resolution_items = [
            *(entity_resolution or {}).get("entities", []),
            *(entity_resolution or {}).get("matches", []),
        ]
        has_project_entity = any(
            isinstance(item, dict)
            and
            str(item.get("entity_type") or item.get("resolved_type") or "").lower() == "project"
            and str(item.get("table") or "").lower() == "project_mst"
            for item in resolution_items
        )
        count_style_store_question = (
            (
                semantic_plan.get("operation") in {"count", "existence_check"}
                or "count" in {str(item).lower() for item in semantic_plan.get("aggregations") or []}
            )
            and re.search(r"\b(?:material\s+issue\s+lines?|store\s+issues?|material\s+issues?)\b", question_lower)
        )
        explicit_material_output = bool(
            re.search(
                r"\b(?:by|per|each)\s+(?:material|item|product)s?\b|"
                r"\b(?:top|bottom|highest|lowest)\s+\d*\s*(?:material|item|product)s?\b|"
                r"\bmaterials?\s+issued\s+with\s+pricing\b",
                question_lower,
            )
        )
        material_as_issue_document = bool(
            re.search(r"\bmaterial\s+issues?\b", question_lower)
            or re.search(r"\bmaterials?\s+(?:was\s+)?issued\s+by\s+(?:project|site)s?\b", question_lower)
        )
        project_level_cross_module = bool(
            ("project" in dimension_names or has_project_entity)
            and len(metric_modules & {"billing", "purchase_orders", "stores", "projects"}) >= 2
            and (
                has_project_entity
                or re.search(r"\bby\s+project\b|\bproject[- ]wise\b|\bproject\s+summary\b", question_lower)
                or re.search(r"\bprojects?\b.*\b(?:billing|po|purchase|material|issued)\b", question_lower)
                or re.search(r"\b(?:billing|po|purchase|material|issued)\b.*\bproject\b", question_lower)
            )
        )
        pruned = []
        for item in dimensions:
            name = self._canonical(item.get("dimension"))
            if (
                project_level_cross_module
                and name in {"purchase_order", "purchase_order_item", "material", "store_issue"}
                and not (
                    re.search(rf"\bby\s+{re.escape(name.replace('_', ' '))}\b", question_lower)
                    and not re.search(r"\bby\s+material\s+issue\s+value\b|\bby\s+po\s+value\b", question_lower)
                )
            ):
                continue
            if (
                name == "client"
                and "invoice" in dimension_names
                and re.search(r"\bclient\s+bills?\b", question_lower)
            ):
                continue
            if (
                name == "invoice"
                and "billing_status" in dimension_names
                and re.search(r"\b(?:bills?|billing)\s+by\s+status\b", question_lower)
            ):
                continue
            # Phrases like "materials with highest shortage in GRN" use GRN as
            # the module/source, not as a requested display grain.
            if (
                name == "goods_receipt"
                and "grn" in metric_modules
                and dimension_names & {"material", "project", "vendor"}
                and not re.search(r"\b(?:grn|goods receipt)\s+(?:number|id|records?|details?)\b", question_lower)
            ):
                continue
            # "purchase order total value" names the grain/source for a scalar
            # metric; it should not force a grouped output.
            if (
                name == "purchase_order"
                and semantic_plan.get("operation") in {"average", "aggregation", "sum", "minimum", "maximum"}
                and name not in requested_outputs
                and not re.search(r"\b(?:by|per|each)\s+(?:purchase\s+order|po|order)\b", question_lower)
            ):
                continue
            if (
                "project_value" in metric_modules
                and str(item.get("applicable_module") or "").lower() != "projects"
                and name in {"purchase_order_status", "status", "purchase_order", "purchase_order_item"}
            ):
                continue
            if (
                name == "material"
                and not explicit_material_output
                and (
                    count_style_store_question
                    or material_as_issue_document
                    or (
                        "project" in dimension_names
                        and metric_modules & {"stores"}
                    )
                )
            ):
                continue
            if (
                name == "inventory"
                and metric_names & {"current_stock"}
                and not re.search(r"\binventory\s+transactions?\b", question_lower)
            ):
                continue
            pruned.append(item)
        return pruned

    def _bind_formulas(self, metrics, verified):
        formulas = verified.get("business_formulas") or []
        result = []
        for metric in metrics:
            names = {
                self._canonical(metric.get("metric")),
                self._canonical(metric.get("requested_concept")),
            }
            matches = [
                item for item in formulas
                if self._canonical(item.get("formula")) in names
                and str(item.get("verification_status") or "").startswith("verified")
            ]
            if matches:
                result.append(dict(sorted(
                    matches,
                    key=lambda item: -self._mapping_confidence(item),
                )[0]))
                continue
            result.append({
                "formula": metric.get("metric"),
                "definition": f"{metric.get('table')}.{metric.get('column')}",
                "preferred_table": metric.get("table"),
                "preferred_column": metric.get("column"),
                "verification_status": metric.get("verification_status"),
                "confidence_score": self._mapping_confidence(metric),
                "source_of_evidence": metric.get("source_of_evidence"),
            })
        return result

    def _bind_business_definitions(
        self,
        semantic_plan,
        metrics,
        dimensions,
        verified,
    ):
        definitions = {
            item.get("concept_id"): item
            for item in verified.get("business_definitions", [])
            if item.get("verification_status") == "verified"
        }
        dimension_names = {
            self._canonical(item.get("dimension"))
            for item in dimensions
        }
        concept_ids = []
        for metric in metrics:
            metric_name = self._canonical(metric.get("metric"))
            column = metric.get("column")
            if metric_name == "billing":
                concept_ids.append(
                    "bill_amount" if column == "bill_amount" else "gross_billing"
                )
            elif metric_name == "purchase_value":
                concept_ids.append(
                    "vendor_purchase_value"
                    if "vendor" in dimension_names
                    else "purchase_value"
                )
            elif metric_name == "purchase_order_subtotal":
                concept_ids.append("purchase_order_subtotal")
            elif metric_name == "purchase_order_item_value":
                concept_ids.append("purchase_order_item_value")
            elif metric_name == "ordered_quantity" and (
                "purchase_order_item" in dimension_names
                or "purchase_order" in dimension_names
            ):
                concept_ids.append("purchase_order_item_quantity")
            elif metric_name == "project_value":
                concept_ids.append("project_value")
            elif metric_name == "requested_quantity":
                concept_ids.append("store_requested_quantity")
            elif metric_name == "material_issue_value":
                concept_ids.append("material_issue_value")
            elif metric_name == "current_stock":
                concept_ids.append("current_stock")
            else:
                concept_ids.append({
                    "material_consumption": "material_issued_quantity",
                    "material_price": "material_issue_price",
                    "attendance": "attendance_count",
                }.get(metric_name, metric_name))
        if semantic_plan.get("operation") == "count" and not metrics:
            if "purchase_order" in dimension_names:
                concept_ids.append("purchase_order_count")
            elif "invoice" in dimension_names or "billing_status" in dimension_names:
                concept_ids.append("client_bill_count")
            elif "project" in dimension_names:
                concept_ids.append("project_count")
            elif "vendor" in dimension_names:
                concept_ids.append("vendor_count")
            elif "store_issue" in dimension_names or "material_issue_line" in dimension_names:
                concept_ids.append("material_issue_count")
        return [
            dict(definitions[concept_id])
            for concept_id in self._unique(concept_ids)
            if concept_id in definitions
        ]

    def _bind_dates(self, semantic_plan, metrics, dimensions, catalog):
        if not semantic_plan.get("date_constraints"):
            return []
        if not metrics and not dimensions:
            return []
        candidates = self._context_candidates(
            catalog.get("date_fields", []),
            metrics,
            dimensions,
        )
        if not candidates:
            return []
        question = str(semantic_plan.get("original_question") or "").lower()
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in metrics
            if item.get("applicable_module")
        }
        if metric_modules:
            candidates = sorted(
                candidates,
                key=lambda item: (
                    0 if str(item.get("applicable_module") or "").lower() in metric_modules else 1,
                    -self._mapping_confidence(item),
                    item.get("table", ""),
                    item.get("column", ""),
                ),
            )
        if re.search(r"\b(?:project|site)s?\b", question) and re.search(r"\b(?:start|started|starting|commence|commenced)\b", question):
            candidates = sorted(
                candidates,
                key=lambda item: (
                    0 if item.get("column") == "project_start_date" else 1,
                    -self._mapping_confidence(item),
                ),
            )
        elif re.search(r"\b(?:project|site)s?\b", question) and re.search(r"\b(?:end|ending|finish|completion|complete)\b", question):
            candidates = sorted(
                candidates,
                key=lambda item: (
                    0 if item.get("column") == "project_end_date" else 1,
                    -self._mapping_confidence(item),
                ),
            )
        elif re.search(r"\b(?:validity|valid|expir(?:y|ing|e|es)|ending)\b", question):
            candidates = sorted(
                candidates,
                key=lambda item: (
                    0 if item.get("column") == "po_validity_date" else 1,
                    -self._mapping_confidence(item),
                ),
            )
        best = dict(candidates[0])
        best["authoritative"] = True
        return [best]

    def _bind_statuses(self, semantic_plan, metrics, dimensions, catalog):
        if not semantic_plan.get("status_constraints"):
            return []
        if not metrics and not dimensions:
            return []
        requested_values = self._status_values(semantic_plan)
        candidates = self._context_candidates(
            catalog.get("status_fields", []),
            metrics,
            dimensions,
        )
        compatible = [
            item for item in candidates
            if not requested_values
            or requested_values & {
                str(value).lower()
                for value in item.get("semantics", [])
            }
        ]
        if requested_values and not compatible:
            return []
        best_candidates = compatible or candidates
        if not best_candidates:
            return []
        best = dict(best_candidates[0])
        best["requested_values"] = sorted(requested_values)
        best["authoritative"] = True
        return [best]

    def _bind_relationships(self, required_tables, catalog):
        tables = self._unique(required_tables)
        if len(tables) < 2:
            return [], []
        edges = [
            item for item in catalog.get("join_paths", [])
            if str(item.get("verification_status") or "").startswith("verified")
            and item.get("runtime_approved") is True
        ]
        connected = {tables[0]}
        relationships = []
        unresolved = []
        for target in tables[1:]:
            if target in connected:
                continue
            path = self._find_path(connected, target, edges)
            if not path:
                unresolved.append({
                    "concept": target,
                    "role": "relationship",
                    "essential": True,
                    "reason": "No approved ERP relationship path connects the required tables.",
                })
                continue
            relationships.extend(path)
            connected.update(self._relationship_tables(path))
        return self._dedupe_mappings(
            relationships,
            ("left_table", "left_column", "right_table", "right_column"),
        ), unresolved

    def _find_path(self, starts, target, edges, max_hops=4):
        adjacency = {}
        for edge in edges:
            left = edge.get("left_table")
            right = edge.get("right_table")
            if not left or not right:
                continue
            adjacency.setdefault(left, []).append((right, edge))
            adjacency.setdefault(right, []).append((left, edge))
        queue = deque((start, [], {start}) for start in sorted(starts))
        candidates = []
        while queue:
            table, path, seen = queue.popleft()
            if len(path) >= max_hops:
                continue
            for next_table, edge in adjacency.get(table, []):
                if next_table in seen:
                    continue
                next_path = [*path, edge]
                if next_table == target:
                    candidates.append(next_path)
                    continue
                queue.append((next_table, next_path, {*seen, next_table}))
        if not candidates:
            return []
        return sorted(
            candidates,
            key=lambda path: (
                len(path),
                -min(self._mapping_confidence(item) for item in path),
                sum(self._duplicate_risk(item) for item in path),
            ),
        )[0]

    def _entity_locations(self, catalog):
        verified = catalog.get("verified_semantic_catalog") or {}
        result = {}
        for item in verified.get("entity_types", []):
            entity_type = self._canonical(item.get("entity_type"))
            locations = []
            for value in item.get("display_columns", []):
                location = self._qualified_location(value, "display")
                if location:
                    locations.append(location)
            for value in item.get("identifier_columns", []):
                location = self._qualified_location(value, "identifier")
                if location:
                    locations.append(location)
            result.setdefault(entity_type, []).extend(locations)
        for item in verified.get("business_concepts", []):
            entity_type = self._canonical(item.get("concept"))
            table = item.get("preferred_table")
            for column, role in (
                (item.get("display_column"), "display"),
                (item.get("preferred_column"), "identifier"),
            ):
                if table and column:
                    result.setdefault(entity_type, []).append({
                        "table": table,
                        "column": column,
                        "role": role,
                        "confidence_score": self._mapping_confidence(item),
                        "verification_status": item.get("verification_status"),
                    })
        return {
            entity_type: self._dedupe_mappings(
                locations,
                ("table", "column"),
            )
            for entity_type, locations in result.items()
        }

    def _relevant_resolution_items(self, metrics, dimensions, resolution):
        items = list(resolution.get("entities") or []) or list(
            resolution.get("matches") or []
        )
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in metrics
        }
        if "billing" in metric_modules:
            return {
                "entities": [
                    item for item in items
                    if item.get("table") == "project_mst"
                    or str(item.get("entity_type") or item.get("resolved_type") or "").lower()
                    in {"client"}
                ],
                "matches": [],
            }
        if metric_modules & {"purchase_orders"} or any(
            str(item.get("applicable_module") or "").lower() == "purchase_orders"
            for item in [*metrics, *dimensions]
        ):
            entities = []
            for item in items:
                entity_type = str(item.get("entity_type") or item.get("resolved_type") or "").lower()
                table = item.get("table")
                if table in {"project_mst", "po_form"} or entity_type in {"purchase_order"}:
                    entities.append(item)
                    continue
                if entity_type == "vendor":
                    cloned = dict(item)
                    cloned["table"] = "po_form"
                    cloned["column"] = "vendor_name"
                    cloned["primary_key_column"] = None
                    cloned["primary_key_value"] = None
                    cloned["primary_key"] = None
                    entities.append(cloned)
                    continue
            return {
                "entities": entities,
                "matches": [],
            }
        if metric_modules & {"vendors"} or any(
            str(item.get("applicable_module") or "").lower() == "vendors"
            for item in [*metrics, *dimensions]
        ):
            return {
                "entities": [
                    item for item in items
                    if item.get("table") in {"vendor_mst", "po_form", "vendor_branch_details", "vendor_types"}
                    or str(item.get("entity_type") or item.get("resolved_type") or "").lower()
                    in {"vendor", "location", "vendor_category"}
                ],
                "matches": [],
            }
        if metric_modules & {"inventory"} or any(
            str(item.get("applicable_module") or "").lower() == "inventory"
            for item in [*metrics, *dimensions]
        ):
            return {
                "entities": [
                    item for item in items
                    if item.get("table") in {"sub_material", "inventory_master"}
                    or str(item.get("entity_type") or item.get("resolved_type") or "").lower()
                    in {"material", "store"}
                ],
                "matches": [],
            }
        return resolution

    def _required_tables(self, metrics, dimensions, dates, statuses, resolution):
        tables = [
            item.get("table") or item.get("preferred_table")
            for item in [*metrics, *dimensions, *dates, *statuses]
        ]
        tables.extend(
            item.get("table")
            for item in [
                *(resolution.get("entities") or []),
                *(resolution.get("matches") or []),
            ]
        )
        return self._unique(tables)

    def _required_columns(
        self,
        metrics,
        dimensions,
        dates,
        statuses,
        relationships,
        resolution,
    ):
        result = {}
        for item in [*metrics, *dimensions, *dates, *statuses]:
            table = item.get("table") or item.get("preferred_table")
            column = item.get("column") or item.get("preferred_column")
            self._append_column(result, table, column)
        for item in relationships:
            self._append_column(result, item.get("left_table"), item.get("left_column"))
            self._append_column(result, item.get("right_table"), item.get("right_column"))
        for item in [
            *(resolution.get("entities") or []),
            *(resolution.get("matches") or []),
        ]:
            self._append_column(result, item.get("table"), item.get("column"))
            self._append_column(
                result,
                item.get("table"),
                item.get("primary_key_column"),
            )
        return result

    def _aggregation_grain(self, semantic_plan, metrics, dimensions):
        grain = [
            item.get("aggregation_grain")
            for item in metrics
            if item.get("aggregation_grain")
        ]
        grain.extend(
            item.get("dimension")
            for item in dimensions
            if item.get("dimension")
        )
        grain.extend(semantic_plan.get("grouping_hints") or [])
        if not grain:
            grain.extend(
                item.get("table")
                for item in metrics
                if item.get("table")
            )
        return self._unique(grain)

    def _catalog_unresolved(self, question, semantic_plan, verified):
        requested = {
            self._canonical(value)
            for value in semantic_plan.get("metrics") or []
        }
        question_terms = {
            self._canonical(value)
            for value in str(question or "").replace("-", " ").split()
        }
        result = []
        question_lower = str(question or "").lower()
        if re.search(r"\bvendors?\b|\bsuppliers?\b", question_lower) and re.search(r"\bperform(?:s|ance)?\b|\bbest\b|\bworst\b|\brating\b", question_lower):
            result.append({
                "concept": "vendor_performance",
                "role": "metric",
                "essential": True,
                "reason": "No verified vendor performance formula, delivery KPI, quality KPI, or score is defined for this ERP POC.",
                "alternatives": ["purchase value", "purchase order count"],
                "verification_status": "unresolved",
            })
        for item in verified.get("unresolved_definitions", []):
            concept = self._canonical(item.get("concept"))
            concept_terms = set(concept.split("_"))
            alias_match = any(
                str(alias).lower() in str(question or "").lower()
                for alias in item.get("aliases", [])
            )
            if (
                concept not in requested
                and not concept_terms <= question_terms
                and not alias_match
            ):
                continue
            result.append({
                "concept": item.get("concept"),
                "role": "metric",
                "essential": True,
                "reason": item.get("reason") or item.get("ambiguity_notes"),
                "alternatives": item.get("alternative_candidates") or [],
                "verification_status": "unresolved",
            })
        return result

    def _clarification_questions(self, semantic_plan, binding):
        questions = []
        for item in binding.unresolved_concepts:
            if not item.get("essential"):
                continue
            alternatives = item.get("alternatives") or []
            questions.append({
                "concept": item.get("concept"),
                "role": item.get("role"),
                "question": self._clarification_text(item),
                "options": alternatives[:6],
                "reason": item.get("reason"),
            })
        if semantic_plan.get("status_constraints") and not binding.bound_status_fields:
            questions.append({
                "concept": "status",
                "role": "status",
                "question": "Which ERP status field should this use?",
                "options": [],
                "reason": "No verified status field matches the requested context.",
            })
        if semantic_plan.get("date_constraints") and not binding.bound_date_fields:
            questions.append({
                "concept": "date",
                "role": "date",
                "question": "Which business date should define this period?",
                "options": [],
                "reason": "No verified date field matches the requested context.",
            })
        return self._dedupe_mappings(questions, ("concept", "role", "question"))

    def _clarification_text(self, item):
        concept = str(item.get("concept") or "requested concept").replace("_", " ")
        role = item.get("role")
        if role == "relationship":
            return f"How should {concept} be related to the other requested business data?"
        if role == "dimension":
            return f"Which ERP business dimension do you mean by {concept}?"
        return f"Which verified definition should be used for {concept}?"

    def _binding_confidence(self, result):
        if not result.authoritative:
            return 0, ["No authoritative ERP catalog was available."]
        score = 95
        reasons = ["Compatible verified ERP catalog loaded."]
        if result.bound_metrics:
            reasons.append("Requested metrics were bound to verified columns.")
        if result.bound_dimensions:
            reasons.append("Requested dimensions were bound to verified display columns.")
        if result.approved_relationships:
            reasons.append("Required tables are connected by approved ERP relationships.")
        if result.unresolved_concepts:
            score -= min(50, len(result.unresolved_concepts) * 15)
            reasons.append("Some requested concepts are not yet verified.")
        if result.clarification_required:
            score = min(score, 55)
        return max(0, score), reasons

    def _context_candidates(self, items, metrics, dimensions):
        modules = {
            item.get("applicable_module")
            for item in [*metrics, *dimensions]
            if item.get("applicable_module")
        }
        tables = {
            item.get("table")
            for item in [*metrics, *dimensions]
            if item.get("table")
        }
        verified = [
            item
            for item in items
            if str(item.get("verification_status") or "").startswith("verified")
        ]
        return sorted(
            verified,
            key=lambda item: (
                -(4 if item.get("table") in tables else 0),
                -(3 if item.get("applicable_module") in modules else 0),
                -self._mapping_confidence(item),
                item.get("table", ""),
                item.get("column", ""),
            ),
        )

    def _rank_metric_candidates(
        self,
        question,
        candidates,
        semantic_plan,
        requested_concept,
    ):
        question_lower = question.lower()
        requested = self._canonical(requested_concept)
        dimensions = {
            self._canonical(value)
            for value in semantic_plan.get("dimensions") or []
        }

        def score(item):
            metric = self._canonical(item.get("metric"))
            compatible = {
                self._canonical(value)
                for value in item.get("compatible_dimensions", [])
            }
            metric_phrase = metric.replace("_", " ")
            column_phrase = str(item.get("column") or "").replace("_", " ").lower()
            return (
                20 if metric == requested else 0,
                12 if metric_phrase and metric_phrase in question_lower else 0,
                len(dimensions & compatible) * 6,
                14 if column_phrase and column_phrase in question_lower else 0,
                18
                if requested == "amount_value"
                and "purchase order" in question_lower
                and "total" in str(item.get("column") or "").lower()
                else 0,
                18
                if requested in {"purchase_value", "amount_value"}
                and "item" in question_lower
                and item.get("table") == "po_items"
                else 0,
                18
                if requested in {"quantity", "ordered_quantity"}
                and "po" in question_lower
                and item.get("table") == "po_items"
                else 0,
                18
                if requested in {"purchase_value", "amount_value"}
                and "subtotal" in question_lower
                and item.get("column") == "sub_total"
                else 0,
                18
                if requested in {"project_value", "amount_value"}
                and re.search(r"\bproject\s+values?\b|\bprojects?\b|\bsite\b", question_lower)
                and item.get("table") == "project_mst"
                else 0,
                20
                if requested in {"requested_quantity", "quantity"}
                and item.get("metric") == "requested_quantity"
                else 0,
                18
                if requested in {"material_consumption", "quantity"}
                and re.search(r"\b(?:issued|issue|consumption|used|material)\b", question_lower)
                and item.get("column") == "issued_quantity"
                else 0,
                18
                if requested in {"requested_quantity", "quantity"}
                and "requested" in question_lower
                and item.get("column") == "requested_quantity"
                else 0,
                18
                if requested in {"material_issue_value", "amount_value"}
                and re.search(r"\b(?:material|issue|issued|store).*(?:value|price|pricing|cost)|(?:value|price|pricing|cost).*(?:material|issue|issued|store)\b", question_lower)
                and item.get("metric") == "material_issue_value"
                else 0,
                10
                if "gross" in question_lower
                and "gross" in str(item.get("column") or "").lower()
                else 0,
                4 if item.get("preferred_column") == item.get("column") else 0,
                self._mapping_confidence(item),
            )

        return sorted(
            candidates,
            key=lambda item: (
                *(-value for value in score(item)),
                item.get("table", ""),
                item.get("column", ""),
            ),
        )

    def _metric_names(self, item):
        names = {
            self._canonical(item.get("metric")),
            self._canonical(item.get("requested_concept")),
        }
        names.update(self._canonical(value) for value in item.get("semantic_aliases", []))
        return names

    def _dimension_names(self, item):
        names = {self._canonical(item.get("dimension"))}
        names.update(self._canonical(value) for value in item.get("semantic_aliases", []))
        return names

    def _dimension_is_essential(self, concept, semantic_plan):
        required = {
            self._canonical(value)
            for value in [
                *(semantic_plan.get("grouping_hints") or []),
                *(semantic_plan.get("requested_output") or []),
                *((semantic_plan.get("question_contract") or {}).get("required_dimensions") or []),
                *((semantic_plan.get("question_contract") or {}).get("required_outputs") or []),
            ]
        }
        return self._canonical(concept) in required

    def _status_values(self, semantic_plan):
        values = set()
        for item in semantic_plan.get("status_constraints") or []:
            if not isinstance(item, dict):
                continue
            values.update(
                str(value).lower()
                for value in (
                    item.get("values")
                    or [item.get("value") or item.get("raw_value")]
                )
                if value
            )
        return values

    def _relationship_tables(self, relationships):
        return self._unique([
            value
            for item in relationships
            for value in (item.get("left_table"), item.get("right_table"))
        ])

    def _qualified_location(self, value, role):
        if "." not in str(value):
            return None
        table, column = value.split(".", 1)
        return {
            "table": table,
            "column": column,
            "role": role,
            "verification_status": "verified",
            "confidence_score": 90,
        }

    def _append_column(self, result, table, column):
        if not table or not column:
            return
        result.setdefault(table, [])
        if column not in result[table]:
            result[table].append(column)

    def _canonical(self, value):
        normalized = str(value or "").strip().lower().replace(" ", "_")
        return self.CONCEPT_ALIASES.get(normalized, normalized)

    def _mapping_confidence(self, item):
        value = item.get("confidence_score", item.get("confidence", 0))
        if isinstance(value, str):
            return {"high": 90, "medium": 70, "low": 45}.get(value.lower(), 0)
        if not isinstance(value, (int, float)):
            return 0
        return int(round(value * 100 if 0 <= value <= 1 else value))

    def _duplicate_risk(self, item):
        risk = str(
            item.get("duplicate_amplification_risk")
            or item.get("row_multiplication_risk")
            or "low"
        ).lower()
        return {"low": 0, "medium": 1, "high": 2}.get(risk, 1)

    def _dedupe_unresolved(self, values):
        return self._dedupe_mappings(values, ("concept", "role"))

    def _dedupe_mappings(self, values, keys):
        result = []
        seen = set()
        for value in values:
            identity = tuple(str(value.get(key) or "").lower() for key in keys)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(value)
        return result

    def _unique(self, values):
        result = []
        seen = set()
        for value in values:
            if value in (None, ""):
                continue
            key = str(value).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result


erp_semantic_binder = ERPSemanticBinder()
