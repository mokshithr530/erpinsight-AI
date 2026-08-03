from config import (
    LLM_POLISH_VERIFIED_ANSWERS,
    LIVE_DATABASE_CACHE_TTL_SECONDS,
    MAX_RETRIES,
    MYSQL_LLM_FULL_SCHEMA_MAX_COLUMNS,
    MYSQL_LLM_SCHEMA_MODE,
)
from llm import llm
from schema_reader import SchemaReader
from session_memory import session_memory
from sql_executor import sql_executor
from validator import validator
from metrics import token_metrics
from response_cache import response_cache
from sqlserver_adapter import sqlserver_adapter
from mysql_adapter import mysql_adapter
from confidence_engine import confidence_engine
from database_memory import database_memory
import logging
import re
from database_fingerprint import sha256_text, stable_json
from entity_resolver import entity_resolver
from question_classifier import question_classifier
from semantic_parser import semantic_parser
from schema_profile_history import schema_profile_history
from query_plan import query_plan_guard
from erp_semantic_binder import erp_semantic_binder
from erp_sql_context import erp_sql_context_builder
from verified_execution import (
    build_verified_execution_receipt,
    verified_fact_payload,
)
from verified_sql_compiler import verified_sql_compiler
from clarification_planner import ordered_clarification_planner

logger = logging.getLogger(__name__)


OPTION_CODE_PATTERN = re.compile(r"\b(\d+)\s*([A-Za-z])\b")


