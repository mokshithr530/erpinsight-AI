import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClarificationPlan:
    required: bool
    module: str | None = None
    intent: str | None = None
    filled_slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    next_slot: str | None = None
    expected_answer_type: str | None = None
    expected_entity_types: list[str] = field(default_factory=list)
    question: str | None = None
    options: list[str] = field(default_factory=list)
    safe_unsupported: bool = False
    unsupported_reason: str | None = None

    def to_context(self, original_question: str) -> dict[str, Any]:
        return {
            "active": True,
            "original_question": original_question,
            "intent": self.intent,
            "module": self.module,
            "missing_slots": self.missing_slots,
            "filled_slots": self.filled_slots,
            "expected_entity_types": self.expected_entity_types,
            "current_expected_slot": self.next_slot,
            "requested_slots": [self.next_slot] if self.next_slot else [],
            "clarification_history": [],
            "options": self.options,
            "entity_options": self.options if self.next_slot in {"entity", "entity_pair"} else [],
            "metric_options": self.options if self.next_slot == "metric" else [],
            "expires_after_turns": 3,
            "turns_remaining": 3,
        }


class OrderedClarificationPlanner:
    MODULE_PRIORITY = [
        "billing",
        "purchase_orders",
        "project_master",
        "store_material",
        "inventory",
        "vendors",
    ]

    def plan(
        self,
        *,
        question: str,
        semantic_plan: dict[str, Any],
        entity_resolution: dict[str, Any] | None = None,
        semantic_binding: dict[str, Any] | None = None,
        existing_context: dict[str, Any] | None = None,
    ) -> ClarificationPlan:
        question_lower = question.lower()
        module = self._module(question_lower, semantic_plan)
        intent = self._intent(question_lower, semantic_plan)
        mentions = list(semantic_plan.get("candidate_entity_mentions") or [])
        metrics = [str(item).lower() for item in semantic_plan.get("metrics") or []]
        dimensions = [str(item).lower() for item in semantic_plan.get("dimensions") or []]
        filled = dict((existing_context or {}).get("filled_slots") or {})

        unsupported = self._unsupported_metric(question_lower, module)
        if unsupported:
            return ClarificationPlan(
                required=True,
                module=module,
                intent=intent,
                filled_slots=filled,
                missing_slots=[],
                next_slot="unsupported_metric",
                expected_answer_type="unsupported",
                question=unsupported,
                safe_unsupported=True,
                unsupported_reason=unsupported,
            )

        entity_choice = self._candidate_choice_plan(question, module, intent, filled, entity_resolution or {})
        if entity_choice:
            return entity_choice

        slots = self._required_slots(
            question_lower,
            module,
            intent,
            mentions,
            metrics,
            dimensions,
            semantic_plan,
            entity_resolution or {},
        )
        slots = [slot for slot in slots if not self._slot_filled(slot, filled, mentions, semantic_plan)]
        if not slots:
            return ClarificationPlan(required=False, module=module, intent=intent, filled_slots=filled)

        next_slot = slots[0]
        question_text, options, entity_types, answer_type = self._question_for_slot(
            next_slot,
            module,
            mentions,
            entity_resolution or {},
        )
        return ClarificationPlan(
            required=True,
            module=module,
            intent=intent,
            filled_slots=filled,
            missing_slots=slots,
            next_slot=next_slot,
            expected_answer_type=answer_type,
            expected_entity_types=entity_types,
            question=question_text,
            options=options,
        )

    def _module(self, question_lower: str, plan: dict[str, Any]) -> str | None:
        terms = set(plan.get("metrics") or []) | set(plan.get("dimensions") or []) | set(plan.get("requested_output") or [])
        joined = " ".join(sorted(str(item).lower() for item in terms))
        text = f"{question_lower} {joined}"
        if re.search(r"\b(purchase orders?|po|procurement)\b", text):
            return "purchase_orders"
        if re.search(r"\b(billing|bills?|invoice)\b", text):
            return "billing"
        if re.search(r"\b(inventory|stock|available material|low stock)\b", text):
            return "inventory"
        if re.search(r"\b(material movement|material issued|materials? issued|store transactions?|issues?)\b", text):
            return "store_material"
        if re.search(r"\b(vendors?|suppliers?)\b", text):
            return "vendors"
        if re.search(r"\b(projects?|sites?)\b", text):
            return "project_master"
        return None

    def _intent(self, question_lower: str, plan: dict[str, Any]) -> str:
        operation = plan.get("operation") or "unknown"
        if "compare" in question_lower or operation == "comparison":
            metrics = [
                str(item).lower()
                for item in (plan.get("metrics") or [])
                if item
            ]
            dimensions = {
                str(item).lower()
                for item in [
                    *(plan.get("dimensions") or []),
                    *(plan.get("requested_output") or []),
                    *(plan.get("requested_outputs") or []),
                ]
                if item
            }
            mentions = plan.get("candidate_entity_mentions") or []
            if len(metrics) >= 2 and not mentions and (
                dimensions
                or re.search(r"\b(?:by|per|project[- ]wise)\b", question_lower)
            ):
                return "metric_comparison"
            return "compare"
        if re.search(r"\b(top|highest|biggest|bottom|lowest)\b", question_lower) or operation == "ranking":
            return "ranking"
        if re.search(r"\b(latest|recent)\b", question_lower):
            return "latest"
        if re.search(r"\b(details?|summary)\b", question_lower):
            return "details"
        if re.search(r"\b(total|average|min|max|count|how many)\b", question_lower):
            return "aggregate"
        return operation

    def _required_slots(self, question_lower, module, intent, mentions, metrics, dimensions, plan, entity_resolution):
        slots: list[str] = []
        has_date = bool(plan.get("date_constraints"))
        has_status = bool(plan.get("status_constraints"))
        entity_needs_type = self._entity_needs_type(question_lower, mentions, entity_resolution)
        if module == "purchase_orders":
            if entity_needs_type:
                slots.append("entity_type")
            if intent == "compare":
                slots.append("entity_pair")
                slots.append("metric")
            elif intent == "ranking" and not self._has_po_metric(question_lower, metrics):
                slots.append("metric")
            elif mentions and not self._has_po_metric(question_lower, metrics):
                # Entity comes first; metric can be asked in the next turn if needed.
                slots.append("metric")
            if "pending" in question_lower and not has_status:
                slots.append("status")
        elif module == "billing":
            if entity_needs_type:
                slots.append("entity_type")
            if intent == "compare":
                slots.append("entity_pair")
            if intent == "latest" and not re.search(r"\b\d+\b", question_lower):
                slots.append("limit")
        elif module == "project_master":
            if intent == "details" and not mentions:
                slots.append("entity")
            if intent == "compare":
                slots.append("entity_pair")
            if "active" in question_lower and "status" not in question_lower and "end date" not in question_lower:
                slots.append("project_status")
        elif module == "store_material":
            if entity_needs_type:
                slots.append("entity_type")
            elif intent == "compare":
                slots.append("entity_pair")
            elif re.search(r"\b(material movement|store transactions?)\b", question_lower):
                slots.append("entity_scope")
            if not self._has_material_metric(question_lower, metrics):
                slots.append("metric")
        elif module == "inventory":
            if entity_needs_type:
                slots.append("entity_type")
            elif intent == "compare":
                slots.append("entity_scope")
                slots.append("entity_pair")
            if "low stock" in question_lower and not re.search(r"\b(?:below|under|less than)\s+\d+\b", question_lower):
                slots.append("threshold")
        elif module == "vendors":
            if intent == "compare":
                slots.append("entity_pair")
                slots.append("metric")
            elif intent == "ranking" and not self._has_vendor_metric(question_lower, metrics):
                slots.append("metric")
        if re.search(r"\b(march|january|february|april|may|june|july|august|september|october|november|december)\b", question_lower) and not re.search(r"\b20\d{2}\b", question_lower):
            slots.append("date_range")
        return list(dict.fromkeys(slots))

    def _candidate_choice_plan(self, question, module, intent, filled, entity_resolution):
        ambiguities = entity_resolution.get("ambiguities") or []
        if not ambiguities:
            unresolved = entity_resolution.get("unresolved") or []
            if unresolved:
                mention = unresolved[0].get("mention")
                types = unresolved[0].get("expected_entity_types") or ["project", "vendor", "material", "client"]
                if mention and re.fullmatch(r"ABC", str(mention), flags=re.IGNORECASE):
                    module_types = {
                        "purchase_orders": ["project", "vendor"],
                        "billing": ["project", "client"],
                        "store_material": ["project", "material"],
                        "inventory": ["material", "project", "store"],
                    }.get(module, types)
                    return ClarificationPlan(
                        required=True,
                        module=module,
                        intent=intent,
                        filled_slots=filled,
                        missing_slots=["entity_type"],
                        next_slot="entity_type",
                        expected_answer_type="entity_type",
                        expected_entity_types=module_types,
                        question=f"Is {mention} a {self._join_words(module_types)}?",
                        options=module_types,
                    )
                return ClarificationPlan(
                    required=True,
                    module=module,
                    intent=intent,
                    filled_slots=filled,
                    missing_slots=["entity"],
                    next_slot="entity",
                    expected_answer_type="entity",
                    expected_entity_types=types,
                    question=f"I couldn't find a verified {self._join_words(types)} matching '{mention}'. Please choose a verified name.",
                    options=[],
                    safe_unsupported=True,
                    unsupported_reason="No verified entity match was found.",
                )
            return None
        ambiguity = ambiguities[0]
        mention = ambiguity.get("mention")
        options = list(dict.fromkeys(
            str(match.get("matched_value") or match.get("value"))
            for match in ambiguity.get("options") or []
            if match.get("matched_value") or match.get("value")
        ))
        if len(options) <= 1 and ambiguity.get("reason") != "entity_type_ambiguity":
            return None
        entity_types = list(dict.fromkeys(
            str(match.get("entity_type"))
            for match in ambiguity.get("options") or []
            if match.get("entity_type")
        ))
        if (
            ambiguity.get("reason") == "entity_type_ambiguity"
            or (not entity_types and mention and re.fullmatch(r"ABC", str(mention), flags=re.IGNORECASE))
        ):
            if not entity_types:
                entity_types = {
                    "purchase_orders": ["project", "vendor"],
                    "billing": ["project", "client"],
                    "store_material": ["project", "material"],
                    "inventory": ["material", "project", "store"],
                }.get(module, ["project", "vendor", "material", "client"])
            return ClarificationPlan(
                required=True,
                module=module,
                intent=intent,
                filled_slots=filled,
                missing_slots=["entity_type"],
                next_slot="entity_type",
                expected_answer_type="entity_type",
                expected_entity_types=entity_types,
                question=f"Is {mention} a {self._join_words(entity_types)}?",
                options=entity_types,
            )
        entity_label = entity_types[0] if entity_types else "entity"
        return ClarificationPlan(
            required=True,
            module=module,
            intent=intent,
            filled_slots=filled,
            missing_slots=["entity"],
            next_slot="entity",
            expected_answer_type="entity",
            expected_entity_types=entity_types,
            question=f"I found multiple {entity_label}s matching '{mention}'. Which one did you mean?",
            options=options,
        )

    def _entity_needs_type(self, question_lower, mentions, entity_resolution):
        if not mentions:
            return False
        if re.search(r"\bABC\b", " ".join(mentions), re.IGNORECASE):
            return True
        if entity_resolution.get("clarification_required"):
            return True
        unresolved = entity_resolution.get("unresolved") or []
        ambiguities = entity_resolution.get("ambiguities") or []
        return bool(unresolved or ambiguities)

    def _slot_filled(self, slot, filled, mentions, plan):
        if slot == "entity_type":
            return bool(filled.get("entity_type") and (filled.get("entity") or mentions))
        if slot == "entity":
            return bool(filled.get("entity") or mentions)
        if slot == "entity_pair":
            return bool(
                (filled.get("entity_1") and filled.get("entity_2"))
                or len(mentions) >= 2
            )
        if slot == "metric":
            return bool(filled.get("metric") or plan.get("metrics"))
        if slot == "date_range":
            return bool(filled.get("date_range") or plan.get("date_constraints"))
        if slot == "status":
            return bool(filled.get("status") or plan.get("status_constraints"))
        if slot == "threshold":
            return bool(filled.get("threshold") or plan.get("numeric_constraints"))
        if slot == "limit":
            return bool(filled.get("limit") or plan.get("limit"))
        if slot == "entity_scope":
            dimensions = {
                str(item).strip().lower()
                for item in plan.get("dimensions") or []
            }
            return bool(
                filled.get("entity_scope")
                or filled.get("entity_type")
                or dimensions & {"project", "material", "store"}
            )
        return bool(filled.get(slot))

    def _question_for_slot(self, slot, module, mentions, entity_resolution):
        mention = mentions[0] if mentions else None
        if slot == "entity_type":
            types = {
                "purchase_orders": ["project", "vendor"],
                "billing": ["project", "client"],
                "store_material": ["project", "material"],
                "inventory": ["material", "project", "store"],
            }.get(module, ["project", "vendor", "material", "client", "store"])
            return (
                f"Is {mention} a {self._join_words(types)}?",
                types,
                types,
                "entity_type",
            )
        if slot == "entity":
            label = "project" if module == "project_master" else "entity"
            options = (
                [
                    "RDSS-VIZAG",
                    "RDSS-UGCABLE",
                    "RDSS-NELLORE",
                    "NKLIS (PACKAGE-1)",
                    "HRDCL",
                ]
                if module == "project_master"
                else []
            )
            return f"Which {label} should I show?", options, [label], "entity"
        if slot == "entity_pair":
            labels = {
                "vendors": "vendors",
                "purchase_orders": "projects or vendors",
                "billing": "projects or clients",
                "project_master": "projects",
                "store_material": "projects or materials",
                "inventory": "materials, projects, or stores",
            }.get(module, "items")
            options = {
                "vendors": [
                    "POLYCAB INDIA LIMITED and KEI Industries",
                    "POLYCAB INDIA LIMITED and STEEL AUTHORITY OF INDIA LIMITED",
                ],
                "purchase_orders": [
                    "RDSS-VIZAG and RDSS-UGCABLE",
                    "RDSS-VIZAG and RDSS-NELLORE",
                    "POLYCAB INDIA LIMITED and KEI Industries",
                ],
                "billing": [
                    "RDSS-VIZAG and RDSS-UGCABLE",
                    "RDSS-VIZAG and RDSS-NELLORE",
                ],
                "project_master": [
                    "RDSS-VIZAG and RDSS-UGCABLE",
                    "RDSS-VIZAG and RDSS-NELLORE",
                ],
                "store_material": [
                    "RDSS-VIZAG and RDSS-UGCABLE",
                    "HIGH SPEED DIESEL (HSD) and 11KV FABRICATION MATERIAL",
                ],
            }.get(module, [])
            return f"Which two {labels} would you like to compare?", options, labels.split(" or "), "entity_pair"
        if slot == "metric":
            options = self._metric_options(module)
            subject = {
                "vendors": "vendors",
                "purchase_orders": "purchase orders",
                "billing": "billing",
                "store_material": "material movement",
                "inventory": "inventory",
            }.get(module, "this")
            return f"Should I compare {options[0]} or {options[1]} for {subject}?", options, [], "metric"
        if slot == "status":
            return "Which verified status should I filter by?", [], [], "status"
        if slot == "project_status":
            options = ["Project status is active", "Project end date has not passed"]
            return "Should active mean the project status is active, or that the project end date has not passed?", options, [], "status"
        if slot == "date_range":
            return "Which year or date range should I use?", [], [], "date_range"
        if slot == "threshold":
            return "What stock quantity should be considered low?", [], [], "threshold"
        if slot == "limit":
            options = ["5 recent bills", "10 recent bills", "20 recent bills"]
            return "How many recent bills should I show?", options, [], "limit"
        if slot == "entity_scope":
            options = ["project", "material", "store"]
            return "Do you want this for a project, material, or store?", options, options, "entity_scope"
        return "Which specific value should I use?", [], [], slot

    def _metric_options(self, module):
        return {
            "purchase_orders": ["PO total value", "PO count"],
            "vendors": ["PO total value", "PO count"],
            "billing": ["billing value", "bill count"],
            "store_material": ["material issued quantity", "material requested quantity"],
            "inventory": ["current stock quantity", "material count"],
        }.get(module, ["total value", "count"])

    def _unsupported_metric(self, question_lower, module):
        if module == "vendors" and re.search(r"\b(performance|credit|payment history|delivery kpi|risk score)\b", question_lower):
            return (
                "Vendor performance is not currently defined in the verified catalog. "
                "I can compare vendors by PO total value or PO count."
            )
        return None

    def _has_po_metric(self, question_lower, metrics):
        return bool(
            {"purchase_value", "purchase_order_count"} & set(metrics)
            or re.search(r"\b(total value|po value|po count|order count|item value|subtotal)\b", question_lower)
            or (
                re.search(r"\bprocurement\b", question_lower)
                and re.search(r"\b(top|highest|biggest|largest|vendor|supplier)\b", question_lower)
            )
        )

    def _has_vendor_metric(self, question_lower, metrics):
        return self._has_po_metric(question_lower, metrics)

    def _has_material_metric(self, question_lower, metrics):
        return bool(
            {"material_issues", "material_consumption", "current_stock"} & set(metrics)
            or re.search(r"\b(issued|used|requested|returned|movement|quantity|stock)\b", question_lower)
        )

    def _join_words(self, words):
        if len(words) <= 1:
            return words[0] if words else "value"
        return ", ".join(words[:-1]) + f", or {words[-1]}"


ordered_clarification_planner = OrderedClarificationPlanner()