class ChatPipeline:
    _CASUAL_STARTERS = {
        "hello",
        "hi",
        "hey",
        "hii",
        "yo",
        "good morning",
        "good afternoon",
        "good evening",
    }

    def _ensure_database(self):
        database = session_memory.get_database()

        if not database["database_name"]:
            raise ValueError("Upload a database before asking questions.")

    def _validate_question(self, question):
        if not question or not question.strip():
            raise ValueError("Question cannot be empty.")

        return question.strip()

    def is_casual_message(self, question):
        normalized = " ".join(question.strip().lower().split())
        stripped = normalized.rstrip("!.?")
        return stripped in self._CASUAL_STARTERS

    def casual_response(self, has_database=False):
        if has_database:
            return {
                "success": True,
                "answer": (
                    "Hey, I am ready. Ask me something about the uploaded database, "
                    "like top products, revenue, order status, or customer trends."
                ),
                "sql": None,
                "result": None,
                "error": None,
                "metrics": token_metrics.get_metrics_summary(),
                "cache_hit": False,
            }
        return {
            "success": True,
            "answer": (
                "Hello. Upload a database first, then I can help you ask questions "
                "about sales, products, orders, customers, risk, or trends."
            ),
            "sql": None,
            "result": None,
            "error": None,
            "metrics": token_metrics.get_metrics_summary(),
            "cache_hit": False,
        }

    def merge_clarification_followup(self, question, context):
        question = self._validate_question(question)
        if not context or not context.get("active"):
            return {
                "handled": False,
                "question": question,
                "unrelated": False,
                "filled_slots": {},
            }
        if self._looks_like_new_question(question, context):
            return {
                "handled": False,
                "question": question,
                "unrelated": True,
                "filled_slots": {},
            }
        filled = self._clarification_followup_slots(question, context)
        if not filled:
            return {
                "handled": False,
                "question": question,
                "unrelated": False,
                "filled_slots": {},
            }
        merged_question = self._merge_clarification_question(
            context.get("original_question") or "",
            filled,
        )
        return {
            "handled": True,
            "question": merged_question,
            "unrelated": False,
            "filled_slots": filled,
        }

    def _looks_like_new_question(self, question, context):
        normalized = " ".join(str(question or "").lower().split())
        if OPTION_CODE_PATTERN.search(normalized):
            return False
        if re.match(r"^(what|which|how|show|list|compare|give|tell)\b", normalized):
            parsed = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
            if (
                len(normalized.split()) >= 5
                and (
                    parsed.get("metrics")
                    or parsed.get("dimensions")
                    or parsed.get("candidate_entity_mentions")
                )
            ):
                return True
        if re.match(r"^(what|which|how|show|list|compare|give|tell)\b", normalized):
            original = " ".join(str(context.get("original_question") or "").lower().split())
            original_terms = {
                term
                for term in re.findall(r"[a-z0-9]+", original)
                if len(term) >= 4
            }
            new_terms = {
                term
                for term in re.findall(r"[a-z0-9]+", normalized)
                if len(term) >= 4
            }
            return not bool(original_terms & new_terms)
        if len(normalized.split()) <= 5:
            return False
        original = " ".join(str(context.get("original_question") or "").lower().split())
        original_terms = {
            term
            for term in re.findall(r"[a-z0-9]+", original)
            if len(term) >= 4
        }
        new_terms = {
            term
            for term in re.findall(r"[a-z0-9]+", normalized)
            if len(term) >= 4
        }
        if original_terms and len(original_terms & new_terms) >= 1:
            return False
        return bool(re.search(r"\b(show|list|compare|what|which|how|give|tell|count)\b", normalized))

    def _canonicalize_mysql_question(self, question):
        text = str(question or "").strip()
        po_compare = re.search(
            r"^\s*compare\s+(?:po|purchase\s+order)\s+(?:spend|amount|total\s+value|value)\s+for\s+(.+?)\s+(?:versus|vs\.?|and)\s+(.+?)[?.!]*\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if po_compare:
            return (
                "Compare purchase order total value by project for "
                f"project {po_compare.group(1).strip()} and project {po_compare.group(2).strip()}."
            )
        if re.search(r"\btop\b.+\bmaterials?\s+(?:used|issued)\s+from\s+stores?\b", text, flags=re.IGNORECASE):
            return "Show top ten materials issued."
        replacements = [
            (r"\bPO\s+spend\b", "purchase order total value"),
            (r"\bPO\s+amount\b", "purchase order total value"),
            (r"\bPOs?\s+by\s+amount\b", "purchase orders by total value"),
            (r"\bbiggest\s+(\w+)\s+POs?\b", r"top \1 purchase orders"),
            (r"\bmaterials?\s+used\s+from\s+stores?\b", "materials issued from stores"),
        ]
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def _clarification_followup_slots(self, question, context):
        normalized = " ".join(str(question or "").strip().split())
        lower = normalized.lower()
        filled = dict(context.get("filled_slots") or {})
        expected_slot = context.get("current_expected_slot")
        if not expected_slot:
            missing_slots = context.get("missing_slots") or []
            if len(missing_slots) == 1:
                expected_slot = missing_slots[0]
        if expected_slot:
            expected = self._fill_expected_clarification_slot(
                expected_slot,
                normalized,
                lower,
                context,
            )
            option_values = self._clarification_option_values(context, lower)
            slot_hints = self._slot_hints_for_followup(lower)
            for slot, value in option_values.items():
                expected.setdefault(slot, value)
            for slot, value in slot_hints.items():
                expected.setdefault(slot, value)
            if (
                expected_slot == "metric"
                and "entity" not in expected
                and self._looks_like_entity_followup(normalized)
            ):
                entity_text = self._clean_followup_entity_text(normalized, slot_hints)
                if entity_text and self._looks_like_entity_followup(entity_text):
                    expected["entity"] = entity_text
            if (
                expected_slot in {"metric", "entity_pair"}
                and not {"entity_1", "entity_2"}.issubset(expected)
            ):
                entity_text = self._clean_followup_entity_text(normalized, slot_hints)
                parts = self._split_entity_pair(entity_text)
                if len(parts) >= 2:
                    expected["entity_1"] = parts[0]
                    expected["entity_2"] = parts[1]
            for slot, value in expected.items():
                filled[slot] = value
            return {
                key: value
                for key, value in filled.items()
                if value not in (None, "")
            }
        slot_hints = self._slot_hints_for_followup(lower)
        option_values = self._clarification_option_values(context, lower)
        for slot, value in option_values.items():
            filled[slot] = value
        for slot, value in slot_hints.items():
            filled[slot] = value

        if (
            "entity" in (context.get("missing_slots") or [])
            and "entity" not in filled
            and "metric" not in slot_hints
        ):
            if self._looks_like_entity_followup(normalized):
                filled["entity"] = normalized
        if (
            {"entity_1", "entity_2"} & set(context.get("missing_slots") or [])
            and not {"entity_1", "entity_2"}.issubset(filled)
        ):
            entities = self._split_entity_pair(normalized)
            if len(entities) >= 2:
                filled["entity_1"] = entities[0]
                filled["entity_2"] = entities[1]
        return {
            key: value
            for key, value in filled.items()
            if value not in (None, "")
        }

    def _fill_expected_clarification_slot(self, expected_slot, normalized, lower, context):
        option_pick = self._letter_option_value(context, normalized, expected_slot)
        if expected_slot == "entity_type":
            entity_type = self._entity_type_from_text(
                str(option_pick or lower).lower(),
                context.get("expected_entity_types") or [],
            )
            values = {}
            if entity_type:
                values["entity_type"] = entity_type
            if self._looks_like_entity_followup(normalized) and normalized.lower() != str(entity_type or "").lower():
                values["entity"] = normalized
                if not entity_type:
                    inferred = self._infer_entity_type(normalized, context.get("expected_entity_types") or [])
                    if inferred:
                        values["entity_type"] = inferred
            return values
        if expected_slot == "entity":
            if option_pick:
                filled_slots = context.get("filled_slots") or {}
                if filled_slots.get("entity_1") and filled_slots.get("entity_2"):
                    return {"entity_2": option_pick}
                return {"entity": option_pick}
            entity_text = self._clean_followup_entity_text(normalized, self._slot_hints_for_followup(lower))
            return {"entity": entity_text} if self._looks_like_entity_followup(entity_text) else {}
        if expected_slot == "entity_pair":
            if option_pick:
                parts = self._split_entity_pair(option_pick)
                if len(parts) >= 2:
                    return {"entity_1": parts[0], "entity_2": parts[1]}
                return {"entity": option_pick}
            parts = self._split_entity_pair(self._clean_followup_entity_text(normalized, self._slot_hints_for_followup(lower)))
            if len(parts) >= 2:
                return {"entity_1": parts[0], "entity_2": parts[1]}
            return {}
        if expected_slot == "metric":
            option_values = self._clarification_option_values(context, lower)
            metric = option_pick or option_values.get("metric") or self._slot_hints_for_followup(lower).get("metric")
            return {"metric": metric} if metric else {}
        if expected_slot == "date_range":
            return {"date_range": normalized}
        if expected_slot in {"status", "project_status"}:
            if option_pick:
                return {"status": option_pick}
            return {"status": normalized}
        if expected_slot == "threshold":
            match = re.search(r"\d+(?:\.\d+)?", lower)
            return {"threshold": match.group(0) if match else normalized}
        if expected_slot == "limit":
            if option_pick:
                match = re.search(r"\d+", str(option_pick))
                return {"limit": match.group(0) if match else option_pick}
            match = re.search(r"\d+", lower)
            return {"limit": match.group(0) if match else normalized}
        if expected_slot == "entity_scope":
            entity_type = self._entity_type_from_text(str(option_pick or lower).lower(), ["project", "material", "store"])
            return {"entity_scope": entity_type} if entity_type else {}
        return {}

    def _letter_option_value(self, context, normalized, expected_slot=None):
        text = str(normalized or "").strip()
        direct = re.fullmatch(r"([A-Za-z])", text)
        compact = re.fullmatch(r"\d+\s*([A-Za-z])", text)
        prefixed = re.match(r"^([A-Za-z])\s*[.):-]\s+.+", text)
        if not direct and not compact and not prefixed:
            return None
        index = ord((direct or compact or prefixed).group(1).upper()) - ord("A")
        if index < 0:
            return None
        if expected_slot == "metric":
            options = context.get("metric_options") or context.get("options") or []
        elif expected_slot in {"entity_type", "entity_scope"}:
            options = context.get("expected_entity_types") or context.get("options") or []
        elif expected_slot in {"entity", "entity_pair"}:
            options = context.get("entity_options") or context.get("options") or []
        else:
            options = (
                context.get("entity_options")
                or context.get("metric_options")
                or context.get("options")
                or []
            )
        if index < len(options):
            return options[index]
        expected = context.get("expected_entity_types") or []
        if index < len(expected):
            return expected[index]
        return None

    def _entity_type_from_text(self, lower, expected_types):
        for entity_type in expected_types:
            if str(entity_type).lower().strip("s") in lower:
                return str(entity_type).lower().strip("s")
        return None

    def _infer_entity_type(self, text, expected_types):
        lower = str(text or "").lower()
        expected = {str(item).lower().strip("s") for item in expected_types}
        if "project" in expected and (re.search(r"\brdss|apdrp|project|package|construction|nanded|vizag|ugcable\b", lower) or "-" in lower):
            return "project"
        if "vendor" in expected and re.search(r"\blimited|ltd|industries|traders|steel|polycab|vendor\b", lower):
            return "vendor"
        if "material" in expected and re.search(r"\bconductor|pole|cable|cement|steel|material|sqmm|nos|km\b", lower):
            return "material"
        if "client" in expected and re.search(r"\bclient|apspdcl|apepdcl|government|company\b", lower):
            return "client"
        if "store" in expected and "store" in lower:
            return "store"
        return next(iter(expected), None) if expected and self._looks_like_entity_followup(text) else None

    def _clarification_option_values(self, context, lower):
        values = {}
        missing = set(context.get("missing_slots") or [])
        for number, letter in OPTION_CODE_PATTERN.findall(lower):
            index = ord(letter.upper()) - ord("A")
            if index < 0:
                continue
            if number == "1" and "date_range" in missing:
                values["date_range"] = [
                    "latest available 30 days",
                    "last 30 calendar days",
                    "custom date range",
                ][min(index, 2)]
            elif number in {"1", "2"} and {"entity", "entity_1", "entity_2"} & missing:
                if index == 0:
                    values["entity_rule"] = "auto-pick"
                else:
                    options = context.get("entity_options") or []
                    if index - 1 < len(options):
                        if {"entity_1", "entity_2"} & missing:
                            pair = self._split_entity_pair(options[index - 1])
                            if len(pair) >= 2:
                                values["entity_1"], values["entity_2"] = pair[0], pair[1]
                            else:
                                values["entity"] = options[index - 1]
                        else:
                            values["entity"] = options[index - 1]
            if "metric" in missing or "metric" in (context.get("missing_slots") or []):
                if (number == "1" and "date_range" not in missing and not ({"entity", "entity_1", "entity_2"} & missing)) or number in {"2", "3"}:
                    metrics = context.get("metric_options") or []
                    if index < len(metrics):
                        values["metric"] = metrics[index]
        return values

    def _slot_hints_for_followup(self, lower):
        slots = {}
        if re.search(r"\b(total value|po value|purchase order value|purchase value)\b", lower):
            slots["metric"] = "PO total value"
        elif re.search(r"\b(po count|order count|number of orders|count)\b", lower):
            slots["metric"] = "PO count"
        elif re.search(r"\b(item value|line item|line-item)\b", lower):
            slots["metric"] = "PO item value"
        elif re.search(r"\b(subtotal|sub total)\b", lower):
            slots["metric"] = "PO subtotal"
        elif re.search(r"\b(billing|bill value|client billing)\b", lower):
            slots["metric"] = "Billing value"
        elif re.search(r"\b(issued|used|consumed)\b", lower):
            slots["metric"] = "Material issued quantity"
        elif re.search(r"\b(requested)\b", lower):
            slots["metric"] = "Material requested quantity"
        elif re.search(r"\b(returned|return)\b", lower):
            slots["metric"] = "Material returned quantity"

        if re.search(r"\b(financial year|fy)\b", lower):
            slots["date_range"] = lower
        elif re.search(r"\b(calendar year|last year|this year|march|january|february|april|may|june|july|august|september|october|november|december|\d{4})\b", lower):
            slots["date_range"] = lower
        return slots

    def _clean_followup_entity_text(self, text, slot_hints=None):
        cleaned = str(text or "").strip()
        hints = {str(value).lower() for value in (slot_hints or {}).values()}
        removable_phrases = {
            "po total value",
            "po count",
            "po item value",
            "po subtotal",
            "billing value",
            "material issued quantity",
            "material requested quantity",
            "material returned quantity",
            "issued quantity",
            "requested quantity",
            "returned quantity",
            "current stock quantity",
            "total value",
            "purchase order value",
            "purchase value",
            "issue date",
            "approval date",
        } | hints
        for phrase in sorted(removable_phrases, key=len, reverse=True):
            if phrase:
                cleaned = re.sub(rf"\b{re.escape(phrase)}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(using|with|by|metric|measure)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
        return cleaned

    def _looks_like_entity_followup(self, text):
        if OPTION_CODE_PATTERN.fullmatch(text.strip()) or re.fullmatch(r"[A-Za-z]", text.strip()):
            return False
        return bool(
            re.search(r"[A-Za-z]", text)
            and (
                re.search(r"[-&/0-9]", text)
                or text.isupper()
                or len(text.split()) <= 5
            )
        )

    def _split_entity_pair(self, text):
        cleaned = re.sub(r"^\s*compare\s+", "", str(text), flags=re.IGNORECASE)
        parts = re.split(r"\s+(?:and|vs|versus)\s+", cleaned, flags=re.IGNORECASE)
        return [part.strip(" .") for part in parts if part.strip(" .")]

    def _merge_clarification_question(self, original_question, filled):
        base = original_question.rstrip(" .?")
        if filled.get("entity") and filled.get("entity_type"):
            base = re.sub(
                r"\bABC\b",
                f"{filled['entity_type']} {filled['entity']}",
                base,
                flags=re.IGNORECASE,
            )
        elif filled.get("entity"):
            base = re.sub(
                r"\bABC\b",
                str(filled["entity"]),
                base,
                flags=re.IGNORECASE,
            )
        elif filled.get("entity_type"):
            base = re.sub(
                r"\bABC\b",
                f"{filled['entity_type']} ABC",
                base,
                flags=re.IGNORECASE,
            )
        canonical = self._canonical_clarified_question(base, filled)
        if canonical:
            return canonical
        parts = [base]
        if filled.get("entity_1") and filled.get("entity_2"):
            parts.append(f"for {filled['entity_1']} and {filled['entity_2']}")
        elif filled.get("entity") and filled["entity"].lower() not in base.lower():
            parts.append(f"for {filled['entity']}")
        elif filled.get("entity_rule") == "auto-pick":
            parts.append("using an auto-picked relevant project/site")
        if filled.get("metric"):
            parts.append(f"using {filled['metric']}")
        if filled.get("date_range"):
            parts.append(f"for {filled['date_range']}")
        if filled.get("status"):
            parts.append(f"with status meaning {filled['status']}")
        if filled.get("threshold"):
            parts.append(f"below {filled['threshold']}")
        if filled.get("entity_scope"):
            parts.append(f"for {filled['entity_scope']}")
        if filled.get("limit"):
            parts.append(f"limit {filled['limit']}")
        return " ".join(parts).strip() + "."

    def _canonical_clarified_question(self, base, filled):
        lower = base.lower()
        metric = str(filled.get("metric") or "").strip()
        metric_lower = metric.lower()
        entity = filled.get("entity")
        entity_1 = filled.get("entity_1")
        entity_2 = filled.get("entity_2")
        date_range = filled.get("date_range")
        date_text = f" for {date_range}" if date_range else ""
        if re.search(r"\bproject\b", lower) and re.search(r"\b(detail|details|summary)\b", lower) and entity:
            return f"Show project details for {entity}."
        if re.search(r"\b(material|store|inventory)\b", lower):
            if not metric:
                return None
            metric_text = "material issued quantity"
            if "requested" in metric_lower:
                metric_text = "material requested quantity"
            elif "return" in metric_lower:
                metric_text = "material returned quantity"
            elif "price" in metric_lower or "rate" in metric_lower:
                metric_text = "material unit price"
            elif "stock" in metric_lower or "inventory" in metric_lower:
                metric_text = "current stock quantity"
            dimension = filled.get("entity_type") or self._entity_dimension_hint(entity or entity_1 or "")
            if dimension not in {"project", "material", "store", "vendor", "client"}:
                dimension = "project"
            if entity_1 and entity_2:
                return f"Compare {metric_text} by {dimension} for {dimension} {entity_1} and {dimension} {entity_2}{date_text}."
            if entity:
                return f"Show {metric_text} by {dimension} for {dimension} {entity}{date_text}."
            return f"Show {metric_text}{date_text}."
        if "cost" in lower and "project" in lower:
            if not metric:
                return None
            if "material" in metric_lower or "base charge" in metric_lower:
                return f"Which project has the highest material issue value{date_text}."
            if "po" in metric_lower or "purchase" in metric_lower or "total value" in metric_lower:
                return f"Which project has the highest purchase order total value{date_text}."
        if "vendor" in lower:
            if not metric:
                return None
            metric_text = "purchase value"
            if "count" in metric_lower:
                metric_text = "purchase order count"
            elif "subtotal" in metric_lower:
                metric_text = "purchase order subtotal"
            elif "item" in metric_lower:
                metric_text = "purchase order item value"
            if entity_1 and entity_2:
                return f"Compare vendors by {metric_text} for {entity_1} and {entity_2}{date_text}."
            if entity:
                return f"Show vendor {metric_text} for {entity}{date_text}."
            return f"{base} using {metric_text}{date_text}."
        if "purchase order" in lower or re.search(r"\bpo\b", lower):
            if not metric:
                return None
            metric_text = "total value"
            if "count" in metric_lower:
                metric_text = "count"
            elif "subtotal" in metric_lower:
                metric_text = "subtotal"
            elif "item" in metric_lower:
                metric_text = "item value"
            elif "total value" in metric_lower or "purchase value" in metric_lower:
                metric_text = "total value"
            dimension = self._entity_dimension_hint(entity or entity_1 or "")
            if filled.get("entity_type") in {"project", "vendor"}:
                dimension = filled["entity_type"]
            if entity_1 and entity_2:
                return f"Compare purchase order {metric_text} by {dimension} for {dimension} {entity_1} and {dimension} {entity_2}{date_text}."
            if entity:
                return f"Show purchase order {metric_text} by {dimension} for {dimension} {entity}{date_text}."
            if metric:
                return f"{base} by PO {metric_text}{date_text}."
        return None

    def _entity_dimension_hint(self, entity):
        text = str(entity or "").lower()
        if any(word in text for word in ["limited", "ltd", "industries", "traders", "steel authority", "polycab"]):
            return "vendor"
        return "project"

    def _expected_entity_types_from_question(self, question):
        lower = str(question or "").lower()
        if re.search(r"\b(material movement|material issued|materials? issued|materials?\s+used|material\s+used|store transactions?|issues?)\b", lower):
            return ["project", "material", "store"]
        if re.search(r"\b(vendors?|suppliers?)\b", lower):
            return ["vendor"]
        if re.search(r"\b(?:for|by|compare)\s+project\b", lower):
            return ["project"]
        if re.search(r"\b(?:for|by|compare)\s+vendor\b", lower):
            return ["vendor"]
        if re.search(r"\b(?:for|by|compare)\s+material\b", lower):
            return ["material"]
        if re.search(r"\b(?:for|by|compare)\s+client\b", lower):
            return ["client"]
        if re.search(r"\b(purchase orders?|po|procurement)\b", lower):
            return ["project", "vendor"]
        if re.search(r"\b(billing|bills?|invoice)\b", lower):
            return ["project", "client"]
        if re.search(r"\b(inventory|stock|available material|low stock)\b", lower):
            return ["material", "project", "store"]
        expected = []
        hints = {
            "project": r"\b(projects?|sites?)\b",
            "vendor": r"\b(vendors?|suppliers?)\b",
            "material": r"\b(materials?|items?|products?)\b",
            "client": r"\b(clients?|customers?)\b",
            "store": r"\b(stores?|warehouses?)\b",
        }
        for entity_type, pattern in hints.items():
            if re.search(pattern, lower):
                expected.append(entity_type)
        return expected or None

    def build_clarification_context(self, question, response):
        if response.get("clarification_context"):
            context = dict(response["clarification_context"])
            context["original_question"] = question
            return context
        if not response or response.get("sql") or response.get("result"):
            return None
        answer = str(response.get("answer") or "")
        if not self._is_clarification_answer(answer):
            return None
        missing_slots = self._missing_slots_from_answer(question, answer, response)
        if not missing_slots:
            return None
        return {
            "active": True,
            "original_question": question,
            "intent": response.get("intent"),
            "module": self._module_from_question(question, response),
            "missing_slots": missing_slots,
            "filled_slots": {},
            "expected_entity_types": self._expected_entity_types(question, answer),
            "entity_options": self._entity_options_from_answer(answer),
            "metric_options": self._metric_options_from_answer(answer),
            "expires_after_turns": 3,
            "turns_remaining": 3,
            "last_answer": answer,
        }

    def remaining_clarification_slots(self, context, filled):
        missing = []
        for slot in context.get("missing_slots") or []:
            if slot == "entity_type" and filled.get("entity_type"):
                continue
            if slot == "entity" and (
                filled.get("entity") or filled.get("entity_rule")
            ):
                continue
            if slot in {"entity_1", "entity_2"} and filled.get(slot):
                continue
            if slot == "metric" and filled.get("metric"):
                continue
            if slot == "date_range" and filled.get("date_range"):
                continue
            if slot == "limit" and filled.get("limit"):
                continue
            missing.append(slot)
        return missing

    def clarification_response_for_remaining(self, context, filled, database, database_identity):
        updated = dict(context)
        updated["filled_slots"] = filled
        updated["missing_slots"] = self.remaining_clarification_slots(context, filled)
        merged = self._merge_clarification_question(context.get("original_question") or "", filled)
        return self.ask_mysql(merged, database, database_identity)

    def _is_clarification_answer(self, answer):
        lower = answer.lower()
        return any(
            marker in lower
            for marker in [
                "need more clarification",
                "need one clarification",
                "which ",
                "do you mean",
                "option",
                "please specify",
                "before i run",
            ]
        )

    def _missing_slots_from_answer(self, question, answer, response):
        answer_core = str(answer or "").split("**Related questions that could be asked**", 1)[0]
        answer_core = answer_core.split("**Reply with", 1)[0]
        answer_core = answer_core.split("**To continue", 1)[0]
        lower = answer_core.lower()
        question_lower = question.lower()
        slots = []
        has_placeholder_entity = bool(re.search(r"\bABC\b", question, re.IGNORECASE))
        metric_only_clarification = any(
            phrase in lower
            for phrase in [
                "what should this value mean",
                "which po measure",
                "which material quantity",
                "should i check issued",
                "should i compare po total value",
                "what material quantity",
                "which verified definition",
            ]
        )
        if has_placeholder_entity or (
            not metric_only_clarification
            and any(word in lower for word in ["project", "vendor", "client", "material", "entity", "abc"])
        ):
            if "compare" in question_lower or "two " in lower or "side by side" in lower:
                slots.extend(["entity_1", "entity_2"])
            else:
                slots.append("entity")
        if any(word in lower for word in ["metric", "measure", "value", "quantity", "count", "amount", "po total", "billing value"]):
            slots.append("metric")
        if any(word in lower for word in ["date", "time period", "year", "today"]) or re.search(
            r"\b(last|latest|recent|this year|march|january|february|april|may|june|july|august|september|october|november|december)\b",
            question_lower,
        ):
            slots.append("date_range")
        if response.get("entity_resolution", {}).get("unresolved"):
            slots.append("entity")
        return list(dict.fromkeys(slots))

    def _expected_entity_types(self, question, answer):
        text = f"{question} {answer}".lower()
        entity_types = []
        for name in ["project", "vendor", "client", "material", "store"]:
            if name in text:
                entity_types.append(name)
        return entity_types

    def _module_from_question(self, question, response):
        text = f"{question} {response.get('intent') or ''}".lower()
        if "purchase order" in text or re.search(r"\bpo\b", text):
            return "purchase_orders"
        if "billing" in text or "bill" in text:
            return "billing"
        if "inventory" in text or "stock" in text:
            return "inventory"
        if "material" in text or "store" in text:
            return "store_material"
        if "vendor" in text:
            return "vendors"
        if "project" in text:
            return "projects"
        return response.get("intent")

    def _entity_options_from_answer(self, answer):
        options = []
        for line in str(answer or "").splitlines():
            match = re.match(r"\*\*[A-Z]\.\*\*\s+(?:Compare\s+\*\*)?(.+?)(?:\*\*)?$", line.strip())
            if match:
                text = re.sub(r"^\s*Compare\s+", "", match.group(1).strip("* "))
                if text and not any(word in text.lower() for word in ["auto-pick", "i will type", "po ", "material ", "billing ", "count", "value", "quantity"]):
                    options.append(text)
        return options[:8]

    def _metric_options_from_answer(self, answer):
        options = []
        for line in str(answer or "").splitlines():
            match = re.match(r"\*\*([A-Z])\.\*\*\s+(.+?)$", line.strip())
            if not match:
                match = re.match(r"\*\*([A-Z])\.\s+(.+?)\*\*$", line.strip())
            if match:
                text = match.group(2).strip()
                if any(word in text.lower() for word in ["po", "billing", "material", "stock", "quantity", "value", "count", "subtotal", "base charge"]):
                    options.append(text)
        return options[:8]

    def _llm_cache_identity(self):
        provider = getattr(llm, "provider", None)
        return {
            "provider": getattr(provider, "provider_name", None),
            "model": getattr(provider, "model_name", None),
        }

    def _dataframe_payload(self, dataframe):
        return {
            "rows": len(dataframe),
            "columns": list(dataframe.columns),
            "records": dataframe.to_dict(orient="records"),
        }

    def _confidence_response(self, **signals):
        return confidence_engine.build(**signals)

    def _public_response(self, response):
        public = dict(response)
        public.pop("metrics", None)
        return public

    def _referenced_columns(self, sql, database):
        if not sql:
            return []
        schema = mysql_adapter.inspect_schema(database)
        sql_lower = sql.lower()
        columns = []
        for info in schema.values():
            for column in info["columns"]:
                name = column["column_name"]
                if re.search(rf"`?{re.escape(name.lower())}`?", sql_lower):
                    columns.append(name)
        return sorted(set(columns))[:30]

    def _mysql_alias_map(self, sql):
        if not sql:
            return {}
        aliases = {}
        pattern = re.compile(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*)`?\s+(?:AS\s+)?`?([A-Za-z_][\w]*)`?",
            re.IGNORECASE,
        )
        for table, alias in pattern.findall(sql):
            if alias.upper() not in {"ON", "WHERE", "GROUP", "ORDER", "LIMIT", "JOIN", "LEFT", "RIGHT", "INNER"}:
                aliases[alias] = table
        return aliases

    def _repair_mysql_unknown_alias_column(self, sql, database, error_message):
        match = re.search(r"Unknown column '([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)'", str(error_message))
        if not match:
            return None
        bad_alias, column = match.groups()
        alias_map = self._mysql_alias_map(sql)
        if bad_alias not in alias_map:
            return None
        schema = mysql_adapter.inspect_schema(database)
        candidates = []
        for alias, table in alias_map.items():
            table_columns = {
                item["column_name"].lower()
                for item in schema.get(table, {}).get("columns", [])
            }
            if column.lower() in table_columns:
                candidates.append(alias)
        if not candidates:
            return None
        replacement_alias = candidates[0]
        if replacement_alias == bad_alias:
            return None
        return re.sub(
            rf"\b{re.escape(bad_alias)}\.`?{re.escape(column)}`?\b",
            f"{replacement_alias}.`{column}`",
            sql,
        )

    def _has_inferred_join(self, sql):
        return bool(sql and re.search(r"\bJOIN\b", sql, re.IGNORECASE))

    def _result_signature(self, result):
        return sha256_text(stable_json({
            "columns": result.get("columns", []),
            "row_count": result.get("rows"),
            "first_rows": result.get("records", [])[:5],
        }))

    def _mysql_validate_with_plan(self, sql, database, query_plan):
        validation = (
            mysql_adapter.validate_sql(sql, database)
            if sql
            else {"valid": False, "errors": ["No SQL query was generated."], "warnings": []}
        )
        if not validation.get("valid"):
            return validation
        schema = None
        if query_plan.catalog_authoritative:
            try:
                schema = mysql_adapter.inspect_schema(database)
            except Exception as error:
                logger.warning(
                    "Plan-aware schema inspection unavailable; validating against query-plan context only: %s",
                    error,
                )
        plan_validation = query_plan_guard.validate_sql(
            sql,
            query_plan,
            schema=schema,
        )
        return {
            "valid": bool(validation.get("valid")) and bool(plan_validation["valid"]),
            "errors": [*validation.get("errors", []), *plan_validation["errors"]],
            "warnings": [*validation.get("warnings", []), *plan_validation["warnings"]],
        }

    def _looks_like_empty_mysql_answer(self, question, dataframe):
        question_lower = question.lower()
        if dataframe is None or dataframe.empty:
            return True
        if not (
            re.search(r"\bhow\s+many\b|\bcount\b|\bnumber of\b|\btotal number\b", question_lower)
            or re.search(r"\b(?:list|show|top|highest|largest|biggest|maximum|max)\b", question_lower)
        ):
            return False
        if len(dataframe) == 0:
            return True
        first = dataframe.iloc[0].to_dict()
        for column, value in first.items():
            column_lower = str(column).lower()
            if any(marker in column_lower for marker in ("count", "total", "row_count")):
                try:
                    return float(value or 0) == 0
                except (TypeError, ValueError):
                    return False
        return False

    def _is_date_window_question(self, question):
        return bool(
            re.search(r"\b(last|past|previous|between|from|to|days?|weeks?|months?|year|date|today|recent|recently|latest)\b", question.lower())
        )

    def _mysql_cache_identity(self, question, database, database_identity, *, resolution=None):
        cache_identity = self._llm_cache_identity()
        schema_fp = mysql_adapter.schema_fingerprint(database)
        return response_cache.identity_for(
            question,
            database_fingerprint=mysql_adapter.database_fingerprint(database, database_identity),
            schema_fingerprint=schema_fp,
            dialect="mysql",
            source_identity=mysql_adapter.source_identity(database),
            provider=cache_identity["provider"],
            model=cache_identity["model"],
            session_scope=None,
            resolution=resolution,
        )

    def _mysql_should_clarify_after_failure(self, question, semantic_plan, errors=None):
        question_lower = question.lower()
        errors_text = " ".join(errors or []).lower()
        analytical = (
            semantic_plan.get("metrics")
            or semantic_plan.get("dimensions")
            or semantic_plan.get("date_constraints")
            or semantic_plan.get("operation") in {"ranking", "comparison", "aggregation", "sum", "trend", "business_insight"}
        )
        if not analytical:
            return False
        if any(
            marker in question_lower
            for marker in (
                "cost", "purchase", "billing", "invoice", "material", "stock",
                "project", "site", "vendor", "client", "employee", "department",
            )
        ):
            return True
        return any(
            marker in errors_text
            for marker in ("missing", "unresolved", "unknown", "group", "date", "status", "entity", "safe")
        )

    def _mysql_should_clarify_empty_result(self, question, semantic_plan, dataframe):
        if not self._looks_like_empty_mysql_answer(question, dataframe):
            return False
        question_lower = question.lower()
        has_cost_ambiguity = any(word in question_lower for word in ["cost", "price", "value", "amount"])
        has_flow_ambiguity = any(word in question_lower for word in ["material", "purchase", "purchased", "received", "issued", "returned", "used"])
        has_scope_ambiguity = any(word in question_lower for word in ["project", "site", "vendor", "client", "department", "any one"])
        has_date_window = bool(semantic_plan.get("date_constraints")) or self._is_date_window_question(question)
        return has_cost_ambiguity and has_flow_ambiguity and (has_scope_ambiguity or has_date_window)

    def _mysql_should_clarify_before_sql(self, question, semantic_plan, entity_resolution):
        question_lower = question.lower()
        if entity_resolution.get("entities") or entity_resolution.get("matches"):
            return False
        has_material = any(word in question_lower for word in ["material", "materials", "item", "items"])
        has_purchase_flow = any(word in question_lower for word in ["purchase", "purchased", "procurement", "po"])
        has_issue_flow = any(word in question_lower for word in ["issued", "issue", "used", "usage", "consumption", "returned", "return"])
        has_cost = any(word in question_lower for word in ["cost", "costs", "price", "value", "amount"])
        asks_scope = any(word in question_lower for word in ["project", "site", "any one", "which one", "one of"])
        explicit_auto_scope = bool(re.search(r"\b(?:choose|pick|select|use)\s+(?:any\s+)?one\b|\bany one\b", question_lower))
        has_date = bool(semantic_plan.get("date_constraints")) or self._is_date_window_question(question)
        if has_material and has_purchase_flow and has_cost and (asks_scope or has_date):
            return True
        if has_material and has_issue_flow and has_date and asks_scope and explicit_auto_scope and "return" not in question_lower:
            return False
        return has_material and has_issue_flow and has_date and asks_scope

    def _prefer_project_master_details(self, question, semantic_binding, entity_resolution):
        question_lower = str(question or "").lower()
        asks_project_details = (
            re.search(r"\b(details?|summary|about|info|information)\b", question_lower)
            and not re.search(
                r"\b(billing|bill|purchase|po|vendor|material|stock|inventory|issue|issued|cost|expense|subcontract|workorder)\b",
                question_lower,
            )
        )
        has_project_entity = any(
            str(match.get("entity_type") or "").lower() == "project"
            and str(match.get("table") or "").lower() == "project_mst"
            for match in entity_resolution.get("matches", [])
            if isinstance(match, dict)
        )
        if not (asks_project_details and has_project_entity):
            return semantic_binding
        binding = dict(semantic_binding or {})
        binding.update({
            "authoritative": True,
            "clarification_required": False,
            "required_tables": ["project_mst"],
            "required_columns": {
                "project_mst": [
                    "project_id",
                    "project_name",
                    "client_name",
                    "project_state",
                    "project_value",
                    "project_start_date",
                    "project_end_date",
                    "status",
                ]
            },
            "bound_metrics": [],
            "bound_dimensions": [{
                "requested_concept": "project",
                "bound_concept": "project",
                "table": "project_mst",
                "column": "project_name",
                "confidence": 95,
            }],
            "approved_relationships": [],
            "unresolved_concepts": [],
            "warnings": [],
            "reasons": [
                "Project details question with a resolved project entity uses Project Master.",
            ],
        })
        return binding

    def _mysql_clarification_response(
        self,
        *,
        question,
        database,
        intent,
        semantic_plan,
        query_plan=None,
        schema_context=None,
        errors=None,
    ):
        answer = self._mysql_clarification_answer(
            question=question,
            database=database,
            semantic_plan=semantic_plan,
            query_plan=query_plan,
            schema_context=schema_context,
            errors=errors,
        )
        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()
        return self._public_response({
            "success": True,
            "answer": answer,
            "sql": None,
            "result": None,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
            "intent": intent,
            "confidence": self._confidence_response(
                intent=intent,
                question=question,
                selected_tables=(schema_context or {}).get("tables", []),
                validation_ok=True,
                execution_ok=True,
                assumptions=1,
                limitations=["Clarification is required before running a safe query."],
            ),
        })

    def _mysql_binding_clarification_response(
        self,
        *,
        question,
        intent,
        semantic_binding,
    ):
        lines = [
            "**I need more clarification so I do not compare the wrong thing.**",
            "",
        ]
        for item in semantic_binding.get("clarification_questions", [])[:4]:
            lines.append(f"**{item.get('question') or 'Please clarify the requested business definition.'}**")
            for index, option in enumerate(item.get("options") or [], start=1):
                lines.append(f"**{chr(64 + index)}.** {self._friendly_clarification_option(option)}")
            reason = item.get("reason")
            if reason:
                lines.append(f"Why I am asking: {reason}")
            lines.append("")
        lines.append(
            "Reply with the option letter, or type the exact meaning you want."
        )
        related = self._mysql_related_clarification_questions(question)
        if related:
            lines.extend(["", "**Related questions that could be asked**"])
            lines.extend(f"- {item}" for item in related)
        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()
        return self._public_response({
            "success": True,
            "answer": "\n".join(lines),
            "sql": None,
            "result": None,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
            "intent": intent,
            "semantic_binding": semantic_binding,
            "confidence": self._confidence_response(
                intent=intent,
                question=question,
                selected_tables=semantic_binding.get("required_tables") or [],
                validation_ok=True,
                execution_ok=True,
                assumptions=1,
                limitations=[
                    "A required ERP business definition is unresolved or ambiguous."
                ],
            ),
        })

    def _mysql_ordered_clarification_response(self, *, question, intent, plan):
        lines = []
        if plan.safe_unsupported:
            lines.append(plan.question)
        else:
            lines.append(f"**{plan.question}**")
            if plan.options:
                lines.append("")
                for index, option in enumerate(plan.options[:6], start=1):
                    lines.append(f"**{chr(64 + index)}.** {option}")
            lines.extend([
                "",
                "Reply with the option letter or the exact value.",
            ])
        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()
        return self._public_response({
            "success": True,
            "answer": "\n".join(lines).strip(),
            "sql": None,
            "result": None,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
            "intent": intent,
            "clarification_context": plan.to_context(question),
            "confidence": self._confidence_response(
                intent=intent,
                question=question,
                validation_ok=True,
                execution_ok=True,
                assumptions=1,
                limitations=[
                    plan.unsupported_reason
                    if plan.safe_unsupported
                    else "A required clarification slot must be filled before querying."
                ],
            ),
        })

    def _mysql_clarification_answer(
        self,
        *,
        question,
        database,
        semantic_plan,
        query_plan=None,
        schema_context=None,
        errors=None,
    ):
        question_lower = question.lower()
        opener = self._clarification_phrase(question, "opener", [
            "**I need more clarification so I do not compare the wrong thing.**",
            "**I can help with this, but I need one detail first so the answer stays accurate.**",
            "**Before I run the report, I need to confirm the meaning so I do not pick the wrong data.**",
            "**This is answerable, but I need a little more detail to choose the right ERP data.**",
        ])
        lines = [
            opener,
            "",
        ]
        section_number = 1
        reply_parts = []

        if semantic_plan.get("date_constraints"):
            lines.extend([
                f"**{section_number}. {self._clarification_phrase(question, 'date_title', [
                    'Which date range should I use?',
                    'Which time period should this report cover?',
                    'What date window should I apply?',
                ])}**",
                "**A.** Latest 30 days available in the database",
                "**B.** Last 30 calendar days from today",
                "**C.** I will type a custom date range",
                "",
            ])
            reply_parts.append(f"{section_number}A")
            section_number += 1
        elif any(word in question_lower for word in ["last", "recent", "date", "month", "year", "quarter"]):
            lines.extend([
                f"**{section_number}. {self._clarification_phrase(question, 'date_title', [
                    'Which date range should I use?',
                    'Which time period should this report cover?',
                    'What date window should I apply?',
                ])}**",
                "**A.** The date range mentioned in your question",
                "**B.** Latest available transaction window in the database",
                "**C.** I will type a custom from-date and to-date",
                "",
            ])
            reply_parts.append(f"{section_number}A")
            section_number += 1

        asks_project_scope = any(
            phrase in question_lower
            for phrase in [
                "project", "site", "any one", "any project", "one project",
                "which project", "compare",
            ]
        ) or (
            any(word in question_lower for word in ["material", "materials"])
            and any(word in question_lower for word in ["purchase", "purchased", "procurement", "cost", "value"])
        )
        project_options = (
            self._mysql_project_clarification_options(database, question)
            if asks_project_scope
            else []
        )
        project_options = self._rotate_clarification_options(project_options, question, "project")
        if project_options:
            if "compare" in question_lower and len(project_options) >= 2:
                lines.append(f"**{section_number}. {self._clarification_phrase(question, 'compare_project_title', [
                    'Which projects should I compare?',
                    'Which two projects should I put side by side?',
                    'What project pair should I use for the comparison?',
                ])}**")
                lines.append("**A.** Auto-pick the top 2 projects/sites based on the selected metric")
                lines.append(f"**B.** Compare **{project_options[0]} vs {project_options[1]}**")
                if len(project_options) >= 3:
                    lines.append(f"**C.** Compare **{project_options[0]} vs {project_options[2]}**")
                lines.append("**D.** I will type the project/site names")
            else:
                lines.append(f"**{section_number}. {self._clarification_phrase(question, 'project_title', [
                    'Which project or site should I use?',
                    'Which project/site should I focus on?',
                    'Should I use one project, or should I auto-pick the busiest one?',
                ])}**")
                lines.append("**A.** Auto-pick the top project/site based on the selected metric")
                for index, option in enumerate(project_options[:5], start=2):
                    lines.append(f"**{chr(64 + index)}.** {option}")
                lines.append(f"**{chr(64 + len(project_options[:5]) + 2)}.** I will type the project/site name")
            lines.append("")
            reply_parts.append(f"{section_number}A")
            section_number += 1

        metric_options = self._mysql_metric_clarification_options(database, question, schema_context)
        if metric_options:
            metric_title = "What should this value mean?"
            if "po" in question_lower or "purchase order" in question_lower:
                metric_title = self._clarification_phrase(question, "po_metric_title", [
                    'What should "by PO" mean?',
                    'Which PO measure should I compare?',
                    'How should I measure the purchase orders?',
                ])
            elif any(word in question_lower for word in ["cost", "price", "value", "amount"]):
                metric_title = self._clarification_phrase(question, "value_metric_title", [
                    "Which cost or value should I use?",
                    "What value should this report calculate?",
                    "Which amount should I treat as the main metric?",
                ])
            elif any(word in question_lower for word in ["material", "materials"]) and any(word in question_lower for word in ["quantity", "qty", "used", "issued"]):
                metric_title = self._clarification_phrase(question, "material_metric_title", [
                    "Which material quantity should I check?",
                    "What material quantity do you want to see?",
                    "Should I check issued, requested, or returned material quantity?",
                ])
            lines.append(f"**{section_number}. {metric_title}**")
            for index, option in enumerate(metric_options[:5], start=1):
                label, _, description = str(option).partition("\n")
                lines.append(f"**{chr(64 + index)}. {label}**")
                if description.strip():
                    lines.append(description.strip())
            lines.append("")
            reply_parts.append(f"{section_number}A")
            section_number += 1

        reason_lines = self._friendly_clarification_reasons(question, errors)
        if reason_lines:
            lines.extend([
                "**Why I am asking**",
                *[f"- {item}" for item in reason_lines],
                "",
            ])

        if reply_parts:
            lines.extend([
                f"**{self._clarification_phrase(question, 'reply_title', [
                    'Reply with just the option combo, like:',
                    'You can answer using only the option codes, like:',
                    'To continue, send the option codes like:',
                ])}**",
                "",
                f"`{' '.join(reply_parts)}`",
                "",
                self._clarification_phrase(question, "reply_hint", [
                    "You can also type the exact project, date range, or metric if you prefer.",
                    "Or type the exact project, period, or metric in your own words.",
                    "Full text is also fine if you want to be more specific.",
                ]),
            ])

        related = self._mysql_related_clarification_questions(question)
        if related:
            lines.extend(["", "**Related questions that could be asked**"])
            lines.extend(f"- {item}" for item in related)
        return "\n".join(lines)

    def _clarification_variant_index(self, question, salt, size):
        if size <= 1:
            return 0
        digest = sha256_text(f"{salt}:{question or ''}")
        return int(digest[:8], 16) % size

    def _clarification_phrase(self, question, salt, options):
        if not options:
            return ""
        return options[self._clarification_variant_index(question, salt, len(options))]

    def _rotate_clarification_options(self, options, question, salt):
        clean_options = [option for option in (options or []) if option]
        if len(clean_options) <= 1:
            return clean_options
        index = self._clarification_variant_index(question, salt, len(clean_options))
        return clean_options[index:] + clean_options[:index]

    def _mysql_project_clarification_options(self, database, question):
        try:
            schema = mysql_adapter.inspect_schema(database)
            row_counts = mysql_adapter._approx_row_counts(database)
        except Exception:
            return []
        project_table = mysql_adapter._best_entity_table(
            schema,
            mysql_adapter._table_aliases_for_entity("project"),
            row_counts=row_counts,
        )
        if not project_table:
            return []
        columns = schema[project_table]["columns"]
        name_column = mysql_adapter._best_entity_name_column(columns, "project")
        id_column = mysql_adapter._best_entity_id_column(columns, "project")
        if not name_column:
            return []
        order_column = id_column or name_column
        sql = (
            f"SELECT {mysql_adapter._quote_identifier(name_column)} AS project_name "
            f"FROM {mysql_adapter._quote_identifier(project_table)} "
            f"WHERE {mysql_adapter._quote_identifier(name_column)} IS NOT NULL "
            f"AND TRIM(CAST({mysql_adapter._quote_identifier(name_column)} AS CHAR)) <> '' "
            f"ORDER BY {mysql_adapter._quote_identifier(order_column)} LIMIT 5"
        )
        try:
            rows = mysql_adapter.execute_read_query(sql, database)
        except Exception:
            return []
        return [
            str(row.get("project_name"))
            for row in rows.to_dict(orient="records")
            if row.get("project_name")
        ]

    def _mysql_metric_clarification_options(self, database, question, schema_context=None):
        question_lower = question.lower()
        asks_material = any(word in question_lower for word in ["material", "materials", "item", "items"])
        asks_issue = any(word in question_lower for word in ["used", "usage", "issued", "issue", "consumption", "consumed"])
        asks_return = any(word in question_lower for word in ["returned", "return"])
        asks_quantity = any(word in question_lower for word in ["quantity", "qty", "how much", "used", "issued", "returned"])
        asks_purchase = any(word in question_lower for word in ["purchase", "purchased", "procurement", "po"])
        asks_cost = any(word in question_lower for word in ["cost", "costs", "price", "value", "amount"])
        if asks_material and (asks_issue or asks_return or asks_quantity) and not asks_purchase and not asks_cost:
            return [
                "Material issued quantity\nQuantity issued or used from stores.",
                "Material requested quantity\nQuantity requested for issue/use.",
                "Material returned quantity\nQuantity returned, only if the ERP has a verified return transaction/status.",
            ]
        if asks_material and asks_issue and asks_cost and not asks_purchase:
            return [
                "Material issued value\nIssued quantity multiplied by verified issue-line rate.",
                "Material issued quantity\nQuantity issued or used from stores.",
                "Material unit price\nUnit/base price used on the issue line.",
            ]
        try:
            catalog = mysql_adapter.business_catalog(database)
        except Exception:
            catalog = {"metrics": []}
        selected_tables = set((schema_context or {}).get("tables") or [])
        metric_rows = []
        for metric in catalog.get("metrics", []):
            name = str(metric.get("metric") or "")
            table = metric.get("table")
            column = metric.get("column")
            if not table or not column:
                continue
            text = " ".join([name, table, column]).lower()
            if selected_tables and table not in selected_tables and not any(word in text for word in question_lower.split()):
                continue
            if any(marker in question_lower for marker in ["material", "purchase", "cost"]):
                allowed = ["material", "purchase", "cost", "price", "value", "amount", "quantity", "received", "issued"]
                if asks_material and (asks_issue or asks_quantity) and not asks_purchase:
                    allowed = ["material", "quantity", "issued", "usage", "used", "requested", "return"]
                if not any(marker in text for marker in allowed):
                    continue
            metric_rows.append((metric.get("confidence", 0), name, table, column))

        options = []
        seen = set()
        label_overrides = {
            "purchase_value": "PO total value\nTotal purchase order amount.",
            "vendor_purchase_order_count": "PO count\nNumber of purchase orders.",
            "purchase_order_count": "PO count\nNumber of purchase orders.",
            "material_price": "Material unit price\nUnit or base price for a material.",
            "material_consumption": "Material issued quantity\nQuantity issued or consumed from stores.",
            "material_receipts": "Material received quantity\nQuantity received into stores.",
            "current_stock": "Current stock quantity\nQuantity currently available in inventory.",
            "billing": "Billing value\nTotal client billing amount.",
            "amount_value": "Amount value\nA verified amount field from the selected business flow.",
            "cost": "Cost value\nA verified cost or expense field, if available.",
        }
        physical_overrides = {
            ("po_form", "po_form_id"): "PO count\nNumber of purchase orders.",
            ("po_form", "total_value"): "PO total value\nTotal purchase order amount.",
            ("po_form", "sub_total"): "PO subtotal\nSubtotal before final adjustments.",
            ("po_items", "total_price"): "PO item value\nTotal value from purchase order line items.",
            ("project_mst", "project_value"): "Project value\nContract or project value.",
            ("client_bill_raising", "gross_bill_value"): "Billing value\nTotal client billing amount.",
            ("store_material_usage_quantity", "requested_quantity"): "Material requested quantity\nQuantity requested for issue/use.",
            ("store_material_usage_quantity", "issued_quantity"): "Material issued quantity\nQuantity issued or used from stores.",
            ("store_material_usage_quantity", "base_charge"): "Material unit price\nUnit/base price used on the issue line.",
        }
        for _confidence, metric, table, column in sorted(metric_rows, reverse=True):
            key = (metric, table, column)
            if key in seen:
                continue
            seen.add(key)
            label = physical_overrides.get((table, column)) or label_overrides.get(metric)
            if not label:
                label = f"{metric.replace('_', ' ').title()}\nUse this verified business metric."
            options.append(label)

        if not options and asks_material and (asks_issue or asks_quantity):
            options = [
                "Material issued quantity\nQuantity issued or used from stores.",
                "Material requested quantity\nQuantity requested for issue/use.",
                "Material issued value\nIssued quantity multiplied by verified issue-line rate, if available.",
            ]
        elif not options and any(word in question_lower for word in ["po", "cost", "purchase", "material"]):
            options = [
                "PO total value\nTotal purchase order amount.",
                "PO count\nNumber of purchase orders.",
                "PO item value\nTotal value from purchase order line items.",
                "PO subtotal\nSubtotal before final adjustments.",
            ]
        return options[:5]

    def _friendly_clarification_option(self, option):
        text = str(option or "").strip()
        text = re.sub(r"\s+from\s+`?[A-Za-z0-9_]+\.[A-Za-z0-9_]+`?", "", text)
        replacements = {
            "purchase value": "PO total value",
            "purchase order count": "PO count",
            "gross billing": "Billing value",
            "employee count": "Employee count",
            "attendance count": "Attendance count",
        }
        return replacements.get(text.lower(), text)

    def _friendly_clarification_reasons(self, question, errors=None):
        question_lower = question.lower()
        reasons = []
        if any(phrase in question_lower for phrase in ["any 2", "any two", "compare"]):
            reasons.append("A comparison needs either named projects/sites or a clear auto-pick rule.")
        if "po" in question_lower or "purchase order" in question_lower:
            reasons.append("PO can mean total amount, order count, subtotal, or line-item value.")
        if any(word in question_lower for word in ["cost", "value", "amount", "price"]):
            reasons.append("Cost/value has multiple verified meanings in this ERP.")
        if any(word in question_lower for word in ["last", "recent", "month", "year", "quarter"]):
            reasons.append("Date-based reports need a clear date window and date field.")
        if not reasons and errors:
            reasons.append("The first query idea was not specific enough to run safely.")
        return reasons[:4]

    def _mysql_related_clarification_questions(self, question):
        question_lower = question.lower()
        if "po" in question_lower or "purchase order" in question_lower:
            if "compare" in question_lower or "project" in question_lower:
                return self._rotate_clarification_options([
                    "Compare top 2 projects by PO total value",
                    "Compare top 2 projects by PO count",
                    "Compare RDSS-VIZAG and RDSS-UGCABLE by PO total value",
                    "Compare RDSS-VIZAG and Yavatmal-Nanded by PO item value",
                    "Which project has the highest PO total value?",
                ], question, "related_po_project")
            if "vendor" in question_lower:
                return self._rotate_clarification_options([
                    "Show top 5 vendors by PO total value",
                    "Show vendor-wise PO count",
                    "Compare two vendors by PO total value",
                ], question, "related_po_vendor")
            return self._rotate_clarification_options([
                "Show PO total value by project",
                "Show PO count by vendor",
                "Show top 5 purchase orders by value",
            ], question, "related_po")
        if any(word in question_lower for word in ["billing", "bill"]):
            return self._rotate_clarification_options([
                "Show billing by project",
                "Compare top 2 projects by billing value",
                "Show top 5 clients by billing value",
            ], question, "related_billing")
        if any(word in question_lower for word in ["material", "store", "stock", "inventory"]):
            return self._rotate_clarification_options([
                "Show top materials issued in the last 30 days",
                "Show current stock by material",
                "Show material movement for one project",
            ], question, "related_material")
        return self._rotate_clarification_options([
            "Show top 10 projects by project value",
            "Show vendor-wise PO value",
            "Show billing by project",
        ], question, "related_generic")

    def _mysql_entity_tool_sql(self, question, database, entity_resolution):
        question_lower = question.lower()
        project_entities = [
            entity for entity in entity_resolution.get("entities", [])
            if entity.get("resolved_type") == "project"
            and entity.get("primary_key")
        ]
        if not project_entities:
            return None
        try:
            schema = mysql_adapter.inspect_schema(database)
        except Exception:
            return None
        project_ids = []
        for entity in project_entities:
            value = str(entity.get("primary_key"))
            if re.fullmatch(r"\d+", value) and value not in project_ids:
                project_ids.append(value)
        if not project_ids:
            return None
        id_list = ", ".join(project_ids[:10])

        if "billing" in question_lower and {"client_bill_raising", "project_mst"}.issubset(schema):
            return f"""
SELECT
    p.project_id,
    p.project_name,
    COUNT(c.billing_id) AS bill_count,
    ROUND(SUM(COALESCE(c.gross_bill_value, 0)), 2) AS gross_bill_value,
    ROUND(SUM(COALESCE(c.bill_amount, 0)), 2) AS bill_amount,
    ROUND(SUM(COALESCE(c.amount_to_be_received_this_bill, 0)), 2) AS receivable_amount
FROM project_mst p
LEFT JOIN client_bill_raising c
    ON c.project_id = p.project_id
WHERE p.project_id IN ({id_list})
GROUP BY p.project_id, p.project_name
ORDER BY gross_bill_value DESC
LIMIT {max(1, len(project_ids[:10]))}
""".strip()

        if (
            len(project_ids) == 1
            and {"project_mst"}.issubset(schema)
            and any(word in question_lower for word in ["details", "detail", "about", "value", "client", "dates", "state"])
        ):
            return f"""
SELECT
    p.project_id,
    p.project_name,
    p.project_short_name,
    p.client_name,
    p.project_value,
    p.estimated_value,
    p.project_start_date,
    p.project_end_date,
    p.project_state,
    p.project_location,
    p.status,
    p.project_desc
FROM project_mst p
WHERE p.project_id IN ({id_list})
LIMIT 1
""".strip()
        return None

    def _classify_intent(self, question):
        normalized = " ".join(question.lower().split())
        metadata_patterns = [
            r"\bwhat is this database about\b",
            r"\bdescribe this database\b",
            r"\bwhat tables\b",
            r"\btables (?:exist|are available)\b",
            r"\bbusiness modules\b",
            r"\bwhat data is available\b",
            r"\bexplain this erp\b",
            r"\bdatabase summary\b",
        ]
        business_patterns = [
            r"\binsight",
            r"\bopportunit",
            r"\bperform",
            r"\brevenue",
            r"\bloss\b",
            r"\blost\b",
            r"\brisk\b",
            r"\btrend",
            r"\bcompare\b",
            r"\btop customer",
            r"\btop product",
            r"\bunderutilized",
            r"\bworst\b",
            r"\bbest\b",
        ]
        is_metadata = any(re.search(pattern, normalized) for pattern in metadata_patterns)
        is_business = any(re.search(pattern, normalized) for pattern in business_patterns)
        if is_metadata and is_business:
            return "hybrid"
        if is_metadata:
            return "metadata"
        if is_business:
            return "business_insight"
        return "data_query"

    def _unsupported_loss_answer(self):
        return (
            "Short summary\n"
            "- I could not calculate company profit or loss from the currently selected data.\n\n"
            "Limitations\n"
            "- A true loss calculation needs reliable revenue, cost, expense, refund, or margin fields tied to the same period/entity.\n"
            "- I can only analyze metrics that exist in the connected database.\n\n"
            "Next best step\n"
            "- Ask for a visible proxy such as pending payments, cancelled orders, expenses by department, or revenue by project if those tables are available."
        )

    def _is_loss_question(self, question):
        question_lower = question.lower()
        return any(word in question_lower for word in ["loss", "profit", "margin"])

    def _asks_true_company_loss(self, question):
        question_lower = question.lower()
        return "loss" in question_lower and any(
            phrase in question_lower
            for phrase in ["company", "overall", "total", "how much", "what is"]
        )

    def _has_loss_fields(self, database):
        schema = mysql_adapter.inspect_schema(database)
        revenue_markers = ("revenue", "sales", "income", "billing", "invoice")
        cost_markers = ("cost", "expense", "profit", "loss", "margin")
        has_revenue = False
        has_cost = False
        for info in schema.values():
            for column in info["columns"]:
                name = column["column_name"].lower()
                has_revenue = has_revenue or any(marker in name for marker in revenue_markers)
                has_cost = has_cost or any(marker in name for marker in cost_markers)
                if has_revenue and has_cost:
                    return True
        return False

    def ask_sqlserver(self, question, database_identity, allowed_tables=None):
        token_metrics.start()
        question = self._validate_question(question)
        if self.is_casual_message(question):
            response = self.casual_response(has_database=True)
            token_metrics.log_summary()
            return response

        cache_identity = self._llm_cache_identity()
        cached = response_cache.get_by_identity(
            question,
            database_identity,
            **cache_identity,
        )
        if cached:
            return self._public_response({**cached, "cache_hit": True})

        schema_prompt = sqlserver_adapter.schema_prompt(allowed_tables)
        sql = sqlserver_adapter.revenue_product_sql(question, allowed_tables)
        used_local_sql = bool(sql)
        if not sql:
            sql = llm.generate_sqlserver_sql(question, schema_prompt)
        validation = sqlserver_adapter.validate_sql(sql)

        retries = 0
        while not validation["valid"] and retries < MAX_RETRIES:
            retries += 1
            sql = llm.rewrite_failed_mysql_sql(
                question=question,
                schema_prompt=schema_prompt,
                failed_sql=sql,
                error_message="; ".join(validation["errors"]),
            )
            validation = sqlserver_adapter.validate_sql(sql)

        if not validation["valid"]:
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return {
                "success": False,
                "answer": "I could not turn that into a safe SQL Server query yet.",
                "sql": sql,
                "result": None,
                "error": "; ".join(validation["errors"]),
                "metrics": metrics,
            }

        try:
            dataframe = sqlserver_adapter.execute_read_query(sql)
        except Exception as error:
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return {
                "success": False,
                "answer": "I found a query idea, but it did not run against SQL Server.",
                "sql": sql,
                "result": None,
                "error": str(error),
                "metrics": metrics,
            }

        if used_local_sql:
            answer = self._fallback_answer(question, dataframe)
        elif len(dataframe) == 1 and len(dataframe.columns) == 1:
            column = dataframe.columns[0]
            value = dataframe.iloc[0][column]
            answer = f"{column.replace('_', ' ').title()} is {value}."
        else:
            try:
                answer = llm.generate_answer(question, dataframe)
            except RuntimeError:
                answer = self._fallback_answer(question, dataframe)

        result = self._dataframe_payload(dataframe)
        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()
        response = {
            "success": True,
            "answer": answer,
            "sql": sql,
            "result": result,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
        }
        response_cache.set_by_identity(
            question,
            database_identity,
            response,
            **cache_identity,
            ttl_seconds=LIVE_DATABASE_CACHE_TTL_SECONDS,
        )
        return response

    def ask_mysql(self, question, database, database_identity):
        token_metrics.start()
        question = self._validate_question(question)
        question = self._canonicalize_mysql_question(question)
        if self.is_casual_message(question):
            response = self.casual_response(has_database=True)
            token_metrics.log_summary()
            return response

        llm_identity = self._llm_cache_identity()
        semantic_plan = semantic_parser.parse(question).to_dict()
        try:
            catalog = mysql_adapter.business_catalog(database)
        except Exception:
            catalog = {}
        semantic_binding = erp_semantic_binder.bind(
            question=question,
            semantic_plan=semantic_plan,
            catalog=catalog,
        ).to_dict()
        classification = question_classifier.classify(question)
        intent = classification["intent"]
        if intent == "missing_kpi":
            intent = "business_insight"
        analysis = (
            {
                "candidates": [],
                "metrics": [],
                "dimensions": [],
            }
            if semantic_binding.get("authoritative")
            else mysql_adapter.analyze_question(question, database)
        )
        entity_resolution = mysql_adapter.resolve_verified_entities(
            question,
            database,
            semantic_plan=semantic_plan,
            semantic_binding=semantic_binding,
            expected_entity_types=self._expected_entity_types_from_question(question),
        )
        semantic_binding = erp_semantic_binder.bind(
            question=question,
            semantic_plan=semantic_plan,
            catalog=catalog,
            entity_resolution=entity_resolution,
        ).to_dict()
        semantic_binding = self._prefer_project_master_details(
            question,
            semantic_binding,
            entity_resolution,
        )
        ordered_clarification = ordered_clarification_planner.plan(
            question=question,
            semantic_plan=semantic_plan,
            entity_resolution=entity_resolution,
            semantic_binding=semantic_binding,
        )
        if ordered_clarification.required:
            return self._mysql_ordered_clarification_response(
                question=question,
                intent=intent,
                plan=ordered_clarification,
            )
        initial_resolution = {
            "semantic_plan": semantic_plan,
            "semantic_binding": {
                "version": semantic_binding.get("version"),
                "catalog_version": semantic_binding.get("catalog_version"),
                "bound_metrics": semantic_binding.get("bound_metrics"),
                "bound_dimensions": semantic_binding.get("bound_dimensions"),
                "approved_relationships": semantic_binding.get("approved_relationships"),
                "unresolved_concepts": semantic_binding.get("unresolved_concepts"),
            },
            "classification": classification,
            "candidate_entities": [
                candidate["entity"] for candidate in analysis.get("candidates", [])[:5]
            ],
            "resolved_entities": [
                {
                    "entity": match["entity"],
                    "entity_type": match["entity_type"],
                    "table": match["table"],
                    "column": match["column"],
                    "primary_key": match.get("primary_key"),
                    "confidence": match["confidence"],
                }
                for match in entity_resolution.get("matches", [])[:5]
            ],
            "metrics": [
                item.get("metric") for item in analysis.get("metrics", [])[:8]
            ],
            "dimensions": [
                item.get("dimension") for item in analysis.get("dimensions", [])[:8]
            ],
        }
        answer_cache_identity = self._mysql_cache_identity(
            question,
            database,
            database_identity,
            resolution=initial_resolution,
        )
        cached = response_cache.get_by_identity(
            question,
            database_identity,
            **llm_identity,
            cache_identity=answer_cache_identity,
            max_age_seconds=LIVE_DATABASE_CACHE_TTL_SECONDS,
        )
        if cached:
            return {**cached, "cache_hit": True}

        verified_binding_can_proceed = (
            semantic_binding.get("authoritative")
            and not semantic_binding.get("clarification_required")
        )
        if (
            entity_resolution.get("clarification_required")
            and not verified_binding_can_proceed
        ):
            options = entity_resolution.get("matches", [])[:8]
            clarification_limitation = (
                "A required entity filter could not be matched to a stored database value."
                if entity_resolution.get("unresolved")
                else "Multiple entity matches require user clarification."
            )
            lines = [
                "I need one clarification before I query this.",
                "",
            ]
            if entity_resolution.get("unresolved"):
                lines.append("I could not find:")
                for item in entity_resolution["unresolved"][:5]:
                    lines.append(f"- {item['mention']}")
                lines.append("")
            if entity_resolution.get("ambiguities"):
                lines.append("I found multiple possible meanings:")
                for ambiguity in entity_resolution["ambiguities"][:4]:
                    lines.append(f"- {ambiguity['mention']}:")
                    for index, match in enumerate(ambiguity["options"][:4], start=1):
                        lines.append(
                            f"  {index}. {match['entity_type'].title()} {match['value']} "
                            f"from {match['table']}.{match['column']}"
                        )
                lines.append("")
            if entity_resolution.get("relationship_gaps"):
                lines.append("I found the entities, but could not verify a safe relationship between:")
                for gap in entity_resolution["relationship_gaps"][:4]:
                    lines.append(f"- {gap['from_entity']} and {gap['to_entity']}")
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return self._public_response({
                "success": True,
                "answer": "\n".join(lines),
                "sql": None,
                "result": None,
                "error": None,
                "metrics": metrics,
                "cache_hit": False,
                "intent": intent,
                "entity_resolution": entity_resolution,
                "confidence": self._confidence_response(
                    intent=intent,
                    question=question,
                    selected_tables=[match["table"] for match in options],
                    validation_ok=True,
                    execution_ok=True,
                    assumptions=1,
                    limitations=[clarification_limitation],
                ),
            })

        if intent == "metadata":
            summary = mysql_adapter.database_summary(database)
            try:
                answer = llm.generate_metadata_answer(
                    question,
                    mysql_adapter.metadata_prompt(database, question),
                )
            except RuntimeError:
                modules = summary["detected_modules"][:10]
                important_tables = summary["important_tables"][:8]
                largest_tables = [
                    f"{item['table']} ({item['approx_rows']:,} approx rows)"
                    for item in summary["largest_tables"][:5]
                ]
                module_text = ", ".join(modules) if modules else "No clear modules detected"
                answer = (
                    "Short summary\n"
                    f"- This is a {summary['database_type']} database with {summary['table_count']} tables. "
                    f"It appears to cover {module_text}.\n\n"
                    "Key findings\n"
                    f"- Detected modules: {module_text}.\n"
                    f"- Important tables: {', '.join(important_tables) or 'None detected'}.\n"
                    f"- Largest active areas: {', '.join(largest_tables) or 'row-count metadata unavailable'}.\n"
                    "- This overview is generated from table names, columns, row counts, and relationship hints.\n\n"
                    "Limitations\n"
                    "- This is a structural overview, not a business-rule audit.\n"
                    "- Complex KPIs still need validated joins and source columns before calculation."
                )
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            response = {
                "success": True,
                "answer": answer,
                "sql": None,
                "result": None,
                "error": None,
                "metrics": metrics,
                "cache_hit": False,
                "intent": intent,
                "confidence": self._confidence_response(
                    intent=intent,
                    question=question,
                    selected_tables=mysql_adapter.database_summary(database)["important_tables"][:8],
                    validation_ok=True,
                    execution_ok=True,
                    row_count=None,
                    metadata_only=True,
                ),
            }
            response = self._public_response(response)
            response_cache.set_by_identity(
                question,
                database_identity,
                response,
                **llm_identity,
                ttl_seconds=LIVE_DATABASE_CACHE_TTL_SECONDS,
                cache_identity=answer_cache_identity,
                selected_tables=mysql_adapter.database_summary(database)["important_tables"][:8],
            )
            return response

        if self._asks_true_company_loss(question) or (
            self._is_loss_question(question) and not self._has_loss_fields(database)
        ):
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            response = {
                "success": True,
                "answer": self._unsupported_loss_answer(),
                "sql": None,
                "result": None,
                "error": None,
                "metrics": metrics,
                "cache_hit": False,
                "intent": intent,
                "confidence": self._confidence_response(
                    intent=intent,
                    question=question,
                    validation_ok=True,
                    execution_ok=True,
                    missing_kpi=True,
                    assumptions=1,
                    metadata_only=False,
                    limitations=[
                        "True profit/loss requires cost, expense, refund, or margin data tied to revenue."
                    ],
                ),
            }
            response = self._public_response(response)
            response_cache.set_by_identity(
                question,
                database_identity,
                response,
                **llm_identity,
                ttl_seconds=LIVE_DATABASE_CACHE_TTL_SECONDS,
                cache_identity=answer_cache_identity,
            )
            return response

        if semantic_binding.get("clarification_required"):
            return self._mysql_binding_clarification_response(
                question=question,
                intent=intent,
                semantic_binding=semantic_binding,
            )

        entity_resolver_prompt = entity_resolver.format_multi_prompt(entity_resolution)
        resolved_tables = [
            match["table"]
            for match in entity_resolution.get("matches", [])
            if match.get("confidence", 0) >= 70
        ]
        if semantic_binding.get("authoritative"):
            relevant_tables = list(dict.fromkeys([
                *(semantic_binding.get("required_tables") or []),
                *resolved_tables,
            ]))
            question_memory_prompt = ""
            schema_context = {
                "prompt": None,
                "tables": relevant_tables,
                "columns": semantic_binding.get("required_columns") or {},
                "result": {
                    "selected_tables": relevant_tables,
                    "relevant_columns": (
                        semantic_binding.get("required_columns") or {}
                    ),
                    "relationship_paths": (
                        semantic_binding.get("approved_relationships") or []
                    ),
                    "retrieval_confidence": (
                        semantic_binding.get("confidence_score", 0) / 100
                    ),
                    "reasons": semantic_binding.get("reasons") or [],
                    "warnings": semantic_binding.get("warnings") or [],
                    "unresolved_concepts": [
                        item.get("concept")
                        for item in (
                            semantic_binding.get("unresolved_concepts") or []
                        )
                        if item.get("concept")
                    ],
                },
            }
        else:
            relevant_tables = [
                *(semantic_binding.get("required_tables") or []),
                *resolved_tables,
                *mysql_adapter.relevant_tables(
                    question,
                    database,
                    limit=10,
                ),
            ]
            relevant_tables = list(dict.fromkeys(relevant_tables))[:12]
            if not relevant_tables:
                relevant_tables = mysql_adapter.database_summary(
                    database
                )["important_tables"][:8]
            memory = mysql_adapter.database_memory(database)
            question_memory_prompt = database_memory.prompt_for_question(
                question,
                memory,
                limit=10,
            )
            schema_context = mysql_adapter.retrieve_schema_context(
                question,
                database,
                semantic_plan=semantic_plan,
                entity_resolution=entity_resolution,
                candidate_limit=12,
            )
        query_plan = query_plan_guard.build(
            question=question,
            semantic_plan=semantic_plan,
            entity_resolution=entity_resolution,
            retrieval=schema_context.get("result") or {},
            semantic_binding=semantic_binding,
        )
        plan_validation = query_plan_guard.validate_plan(query_plan)
        if not plan_validation["valid"]:
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return self._public_response({
                "success": False,
                "answer": "I could not build a safe query plan for that question yet.",
                "sql": None,
                "result": None,
                "error": "; ".join(plan_validation["errors"]),
                "metrics": metrics,
                "confidence": self._confidence_response(
                    intent=intent,
                    question=question,
                    validation_ok=False,
                    execution_ok=False,
                    limitations=plan_validation["errors"],
                ),
            })
        relevant_tables = list(dict.fromkeys([
            *schema_context.get("tables", []),
            *relevant_tables,
        ]))[:16]
        if (
            not semantic_binding.get("authoritative")
            and self._mysql_should_clarify_before_sql(
                question,
                semantic_plan,
                entity_resolution,
            )
        ):
            return self._mysql_clarification_response(
                question=question,
                database=database,
                intent=intent,
                semantic_plan=semantic_plan,
                query_plan=query_plan,
                schema_context=schema_context,
                errors=[
                    "The question combines material purchase cost, date scope, and project/site scope without a confirmed cost definition."
                ],
            )
        erp_sql_context = None
        if semantic_binding.get("authoritative"):
            try:
                erp_sql_context = erp_sql_context_builder.build(
                    schema=mysql_adapter.inspect_schema(database),
                    query_plan=query_plan,
                    semantic_binding=semantic_binding,
                    catalog=catalog,
                    schema_fingerprint=mysql_adapter.schema_fingerprint(database),
                )
            except Exception as error:
                logger.warning(
                    "Verified ERP SQL context could not be built; using fallback MySQL path: %s",
                    error,
                )
        if erp_sql_context:
            existing_generation_columns = dict(query_plan.generation_columns)
            query_plan.generation_tables = list(
                erp_sql_context.selected_tables
            )
            query_plan.generation_columns = query_plan_guard._merge_columns(
                existing_generation_columns,
                dict(erp_sql_context.selected_columns),
            )
        authoritative_runtime = bool(erp_sql_context)
        if not authoritative_runtime and semantic_binding.get("authoritative"):
            query_plan.catalog_authoritative = False
        schema_prompt = self._mysql_schema_prompt(
            database=database,
            relevant_tables=relevant_tables,
            entity_resolver_prompt=entity_resolver_prompt,
            question_memory_prompt=question_memory_prompt,
            semantic_plan=semantic_plan,
            query_plan_prompt=query_plan_guard.prompt(query_plan),
            schema_retrieval_prompt=schema_context.get("prompt"),
            schema_retrieval_result=schema_context.get("result"),
            erp_sql_context=(
                erp_sql_context.to_dict()
                if erp_sql_context
                else None
            ),
        )
        entity_scoped_question = bool(
            entity_resolution.get("entities")
            and re.search(r"\b(?:for|on|at|from|by|about|details?)\b", question.lower())
        )
        relationship_style_question = bool(
            entity_scoped_question
            and re.search(r"\b(?:vendors?|customers?|employees?|materials?|invoices?|purchase orders?|billing)\b", question.lower())
        )
        if authoritative_runtime:
            tool_sql = verified_sql_compiler.compile(query_plan)
        else:
            entity_tool_sql = self._mysql_entity_tool_sql(
                question,
                database,
                entity_resolution,
            )
            tool_sql = (
                mysql_adapter.count_table_sql(question, database)
                or entity_tool_sql
                or (
                    None
                    if relationship_style_question
                    else mysql_adapter.catalog_sql(question, database)
                )
                or mysql_adapter.business_overview_sql(question, database)
            )
        sql = None
        validation = {"valid": False, "errors": ["No SQL query was generated."], "warnings": []}
        if tool_sql:
            tool_validation = self._mysql_validate_with_plan(tool_sql, database, query_plan)
            if tool_validation["valid"]:
                sql = tool_sql
                validation = tool_validation

        if not sql and not authoritative_runtime:
            try:
                sql = llm.generate_mysql_sql(question, schema_prompt)
            except RuntimeError as error:
                logger.warning("LLM SQL generation failed, using backend tool fallback: %s", error)
                sql = tool_sql

            validation = self._mysql_validate_with_plan(sql, database, query_plan)
        elif not sql:
            validation = {
                "valid": False,
                "errors": ["No verified ERP SQL could be generated."],
                "warnings": [],
            }

        retries = 0
        while sql and sql != tool_sql and not validation["valid"] and retries < min(MAX_RETRIES, 1):
            retries += 1
            sql = llm.rewrite_failed_mysql_sql(
                question=question,
                schema_prompt=schema_prompt,
                failed_sql=sql,
                error_message="; ".join(validation["errors"]),
            )
            validation = self._mysql_validate_with_plan(sql, database, query_plan)

        if not validation["valid"] and tool_sql and tool_sql != sql:
            sql = tool_sql
            validation = self._mysql_validate_with_plan(sql, database, query_plan)

        if not validation["valid"]:
            if self._mysql_should_clarify_after_failure(question, semantic_plan, validation["errors"]):
                return self._mysql_clarification_response(
                    question=question,
                    database=database,
                    intent=intent,
                    semantic_plan=semantic_plan,
                    query_plan=query_plan,
                    schema_context=schema_context,
                    errors=validation["errors"],
                )
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return self._public_response({
                "success": False,
                "answer": "I could not turn that into a safe MySQL query yet.",
                "sql": sql,
                "result": None,
                "error": "; ".join(validation["errors"]),
                "metrics": metrics,
                "confidence": self._confidence_response(
                    intent=intent,
                    question=question,
                    sql=sql,
                    selected_tables=relevant_tables,
                    selected_columns=self._referenced_columns(sql, database),
                    validation_ok=False,
                    execution_ok=False,
                    assumptions=1,
                    limitations=validation["errors"],
                ),
            })

        dataframe = None
        execution_error = None
        try:
            dataframe = mysql_adapter.execute_read_query(sql, database)
        except Exception as error:
            execution_error = error
            if not authoritative_runtime:
                repaired_by_alias = sql
                alias_error = error
                for _ in range(4):
                    candidate = self._repair_mysql_unknown_alias_column(
                        repaired_by_alias,
                        database,
                        str(alias_error),
                    )
                    if not candidate or candidate == repaired_by_alias:
                        break
                    candidate_validation = mysql_adapter.validate_sql(
                        candidate,
                        database,
                    )
                    if candidate_validation.get("valid"):
                        candidate_validation = self._mysql_validate_with_plan(
                            candidate,
                            database,
                            query_plan,
                        )
                    if not candidate_validation["valid"]:
                        break
                    try:
                        dataframe = mysql_adapter.execute_read_query(
                            candidate,
                            database,
                        )
                    except Exception as next_error:
                        repaired_by_alias = candidate
                        alias_error = next_error
                        continue
                    else:
                        logger.info(
                            "Repaired MySQL SQL after alias/column execution failure"
                        )
                        sql = candidate
                        execution_error = None
                        break
                if dataframe is None:
                    execution_error = alias_error
                    sql = repaired_by_alias
        if dataframe is None:
            repaired = None
            try:
                if authoritative_runtime and retries >= 1:
                    raise RuntimeError(
                        str(
                            execution_error
                            or "Verified SQL did not return a usable result."
                        )
                    )
                repaired = llm.rewrite_failed_mysql_sql(
                    question=question,
                    schema_prompt=schema_prompt,
                    failed_sql=sql,
                    error_message=str(execution_error or "Query did not return a usable result."),
                )
                repaired_validation = mysql_adapter.validate_sql(repaired, database)
                if repaired_validation.get("valid"):
                    repaired_validation = self._mysql_validate_with_plan(repaired, database, query_plan)
                if repaired_validation["valid"]:
                    dataframe = mysql_adapter.execute_read_query(repaired, database)
                    logger.info("Repaired MySQL SQL after execution failure")
                    sql = repaired
                    validation = repaired_validation
                else:
                    raise RuntimeError("; ".join(repaired_validation["errors"]))
            except Exception as repair_error:
                repair_errors = [str(repair_error)]
                if self._mysql_should_clarify_after_failure(question, semantic_plan, repair_errors):
                    return self._mysql_clarification_response(
                        question=question,
                        database=database,
                        intent=intent,
                        semantic_plan=semantic_plan,
                        query_plan=query_plan,
                        schema_context=schema_context,
                        errors=repair_errors,
                    )
                metrics = token_metrics.get_metrics_summary()
                token_metrics.log_summary()
                return self._public_response({
                    "success": False,
                    "answer": "I found a query idea, but it did not run against MySQL.",
                    "sql": repaired or sql,
                    "result": None,
                    "error": str(repair_error),
                    "metrics": metrics,
                    "confidence": self._confidence_response(
                        intent=intent,
                        question=question,
                        sql=repaired or sql,
                        selected_tables=relevant_tables,
                        selected_columns=self._referenced_columns(repaired or sql, database),
                        validation_ok=True,
                        execution_ok=False,
                        inferred_join=self._has_inferred_join(repaired or sql),
                        assumptions=1,
                        limitations=[str(repair_error)],
                    ),
                })

        if (
            not authoritative_runtime
            and
            tool_sql
            and tool_sql != sql
            and self._looks_like_empty_mysql_answer(question, dataframe)
        ):
            tool_validation = mysql_adapter.validate_sql(tool_sql, database)
            if tool_validation.get("valid"):
                tool_validation = self._mysql_validate_with_plan(tool_sql, database, query_plan)
            if tool_validation["valid"]:
                try:
                    tool_dataframe = mysql_adapter.execute_read_query(tool_sql, database)
                except Exception as error:
                    logger.warning("Backend MySQL fallback failed after empty result: %s", error)
                else:
                    if not self._looks_like_empty_mysql_answer(question, tool_dataframe):
                        logger.info("Replaced empty MySQL answer with backend resolver SQL")
                        sql = tool_sql
                        dataframe = tool_dataframe

        if (
            not authoritative_runtime
            and
            self._looks_like_empty_mysql_answer(question, dataframe)
            and self._is_date_window_question(question)
        ):
            try:
                retry_sql = llm.rewrite_failed_mysql_sql(
                    question=question,
                    schema_prompt=schema_prompt,
                    failed_sql=sql,
                    error_message=(
                        "The query executed but returned zero rows. Re-check table choice, "
                        "join aliases, and date handling. If date columns are varchar/text "
                        "with DD-MM-YYYY values, use STR_TO_DATE(NULLIF(column, ''), '%%d-%%m-%%Y') "
                        "before comparing to CURDATE() or DATE_SUB(). Prefer populated master/entity tables."
                    ),
                )
                retry_validation = mysql_adapter.validate_sql(retry_sql, database)
                if retry_validation.get("valid"):
                    retry_validation = self._mysql_validate_with_plan(retry_sql, database, query_plan)
                if retry_validation["valid"]:
                    retry_dataframe = mysql_adapter.execute_read_query(retry_sql, database)
                    if not self._looks_like_empty_mysql_answer(question, retry_dataframe):
                        logger.info("Replaced empty MySQL date-window answer with rewritten SQL")
                        sql = retry_sql
                        dataframe = retry_dataframe
            except Exception as error:
                logger.warning("Empty MySQL date-window retry failed: %s", error)

        if (
            not authoritative_runtime
            and
            self._looks_like_empty_mysql_answer(question, dataframe)
            and entity_scoped_question
        ):
            try:
                retry_sql = llm.rewrite_failed_mysql_sql(
                    question=question,
                    schema_prompt=schema_prompt,
                    failed_sql=sql,
                    error_message=(
                        "The query executed but returned zero rows for an entity-specific question. "
                        "Keep the resolved entity anchor from the entity resolution plan, then try a different "
                        "schema-supported relationship path. Prefer populated transaction tables over empty "
                        "master/link tables, keep the exact requested entity filter, and do not return a generic list. "
                        f"Resolved anchors: {self._mysql_entity_anchor_text(entity_resolution)}"
                    ),
                )
                retry_validation = mysql_adapter.validate_sql(retry_sql, database)
                if retry_validation.get("valid"):
                    retry_validation = self._mysql_validate_with_plan(retry_sql, database, query_plan)
                if retry_validation["valid"]:
                    retry_dataframe = mysql_adapter.execute_read_query(retry_sql, database)
                    if not self._looks_like_empty_mysql_answer(question, retry_dataframe):
                        logger.info("Replaced empty MySQL relationship answer with rewritten SQL")
                        sql = retry_sql
                        dataframe = retry_dataframe
            except Exception as error:
                logger.warning("Empty MySQL relationship retry failed: %s", error)

        if (
            not authoritative_runtime
            and self._mysql_should_clarify_empty_result(
                question,
                semantic_plan,
                dataframe,
            )
        ):
            return self._mysql_clarification_response(
                question=question,
                database=database,
                intent=intent,
                semantic_plan=semantic_plan,
                query_plan=query_plan,
                schema_context=schema_context,
                errors=[
                    "The query ran but returned no rows. For this type of ERP question, that may mean the date source, project/site scope, or cost definition needs confirmation."
                ],
            )

        result = self._dataframe_payload(dataframe)
        execution_receipt = None
        if authoritative_runtime:
            execution_receipt = build_verified_execution_receipt(
                sql=sql,
                result=result,
                query_plan=query_plan,
                validation=validation,
                database_fingerprint=mysql_adapter.database_fingerprint(
                    database,
                    database_identity,
                ),
                schema_fingerprint=mysql_adapter.schema_fingerprint(database),
            )
            answer = self._verified_cross_module_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_billing_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_vendor_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_po_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_project_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_inventory_answer(question, dataframe, query_plan)
            if not answer:
                answer = self._verified_store_answer(question, dataframe, query_plan)
            if not answer:
                try:
                    answer = llm.generate_verified_answer(
                        question,
                        verified_fact_payload(result),
                        execution_receipt.to_dict(),
                    )
                except RuntimeError:
                    answer = self._fallback_answer(question, dataframe)
            elif LLM_POLISH_VERIFIED_ANSWERS:
                deterministic_answer = answer
                try:
                    answer = llm.generate_verified_answer(
                        question,
                        verified_fact_payload(result),
                        execution_receipt.to_dict(),
                    )
                except RuntimeError:
                    answer = deterministic_answer
        else:
            local_answer = (
                self._fallback_answer(question, dataframe)
                if sql == tool_sql
                else None
            )
            if local_answer and not local_answer.startswith("The top result is "):
                answer = local_answer
            else:
                try:
                    answer = llm.generate_answer(question, dataframe)
                except RuntimeError:
                    answer = self._fallback_answer(question, dataframe)

        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()
        response = {
            "success": True,
            "answer": answer,
            "sql": sql,
            "result": result,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
            "intent": intent,
            "verification": (
                execution_receipt.to_dict()
                if execution_receipt
                else None
            ),
            "confidence": self._confidence_response(
                intent=intent,
                question=question,
                sql=sql,
                selected_tables=relevant_tables,
                selected_columns=list(dataframe.columns),
                validation_ok=True,
                execution_ok=True,
                row_count=len(dataframe),
                verified_join=bool(
                    authoritative_runtime
                    and query_plan.approved_relationships
                ),
                inferred_join=bool(
                    not authoritative_runtime
                    and self._has_inferred_join(sql)
                ),
                assumptions=(
                    1
                    if (
                        not authoritative_runtime
                        and self._has_inferred_join(sql)
                    )
                    else 0
                ),
            ),
        }
        response = self._public_response(response)
        schema_profile_history.record_success(
            mysql_adapter.schema_fingerprint(database),
            sql,
            mysql_adapter.referenced_tables(sql),
        )
        response_cache.set_by_identity(
            question,
            database_identity,
            response,
            **llm_identity,
            ttl_seconds=LIVE_DATABASE_CACHE_TTL_SECONDS,
            cache_identity=answer_cache_identity,
            selected_tables=relevant_tables,
            selected_columns=list(dataframe.columns),
            result_signature=self._result_signature(result),
        )
        return response

    def _mysql_schema_prompt(
        self,
        *,
        database,
        relevant_tables,
        entity_resolver_prompt,
        question_memory_prompt,
        semantic_plan=None,
        query_plan_prompt=None,
        schema_retrieval_prompt=None,
        schema_retrieval_result=None,
        erp_sql_context=None,
    ):
        if erp_sql_context:
            return erp_sql_context["prompt"]
        semantic_text = stable_json(semantic_plan or {})
        semantic_block = f"Semantic query plan:\n{semantic_text}\n\n" if semantic_plan else ""
        query_plan_block = f"{query_plan_prompt}\n" if query_plan_prompt else ""
        if MYSQL_LLM_SCHEMA_MODE == "full":
            return (
                f"{semantic_block}"
                f"{query_plan_block}"
                f"{entity_resolver_prompt}\n\n"
                f"{question_memory_prompt}\n\n"
                "Full database-wide schema map for every table. Use this to compare all possible matching tables before choosing SQL:\n"
                f"{mysql_adapter.schema_prompt(database, max_columns=MYSQL_LLM_FULL_SCHEMA_MAX_COLUMNS, include_row_counts=True)}\n\n"
                "Most relevant table candidates, repeated for attention:\n"
                f"{mysql_adapter.schema_prompt(database, relevant_tables or None, max_columns=24, include_row_counts=True)}"
            )

        if MYSQL_LLM_SCHEMA_MODE in {"selected", "narrow"}:
            return (
                f"{semantic_block}"
                f"{query_plan_block}"
                f"{entity_resolver_prompt}\n\n"
                f"{question_memory_prompt}\n\n"
                "Exact schema for selected tables:\n"
                f"{mysql_adapter.schema_prompt(database, relevant_tables or None, max_columns=24, include_row_counts=True)}"
            )

        if schema_retrieval_prompt:
            return (
                f"{semantic_block}"
                f"{query_plan_block}"
                f"{entity_resolver_prompt}\n\n"
                f"{question_memory_prompt}\n\n"
                "Use only the compact retrieved schema unless SQL validation fails and a retry is needed. "
                "Do not replace unresolved concepts with related fields:\n"
                f"{schema_retrieval_prompt}"
            )

        return (
            f"{semantic_block}"
            f"{query_plan_block}"
            f"{entity_resolver_prompt}\n\n"
            f"{question_memory_prompt}\n\n"
            "Compact database-wide table index. Use this to find alternative matching tables without reading every column:\n"
            f"{mysql_adapter.schema_prompt(database, max_columns=6, include_row_counts=True)}\n\n"
            "Detailed schema for the strongest candidate and resolved-entity tables:\n"
            f"{mysql_adapter.schema_prompt(database, relevant_tables or None, max_columns=24, include_row_counts=True)}"
        )

    def _mysql_entity_anchor_text(self, entity_resolution):
        anchors = []
        for entity in entity_resolution.get("entities", [])[:6]:
            table = entity.get("table")
            column = entity.get("column")
            value = entity.get("value")
            primary_key = entity.get("primary_key")
            if not table or not column:
                continue
            key_text = f", primary_key={primary_key}" if primary_key is not None else ""
            anchors.append(f"{entity.get('resolved_type') or entity.get('entity_type')}: {table}.{column}='{value}'{key_text}")
        return "; ".join(anchors) or "none"

    def _fallback_answer(self, question, dataframe):
        if dataframe.empty:
            return (
                "Short summary\n"
                "- I did not find matching records for that question.\n\n"
                "Limitations\n"
                "- This means the selected tables/columns did not contain enough matching data for that metric.\n\n"
                "Next step\n"
                "- Try asking with a specific table, module, date range, or business metric."
            )

        columns = set(dataframe.columns)
        question_lower = question.lower()

        if {"product_category", "units_sold", "revenue"}.issubset(columns):
            top = dataframe.iloc[0]
            category = top["product_category"] or "uncategorized products"
            units = int(top["units_sold"])
            revenue = float(top["revenue"])

            if "focus" in question_lower or "should" in question_lower:
                return (
                    f"I would focus first on {category}. It leads this view with "
                    f"{units:,} units sold and about {revenue:,.2f} in revenue, "
                    "so it has both demand and business impact. Compare it with the next few categories before making a final decision, especially if margin data is available."
                )

            return (
                f"The most sold product category is {category}, with {units:,} units sold. "
                f"It generated about {revenue:,.2f} in revenue, so it is also commercially important, not just high-volume."
            )

        if {"product", "revenue"}.issubset(columns):
            top = dataframe.iloc[0]
            product = top["product"] or "unknown product"
            revenue = float(top["revenue"] or 0)
            return (
                f"{product} has the highest revenue at {revenue:,.2f}. "
                "This is based only on the rows returned by the query."
            )

        if len(dataframe) == 1:
            count_columns = [column for column in columns if column.endswith("_count")]
            if count_columns:
                count_column = sorted(count_columns)[0]
                count_value = int(dataframe.iloc[0][count_column] or 0)
                entity = (
                    dataframe.iloc[0].get("counted_entity")
                    if "counted_entity" in dataframe.columns
                    else count_column.removesuffix("_count")
                )
                return f"There are {count_value:,} {entity}s in the connected database."
            if "project_count" in columns:
                project_count = int(dataframe.iloc[0]["project_count"] or 0)
                return f"There are {project_count:,} projects in the connected database."
            if "row_count" in columns:
                row_count = int(dataframe.iloc[0]["row_count"] or 0)
                return f"There are {row_count:,} matching rows."

        if {"table_name", "row_count"}.issubset(columns):
            rows = dataframe.head(6).to_dict(orient="records")
            bullets = "\n".join(
                f"- {row['table_name']}: {int(row['row_count']):,} rows"
                for row in rows
            )
            top = rows[0] if rows else None
            summary = (
                f"- The busiest visible area is {top['table_name']} with "
                f"{int(top['row_count']):,} rows."
                if top
                else "- No table activity was found."
            )
            return (
                "Short summary\n"
                f"{summary}\n\n"
                "Key findings\n"
                f"{bullets}\n\n"
                "Limitations\n"
                "- This is a volume-based overview, not a revenue or profit analysis."
            )

        if {"project_id", "project_name"}.issubset(columns) and "client_name" in columns and "metric_value" not in columns:
            total = len(dataframe)
            names = [
                str(row["project_name"])
                for row in dataframe.head(5).to_dict(orient="records")
                if row.get("project_name")
            ]
            bullets = "\n".join(f"- {name}" for name in names)
            return (
                "Short summary\n"
                f"- Here are {total} projects from the connected database.\n\n"
                "Key findings\n"
                f"{bullets}\n"
                "- The table also includes client, project value, date, state, and status fields when available.\n\n"
                "Limitations\n"
                "- This is a project list only; it does not measure billing, progress, or profitability."
            )

        if {"project_name", "metric_value", "metric_column"}.issubset(columns):
            rows = dataframe.head(10).to_dict(orient="records")
            metric_column = str(rows[0].get("metric_column") or "metric_value") if rows else "metric_value"
            table_lines = ["Rank\tProject Name\tClient\tValue"]
            for index, row in enumerate(rows, start=1):
                table_lines.append(
                    f"{index}\t{row.get('project_name')}\t{row.get('client_name', '')}\t{float(row.get('metric_value') or 0):,.2f}"
                )
            return (
                "Short summary\n"
                f"- Here are the top {len(rows)} projects ranked by {metric_column.replace('_', ' ')}.\n\n"
                "Key findings\n"
                + "\n".join(table_lines)
                + "\n\nLimitations\n"
                "- This is based on the selected project value column, not profit or cash collection."
            )

        if {"vendor_name", "total_purchase_value"}.issubset(columns):
            rows = dataframe.head(10).to_dict(orient="records")
            top = rows[0] if rows else {}
            table_lines = ["Rank\tVendor\tPurchase Rows\tTotal Purchase Value"]
            for index, row in enumerate(rows, start=1):
                table_lines.append(
                    f"{index}\t{row.get('vendor_name')}\t{int(row.get('purchase_rows') or 0)}\t{float(row.get('total_purchase_value') or 0):,.2f}"
                )
            return (
                "Short summary\n"
                f"- {top.get('vendor_name', 'The top vendor')} has the highest purchase value found.\n\n"
                "Key findings\n"
                + "\n".join(table_lines)
                + "\n\nLimitations\n"
                "- This uses purchase-order value only; it does not include all possible expense categories."
            )

        if {"client_name", "gross_bill_value"}.issubset(columns):
            top = dataframe.iloc[0]
            client = top["client_name"] or "Unknown client"
            gross = float(top["gross_bill_value"] or 0)
            return (
                "Short summary\n"
                f"- {client} has the highest visible client billing, with {gross:,.2f} in gross bill value.\n\n"
                "Key findings\n"
                "- The table compares clients by billing value so managers can see the largest revenue contributors.\n"
                "- Bill count shows whether the value is coming from many bills or a smaller number of high-value bills.\n"
                "- Receivable amount should be reviewed separately because billing value is not the same as cash collected.\n\n"
                "Limitations\n"
                "- This uses project client names because the client master table is empty in this dump."
            )

        if {"project_name", "material_name", "issued_quantity", "issued_value"}.issubset(columns):
            top = dataframe.iloc[0]
            project = top["project_name"] or "selected project"
            material = top["material_name"] or "top material"
            value = float(top["issued_value"] or 0)
            from_date = top.get("from_date")
            to_date = top.get("to_date")
            date_text = (
                f" from {from_date} to {to_date}"
                if from_date and to_date
                else " in the latest 30-day window available in the database"
            )
            return (
                "Short summary\n"
                f"- For {project}, {material} had the highest issued material value{date_text}, at {value:,.2f}.\n\n"
                "Key findings\n"
                "- The table shows the top issued materials for one project, ranked by issued value.\n"
                "- Issued value is calculated as issued quantity multiplied by the available base charge on the issue line.\n"
                "- Average, minimum, and maximum base charge help spot pricing variation for the same material.\n"
                "- Use this as a material consumption and pricing view, not a full project cost or profitability report.\n\n"
                "Limitations\n"
                "- The ERP stores issue dates as text, so this uses the latest parsed issue date in the dump as the 30-day reference.\n"
                "- Pricing is based on the issue-line base charge available in the material usage table."
            )

        if {"project_name", "total_purchase_value"}.issubset(columns):
            top = dataframe.iloc[0]
            project = top["project_name"] or f"project {top.get('project_id')}"
            value = float(top["total_purchase_value"] or 0)
            source_table = top.get("source_table") or "the selected purchase table"
            value_source = top.get("value_source") or "available value columns"
            return (
                "Short summary\n"
                f"- {project} has the highest purchase value found, at {value:,.2f}.\n\n"
                "Key findings\n"
                f"- The calculation uses `{source_table}` and the value source `{value_source}`.\n"
                "- The table ranks projects by summed purchase value and includes the number of purchase rows behind each total.\n"
                "- This is a procurement/purchase-value view, not full project cost or profit.\n\n"
                "Limitations\n"
                "- Full project cost may also need labour, subcontractor, overhead, inventory adjustment, and expense tables.\n"
                "- If the database has multiple valid purchase sources, this answer uses the strongest schema match found automatically."
            )

        if {"project_name", "gross_bill_value"}.issubset(columns):
            top = dataframe.iloc[0]
            project = top["project_name"] or f"project {top.get('project_id')}"
            total = float(top["gross_bill_value"] or 0)
            return (
                "Short summary\n"
                f"- {project} is the top project by visible billing in the selected data, with {total:,.2f} in gross bill value.\n\n"
                "Key findings\n"
                "- Use this table to see which projects are contributing the most billing volume.\n"
                "- A project manager can use this to spot high-value projects and projects with weak billing activity.\n"
                "- Compare receivable amount with total billing to understand where collection follow-up may be needed.\n\n"
                "Limitations\n"
                "- This is billing value, not profit or cash collected unless receivable/payment fields confirm it."
            )

        if {"department_name", "employee_count"}.issubset(columns):
            top = dataframe.iloc[0]
            department = top["department_name"] or f"department {top.get('department_id')}"
            count = int(top["employee_count"] or 0)
            return (
                "Short summary\n"
                f"- {department} has the highest staffing concentration with {count:,} employees.\n\n"
                "Key findings\n"
                "- The table ranks departments by headcount, which helps managers understand where people are allocated.\n"
                "- Larger departments may need closer workload, attendance, and cost tracking.\n"
                "- Smaller departments may be support functions or specialized teams, depending on the business context.\n\n"
                "Limitations\n"
                "- This measures staffing size, not true department performance."
            )

        if {"order_status", "affected_orders", "revenue_at_risk"}.issubset(columns):
            top = dataframe.iloc[0]
            status = top["order_status"]
            orders = int(top["affected_orders"])
            revenue_at_risk = float(top["revenue_at_risk"] or 0)

            return (
                "I cannot calculate true profit or loss from this database because it does not include product cost, margin, or refund-cost fields. "
                f"As a practical proxy, the biggest visible risk is {status} orders: {orders:,} orders tied to about {revenue_at_risk:,.2f} in item value. "
                "To manage this, start by reducing cancellations/unavailable orders, checking seller fulfillment issues, and tracking margin or refund data in the next version."
            )

        first_row = dataframe.iloc[0].to_dict()
        readable_values = ", ".join(
            f"{key}: {value}"
            for key, value in first_row.items()
        )

        return f"The top result is {readable_values}."

    def _verified_project_answer(self, question, dataframe, query_plan):
        project_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        project_tables.update(query_plan.generation_tables or [])
        if project_tables & {"store_material_usage_quantity", "store_usage_quantity_master", "sub_material", "client_bill_raising", "po_form", "po_items"}:
            return None
        if "project_mst" not in project_tables:
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching projects were found for the verified filters.\n\n"
                "Limitations\n"
                "- This uses only `project_mst`; transaction activity is not included unless asked separately."
            )
        table = self._markdown_table(rows, columns)
        if len(rows) == 1 and "project_count" in columns:
            return (
                "Short summary\n"
                f"- There are {self._format_number(rows[0].get('project_count'))} projects in the selected data.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- This count is based on one row per project."
            )
        money_columns = [column for column in columns if self._is_money_column(column)]
        if len(rows) == 1 and money_columns and not any(column in columns for column in ("project_name", "client_name", "project_state")):
            column = money_columns[0]
            return (
                "Short summary\n"
                f"- The {column.replace('_', ' ')} is {self._format_inr(rows[0].get(column))}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- This is project master value, not full cost or profit."
            )
        ranking_question = bool(
            re.search(
                r"\b(top|highest|biggest|largest|maximum|max|lowest|smallest|minimum|min|by\s+project\s+value|based\s+on\s+project\s+value)\b",
                str(question or "").lower(),
            )
        )
        if rows and "project_value" in columns and ranking_question:
            label = next(
                (column for column in ("project_name", "client_name", "project_state", "status") if column in columns),
                columns[0],
            )
            top_text = (
                f"- This ranks projects by project master value. Top row is {rows[0].get(label)} with "
                f"{self._format_inr(rows[0].get('project_value'))} project value."
            )
        else:
            scope = self._answer_scope_text(query_plan)
            top_text = f"- This shows project master details {scope}; {len(rows):,} project row(s) are shown."
        return (
            "Short summary\n"
            f"{top_text}\n\n"
            "Result\n"
            f"{table}\n\n"
            "Limitations\n"
            "- This is project master information. Billing, purchases, and material movement can be asked separately."
        )

    def _verified_cross_module_answer(self, question, dataframe, query_plan):
        columns = list(dataframe.columns)
        metric_columns = {
            column
            for column in columns
            if column in {
                "project_value",
                "gross_bill_value",
                "purchase_value",
                "issued_quantity",
                "issued_value",
            }
        }
        if "project_name" not in columns or len(metric_columns) < 2:
            return None
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching cross-module project records were found.\n\n"
                "Limitations\n"
                "- This uses only verified project-level joins and pre-aggregated module totals."
            )
        table = self._markdown_table(rows, columns)
        top = rows[0]
        value_order = [
            "gross_bill_value",
            "purchase_value",
            "issued_value",
            "issued_quantity",
            "project_value",
        ]
        question_lower = str(question or "").lower()
        if "po" in question_lower or "purchase" in question_lower:
            value_order = ["purchase_value", "gross_bill_value", "issued_value", "issued_quantity", "project_value"]
        elif "material" in question_lower or "issued" in question_lower:
            value_order = ["issued_value", "issued_quantity", "purchase_value", "gross_bill_value", "project_value"]
        elif "project value" in question_lower:
            value_order = ["project_value", "gross_bill_value", "purchase_value", "issued_value", "issued_quantity"]
        main_column = next((column for column in value_order if column in metric_columns), sorted(metric_columns)[0])
        formatted = (
            self._format_inr(top.get(main_column))
            if self._is_money_column(main_column)
            else self._format_number(top.get(main_column))
        )
        comparison_lines = self._cross_module_comparison_lines(rows, columns)
        comparison_text = (
            "\n\n" + "\n\n".join(comparison_lines)
            if comparison_lines
            else (
                "\n\n"
                "- Billing, PO value, and material movement are compared separately.\n"
                "- Totals are calculated module-wise first, so detail rows do not inflate the numbers."
            )
        )
        return (
            "Short summary\n"
            f"- This compares verified project-level metrics for the selected projects. "
            f"{top.get('project_name')} has the higher {main_column.replace('_', ' ').title()} in this result.\n\n"
            "Result\n"
            f"{table}"
            f"{comparison_text}"
        )

    def _cross_module_comparison_lines(self, rows, columns):
        lines = []
        if not {"project_name", "gross_bill_value", "purchase_value"} <= set(columns):
            return lines
        for row in rows[:5]:
            project = row.get("project_name") or "This project"
            billing = self._to_float(row.get("gross_bill_value"))
            purchase = self._to_float(row.get("purchase_value"))
            if billing is None or purchase is None:
                continue
            difference = abs(purchase - billing)
            formatted = self._format_inr(difference)
            if purchase > billing:
                lines.append(
                    f"For {project}, the recorded PO value is {formatted} higher than the recorded billing value."
                )
            elif billing > purchase:
                lines.append(
                    f"For {project}, the recorded billing value is {formatted} higher than the recorded PO value."
                )
            else:
                lines.append(
                    f"For {project}, the recorded billing value and PO value are the same."
                )
        return lines

    def _verified_store_answer(self, question, dataframe, query_plan):
        store_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        store_tables.update(query_plan.generation_tables or [])
        if not (store_tables & {"store_material_usage_quantity", "store_usage_quantity_master", "sub_material"}):
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching store/material movement records were found for the verified filters.\n\n"
                "Limitations\n"
                "- This uses verified issue-line data. Return and cancellation meanings are not used unless verified."
            )
        if {"material_count", "sub_material_name"} <= set(columns):
            total = rows[0].get("material_count")
            return (
                "Short summary\n"
                f"- There are {self._format_number(total)} verified material master records.\n"
                f"- Showing the first {len(rows):,} material name(s).\n\n"
                "Result\n"
                f"{self._markdown_table(rows, columns)}\n\n"
                "Limitations\n"
                "- This is the material master list, not current stock or material movement."
            )
        table = self._markdown_table(rows, columns)
        numeric_columns = [
            column for column in columns
            if self._is_quantity_column(column)
            or self._is_money_column(column)
            or str(column).lower() in {"row_count", "issue_count"}
        ]
        if len(rows) == 1 and numeric_columns and not any(column in columns for column in ("project_name", "sub_material_name", "transactiontype", "issue_month")):
            column = numeric_columns[0]
            value = rows[0].get(column)
            formatted = (
                self._format_inr(value)
                if self._is_money_column(column)
                else self._format_number(value)
            )
            return (
                "Short summary\n"
                f"- This shows store/material movement {self._answer_scope_text(query_plan)}. "
                f"{column.replace('_', ' ').title()} is {formatted}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Quantities are reported at material issue line grain."
            )
        label = next(
            (
                column for column in ("project_name", "sub_material_name", "issue_month", "transactiontype")
                if column in columns
            ),
            columns[0],
        )
        value_column = next(
            (
                column for column in ("issued_value", "issued_quantity", "requested_quantity", "average_base_charge", "row_count")
                if column in columns
            ),
            None,
        )
        top_text = f"- Returned {len(dataframe):,} verified store/material row(s)."
        if value_column:
            formatted = (
                self._format_inr(rows[0].get(value_column))
                if self._is_money_column(value_column)
                else self._format_number(rows[0].get(value_column))
            )
            top_text = (
                f"- This shows store/material movement {self._answer_scope_text(query_plan)}. "
                f"Top row is {rows[0].get(label)} with {formatted} {value_column.replace('_', ' ')}."
            )
        return (
            "Short summary\n"
            f"{top_text}\n\n"
            "Result\n"
            f"{table}\n\n"
            "Limitations\n"
            "- Issued value, when shown, is issued quantity multiplied by issue-line base charge.\n"
            "- Material returns and cancelled issue slips require verified transaction/status meanings before calculation."
        )

    def _verified_inventory_answer(self, question, dataframe, query_plan):
        inventory_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        inventory_tables.update(query_plan.generation_tables or [])
        if "inventory_master" not in inventory_tables:
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching inventory balance rows were found for the verified filters.\n\n"
                "Limitations\n"
                "- This uses `inventory_master.available_quantity`; inventory movement history is not included here."
            )
        table = self._markdown_table(rows, columns)
        if len(rows) == 1 and "material_count" in columns:
            return (
                "Short summary\n"
                f"- This shows current inventory material groups across all stores. "
                f"Material count is {self._format_number(rows[0].get('material_count'))}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Count is based on material-level current stock groups from `inventory_master.available_quantity`."
            )
        numeric_columns = [
            column for column in columns
            if str(column).lower() in {"current_stock", "material_count"}
            or self._is_quantity_column(column)
        ]
        if len(rows) == 1 and numeric_columns and not any(column in columns for column in ("sub_material_name", "sub_store_id")):
            column = numeric_columns[0]
            return (
                "Short summary\n"
                f"- This shows current inventory {self._answer_scope_text(query_plan)}. "
                f"{column.replace('_', ' ').title()} is {self._format_number(rows[0].get(column))}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Quantities are not unit-converted; use the UOM column when material-level rows are shown."
            )
        label = next(
            (
                column for column in ("sub_material_name", "sub_store_id")
                if column in columns
            ),
            columns[0],
        )
        value_column = next(
            (
                column for column in ("current_stock", "material_count")
                if column in columns
            ),
            None,
        )
        top_text = f"- Returned {len(dataframe):,} verified inventory row(s)."
        if value_column:
            uom = rows[0].get("uom") if "uom" in columns else None
            unit_text = f" {uom}" if uom not in (None, "") else ""
            top_text = (
                f"- This shows current inventory {self._answer_scope_text(query_plan)}. "
                f"Top row is {rows[0].get(label)} with "
                f"{self._format_number(rows[0].get(value_column))}{unit_text} "
                f"{value_column.replace('_', ' ')}."
            )
        return (
            "Short summary\n"
            f"{top_text}\n\n"
            "Result\n"
            f"{table}\n\n"
            "Limitations\n"
            "- Current stock is read from `inventory_master.available_quantity`.\n"
            "- Stock value, aging, reorder planning, and project-wise stock need separate verified definitions before use."
        )

    def _verified_billing_answer(self, question, dataframe, query_plan):
        billing_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        if "client_bill_raising" not in billing_tables:
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching billing records were found for the verified filters.\n\n"
                "Limitations\n"
                "- No billing rows matched that exact project, period, status, or filter."
            )
        table = self._markdown_table(rows, columns)
        numeric_columns = [
            column for column in columns
            if self._is_money_column(column)
        ]
        if len(rows) == 1 and numeric_columns:
            value = rows[0].get(numeric_columns[0])
            scope = self._answer_scope_text(query_plan)
            return (
                "Short summary\n"
                f"- Total billing {scope} is {self._format_inr(value)}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Summary\n"
                f"- This answer is about client billing {scope}."
            )
        if rows and numeric_columns:
            label_column = next(
                (
                    column for column in columns
                    if column.lower() in {
                        "project_name",
                        "client_name",
                        "billing_status",
                        "bill_month",
                        "ra_bill_number",
                        "billing_id",
                    }
                ),
                columns[0],
            )
            top_text = (
                f"- This compares billing {self._answer_scope_text(query_plan)}. "
                f"Top row is {rows[0].get(label_column)} with "
                f"{self._format_inr(rows[0].get(numeric_columns[0]))}."
            )
        else:
            top_text = f"- This shows billing {self._answer_scope_text(query_plan)}; {len(dataframe):,} row(s) were returned."
        return (
            "Short summary\n"
            f"{top_text}\n\n"
            "Result\n"
            f"{table}\n\n"
            "Summary\n"
            "- Billing value is calculated from verified billing fields and approved project links."
        )

    def _verified_vendor_answer(self, question, dataframe, query_plan):
        vendor_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        vendor_tables.update(query_plan.generation_tables or [])
        question_vendor_context = bool(
            re.search(r"\bvendors?\b|\bsuppliers?\b", str(question or "").lower())
        )
        if not (
            vendor_tables & {"vendor_mst", "vendor_branch_details", "vendor_types"}
            or (
                question_vendor_context
                and "vendor_name" in list(dataframe.columns)
            )
        ):
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching vendor records were found for the verified filters.\n\n"
                "Limitations\n"
                "- This uses the verified Vendor master and PO header mappings only."
            )
        table = self._markdown_table(rows, columns)
        if len(rows) == 1 and "vendor_count" in columns:
            return (
                "Short summary\n"
                f"- Verified vendor count: {self._format_number(rows[0].get('vendor_count'))}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Count is based on one row per vendor in `vendor_mst`."
            )
        if "purchase_value" in columns:
            label = "vendor_name" if "vendor_name" in columns else columns[0]
            top_text = (
                f"- This ranks vendors by purchase order value {self._answer_scope_text(query_plan)}. "
                f"Top row is {rows[0].get(label)} with "
                f"{self._format_inr(rows[0].get('purchase_value'))} purchase value."
            )
            return (
                "Short summary\n"
                f"{top_text}\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Purchase value is based on `po_form.total_value` at purchase-order header grain.\n"
                "- This is not vendor performance, payment history, or credit analysis."
            )
        if "purchase_order_count" in columns:
            return (
                "Short summary\n"
                f"- This ranks vendors by purchase order count {self._answer_scope_text(query_plan)}. "
                f"Top row is {rows[0].get('vendor_name', rows[0].get(columns[0]))} with "
                f"{self._format_number(rows[0].get('purchase_order_count'))} purchase orders.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Count is based on PO header rows grouped by vendor name."
            )
        if "vendor_name" in columns and "vendor_id" in columns:
            return (
                "Short summary\n"
                f"- This shows vendor master details {self._answer_scope_text(query_plan)}; {len(dataframe):,} vendor row(s) are shown.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- Vendor details come from `vendor_mst`; purchase/order analytics use PO headers separately."
            )
        if "state" in columns or "vendor_category" in columns:
            label = "state" if "state" in columns else "vendor_category"
            return (
                "Short summary\n"
                f"- This groups vendors by {label.replace('_', ' ')}. Top group is {rows[0].get(label)} with "
                f"{self._format_number(rows[0].get('vendor_count'))} vendors.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Limitations\n"
                "- State comes from vendor branch records; category comes from the vendor type lookup."
            )
        return (
            "Short summary\n"
            f"- This shows verified vendor information {self._answer_scope_text(query_plan)}; {len(dataframe):,} row(s) are shown.\n\n"
            "Result\n"
            f"{table}\n\n"
            "Limitations\n"
            "- Only verified vendor lookup, grouping, and purchase-order summaries are included."
        )

    def _verified_po_answer(self, question, dataframe, query_plan):
        po_tables = {
            item.get("table")
            for item in [
                *(query_plan.bound_metrics or []),
                *(query_plan.bound_dimensions or []),
            ]
            if isinstance(item, dict)
        }
        po_tables.update(query_plan.generation_tables or [])
        if not (po_tables & {"po_form", "po_items"}):
            return None
        columns = list(dataframe.columns)
        rows = dataframe.head(10).to_dict(orient="records")
        if dataframe.empty:
            return (
                "Short summary\n"
                "- No matching purchase order records were found for the verified filters.\n\n"
                "Limitations\n"
                "- This means the PO tables have no rows for that exact period, project, vendor, status, or filter."
            )
        table = self._markdown_table(rows, columns)
        numeric_columns = [
            column for column in columns
            if self._is_money_column(column)
            or str(column).lower() in {"row_count", "purchase_order_count"}
            or self._is_quantity_column(column)
        ]
        money_columns = [
            column for column in columns
            if self._is_money_column(column)
        ]
        if len(rows) == 1 and numeric_columns and not any(column in columns for column in ("po_no", "vendor_name", "project_name", "po_month", "status")):
            value_column = numeric_columns[0]
            value = rows[0].get(value_column)
            formatted = (
                self._format_inr(value)
                if self._is_money_column(value_column)
                else self._format_number(value)
            )
            scope = self._answer_scope_text(query_plan)
            return (
                "Short summary\n"
                f"- {value_column.replace('_', ' ').title()} {scope} is {formatted}.\n\n"
                "Result\n"
                f"{table}\n\n"
                "Summary\n"
                f"- This answer is about purchase orders {scope}."
            )
        if rows and money_columns:
            label_column = next(
                (
                    column for column in columns
                    if column.lower() in {
                        "po_no",
                        "vendor_name",
                        "project_name",
                        "po_month",
                        "status",
                    }
                ),
                columns[0],
            )
            label_value = rows[0].get(label_column)
            if label_value in (None, ""):
                label_value = (
                    rows[0].get("vendor_name")
                    or rows[0].get("project_name")
                    or rows[0].get("po_form_id")
                    or "the top purchase order"
                )
            top_text = (
                f"- This shows purchase orders {self._answer_scope_text(query_plan)}. "
                f"Top row is {label_value} with "
                f"{self._format_inr(rows[0].get(money_columns[0]))}."
            )
        else:
            top_text = f"- This shows purchase orders {self._answer_scope_text(query_plan)}; {len(dataframe):,} row(s) were returned."
        return (
            "Short summary\n"
            f"{top_text}\n\n"
            "Result\n"
            f"{table}\n\n"
            "Summary\n"
            "- Purchase order values are calculated at the correct PO level to avoid double counting."
        )

    def _markdown_table(self, rows, columns):
        if not rows:
            return "| Result |\n|---|\n| No rows |"
        columns = list(columns)
        header = "| " + " | ".join(str(column).replace("_", " ").title() for column in columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        body = []
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column)
                if self._is_money_column(column):
                    value = self._format_inr(value)
                values.append(str(value if value is not None else ""))
            body.append("| " + " | ".join(values) + " |")
        return "\n".join([header, divider, *body])

    def _answer_scope_text(self, query_plan):
        constraints = list(getattr(query_plan, "resolved_entity_constraints", []) or [])
        if not constraints:
            return "across all projects"
        labels = []
        for item in constraints[:3]:
            entity_type = str(item.get("entity_type") or "").strip().lower()
            value = item.get("value") or item.get("original_text")
            if not value:
                continue
            if entity_type == "project":
                labels.append(f"for {value}")
            elif entity_type == "vendor":
                labels.append(f"for vendor {value}")
            elif entity_type == "material":
                labels.append(f"for material {value}")
            else:
                labels.append(f"for {value}")
        return " and ".join(labels) if labels else "for the selected filters"

    def _format_inr(self, value):
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            return str(value)
        sign = "-" if amount < 0 else ""
        absolute = abs(amount)
        if absolute >= 10_000_000:
            return f"{sign}₹{absolute / 10_000_000:.2f} Cr"
        if absolute >= 100_000:
            return f"{sign}₹{absolute / 100_000:.2f} Lakh"
        return f"{sign}₹{absolute:,.2f}"

    def _format_number(self, value):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.3f}".rstrip("0").rstrip(".")

    def _to_float(self, value):
        try:
            if value is None:
                return None
            return float(str(value).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return None

    def _is_money_column(self, column):
        column_lower = str(column or "").lower()
        if any(word in column_lower for word in ("id", "number", "month", "date", "status")):
            return False
        return any(word in column_lower for word in ("bill", "billing", "value", "amount", "price", "subtotal", "sub_total"))

    def _is_quantity_column(self, column):
        column_lower = str(column or "").lower()
        if any(word in column_lower for word in ("id", "status", "date")):
            return False
        return any(word in column_lower for word in ("qty", "quantity"))

    def _ecommerce_product_sql(self, question):
        question_lower = question.lower()
        schema = session_memory.get_schema() or {}
        tables = set(schema.keys())

        required_tables = {
            "order_items",
            "products",
            "product_category_name_translation",
        }

        if not required_tables.issubset(tables):
            return None

        product_words = ["product", "item", "category"]
        sold_words = ["sold", "selling", "most", "top", "best"]
        focus_words = ["focus", "recommend", "should", "priority", "improve"]

        asks_product = any(word in question_lower for word in product_words)
        asks_sales_rank = any(word in question_lower for word in sold_words)
        asks_focus = any(word in question_lower for word in focus_words)

        if not asks_product or not (asks_sales_rank or asks_focus):
            return None

        order_by = "revenue DESC" if asks_focus else "units_sold DESC"

        return f"""
SELECT
    t.product_category_name_english AS product_category,
    COUNT(*) AS units_sold,
    SUM(oi.price) AS revenue,
    AVG(oi.price) AS average_price
FROM order_items oi
JOIN products p
    ON oi.product_id = p.product_id
LEFT JOIN product_category_name_translation t
    ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
ORDER BY {order_by}
LIMIT 10
""".strip()

    def _ecommerce_loss_sql(self, question):
        question_lower = question.lower()
        schema = session_memory.get_schema() or {}
        tables = set(schema.keys())

        required_tables = {
            "orders",
            "order_items",
        }

        if not required_tables.issubset(tables):
            return None

        loss_words = ["loss", "lost", "risk", "problem", "manage", "reduce"]
        asks_loss = any(word in question_lower for word in loss_words)

        if not asks_loss:
            return None

        return """
SELECT
    o.order_status,
    COUNT(DISTINCT o.order_id) AS affected_orders,
    SUM(oi.price + oi.freight_value) AS revenue_at_risk,
    AVG(oi.price + oi.freight_value) AS average_order_item_value
FROM orders o
JOIN order_items oi
    ON o.order_id = oi.order_id
WHERE o.order_status IN ('canceled', 'unavailable')
GROUP BY o.order_status
ORDER BY revenue_at_risk DESC
LIMIT 10
""".strip()

    def ask(self, question):
        # Start token tracking
        token_metrics.start()

        question = self._validate_question(question)
        if self.is_casual_message(question):
            response = self.casual_response(has_database=True)
            token_metrics.log_summary()
            return response

        self._ensure_database()
        database_path = session_memory.get_database()["database_path"]
        cache_identity = self._llm_cache_identity()
        cached = response_cache.get(question, database_path, **cache_identity)
        if cached:
            session_memory.add_message("user", question)
            session_memory.add_message("assistant", cached["answer"])
            return cached

        reader = SchemaReader()
        schema_prompt = reader.get_schema_prompt()

        sql = (
            self._ecommerce_product_sql(question)
            or self._ecommerce_loss_sql(question)
        )

        if not sql:
            sql = llm.generate_sql(question, schema_prompt)

        validation = validator.validate(sql)

        retries = 0
        last_error = validation["error"]

        while not validation["success"] and retries < MAX_RETRIES:
            retries += 1
            sql = llm.rewrite_failed_sql(
                question=question,
                schema_prompt=schema_prompt,
                failed_sql=sql,
                error_message=last_error,
            )
            validation = validator.validate(sql)
            last_error = validation["error"]

        if not validation["success"]:
            session_memory.set_last_error(validation["error"])
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return {
                "success": False,
                "answer": (
                    "I could not turn that into a safe query yet. "
                    "Try asking it with the table or field name, for example: "
                    "'Which product had the highest total sales?'"
                ),
                "sql": sql,
                "result": None,
                "error": validation["error"],
                "metrics": metrics,
            }

        execution = sql_executor.execute_safe(sql)

        retries = 0

        while not execution["success"] and retries < MAX_RETRIES:
            retries += 1
            sql = llm.rewrite_failed_sql(
                question=question,
                schema_prompt=schema_prompt,
                failed_sql=sql,
                error_message=execution["error"],
            )

            validation = validator.validate(sql)

            if not validation["success"]:
                execution = {
                    "success": False,
                    "error": validation["error"],
                }
                continue

            execution = sql_executor.execute_safe(sql)

        if not execution["success"]:
            session_memory.set_last_error(execution["error"])
            metrics = token_metrics.get_metrics_summary()
            token_metrics.log_summary()
            return {
                "success": False,
                "answer": (
                    "I found a query idea, but it did not run against this database. "
                    "Try rephrasing with a bit more detail, such as the metric you want "
                    "to rank by: quantity sold, revenue, or order count."
                ),
                "sql": sql,
                "result": None,
                "error": execution["error"],
                "metrics": metrics,
            }

        dataframe = execution["data"]["dataframe"]
        try:
            answer = llm.generate_answer(question, dataframe)
        except RuntimeError:
            answer = self._fallback_answer(question, dataframe)

        result = self._dataframe_payload(dataframe)

        session_memory.add_message("user", question)
        session_memory.add_message("assistant", answer)
        session_memory.set_last_question(question)
        session_memory.set_last_sql(sql)
        session_memory.set_last_result(result)
        session_memory.set_last_error(None)

        metrics = token_metrics.get_metrics_summary()
        token_metrics.log_summary()

        response = {
            "success": True,
            "answer": answer,
            "sql": sql,
            "result": result,
            "error": None,
            "metrics": metrics,
            "cache_hit": False,
        }
        response_cache.set(question, database_path, response, **cache_identity)
        return response


chat_pipeline = ChatPipeline()
