import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from cache_identity import CATALOG_VERSION
from database_fingerprint import sha256_text, stable_json
from semantic_parser import semantic_parser


ENTITY_RESOLVER_VERSION = "entity-resolver:v16"
ENTITY_CACHE_DIR = Path(__file__).resolve().parent / "runtime" / "entity_cache"


@dataclass
class EntityCandidate:
    text: str
    normalized_text: str
    source: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    position: int = 0
    predicted_types: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class ResolvedEntity:
    original_text: str
    normalized_text: str
    entity_type: str
    table: str
    column: str
    primary_key_column: str | None
    primary_key_value: str | int | None
    matched_value: str
    confidence: float
    match_type: str

    def to_dict(self):
        return asdict(self)


@dataclass
class ClarificationDecision:
    required: bool
    reason: str | None = None
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class EntityResolver:
    ENTITY_ALIAS_DICTIONARY = {
        "project": {
            "rdss vizg": "rdss vizag",
            "rdss vizag": "rdss vizag",
            "ug cable": "ugcable",
            "ug cables": "ugcable",
            "ug-cable": "ugcable",
            "ugcable": "ugcable",
            "nklis pkg 1": "nklis package 1",
            "package 1": "package 1",
            "pkg 1": "package 1",
        },
        "vendor": {
            "kei": "kei industries",
            "kei industry": "kei industries",
            "sagar ind": "sagar industries",
            "polycab": "polycab india",
            "pollycab": "polycab india",
            "ultratech": "ultratech cement",
            "ultra tech": "ultratech cement",
            "sail": "steel authority of india",
        },
        "material": {
            "cement": "cement",
            "steel": "steel",
            "conductor": "conductor",
            "rabbit conductor": "rabbit conductor",
            "aaac rabbit": "aaac rabbit",
            "pole": "poles",
            "poles": "poles",
            "cable": "cable",
            "insulator": "insulators",
            "insulators": "insulators",
        },
        "reference": {
            "no": "number",
            "num": "number",
            "ref": "reference",
        },
    }
    COMMON_ALIAS_REPLACEMENTS = {
        "m s": "",
        "ms": "",
        "private": "",
        "pvt": "",
        "limited": "",
        "ltd": "",
        "incorporated": "",
        "inc": "",
        "company": "co",
        "corporation": "corp",
        "engineering": "engg",
        "engineers": "engg",
        "industries": "industry",
        "industrys": "industry",
        "electricals": "electrical",
        "constructions": "construction",
        "enterprises": "enterprise",
        "solutions": "solution",
        "project": "",
        "vendor": "",
        "supplier": "",
        "material": "",
        "item": "",
    }
    ENTITY_HINTS = {
        "project": ("project", "site", "job", "work"),
        "employee": ("employee", "emp", "staff", "worker"),
        "department": ("department", "dept", "division"),
        "vendor": ("vendor", "supplier", "contractor", "seller"),
        "vendor_category": ("vendor category", "vendor type", "category", "type", "msme", "non msme"),
        "customer": ("customer", "client", "party"),
        "invoice": ("invoice", "bill", "billing"),
        "purchase_order": ("purchase", "po", "order", "workorder", "work_order"),
        "material": ("material", "product", "item", "asset", "spare", "equipment"),
        "location": ("city", "state", "country", "location", "site"),
        "reference": ("code", "no", "number", "ref", "serial", "ticket", "key"),
    }
    VALUE_COLUMN_MARKERS = (
        "name",
        "code",
        "no",
        "number",
        "ref",
        "reference",
        "title",
        "description",
        "serial",
        "city",
        "state",
        "country",
        "location",
        "email",
        "gst",
        "pan",
    )
    STOPWORDS = {
        "show",
        "give",
        "details",
        "detail",
        "tell",
        "about",
        "project",
        "vendor",
        "customer",
        "employee",
        "department",
        "invoice",
        "material",
        "site",
        "for",
        "on",
        "the",
        "me",
        "all",
        "what",
        "which",
        "who",
        "how",
        "much",
        "many",
        "last",
        "days",
        "compare",
        "with",
        "and",
        "are",
        "is",
        "any",
        "has",
        "have",
        "one",
        "of",
        "highest",
        "largest",
        "top",
        "purchase",
        "value",
        "values",
        "used",
        "usage",
        "or",
        "return",
        "returned",
        "returns",
        "during",
        "from",
        "to",
        "between",
        "in",
        "sites",
        "projects",
        "vendors",
        "customers",
        "employees",
        "departments",
        "invoices",
        "materials",
        "products",
        "orders",
        "erp",
        "database",
        "db",
        "data",
        "system",
        "table",
        "tables",
        "schema",
        "records",
        "record",
        "module",
        "modules",
        "business",
    }
    STATUS_WORDS = {
        "pending",
        "unpaid",
        "paid",
        "active",
        "inactive",
        "completed",
        "complete",
        "overdue",
        "cancelled",
        "canceled",
        "approved",
        "rejected",
    }

    def cache_path(self, database_fingerprint, schema_fingerprint, entity_text, entity_type=None):
        key = sha256_text(stable_json({
            "version": ENTITY_RESOLVER_VERSION,
            "catalog_version": CATALOG_VERSION,
            "database_fingerprint": database_fingerprint,
            "schema_fingerprint": schema_fingerprint,
            "entity_text": self.normalize(entity_text),
            "entity_type": entity_type,
        }))
        ENTITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return ENTITY_CACHE_DIR / f"{key}.json"

    def get_cached(self, database_fingerprint, schema_fingerprint, entity_text, entity_type=None):
        path = self.cache_path(database_fingerprint, schema_fingerprint, entity_text, entity_type)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set_cached(self, database_fingerprint, schema_fingerprint, entity_text, result, entity_type=None):
        path = self.cache_path(database_fingerprint, schema_fingerprint, entity_text, entity_type)
        payload = {
            **result,
            "cached_at": time.time(),
            "entity_resolver_version": ENTITY_RESOLVER_VERSION,
            "catalog_version": CATALOG_VERSION,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def normalize(self, value):
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char))
        text = re.sub(r"[_\-./]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text

    def compact(self, value):
        return re.sub(r"[^a-z0-9]+", "", self.normalize(value))

    def alias_key(self, value, entity_type=None):
        text = self.normalize(value)
        if not text:
            return ""
        text = self._apply_entity_aliases(text, entity_type)
        text = f" {text} "
        for source, target in sorted(
            self.COMMON_ALIAS_REPLACEMENTS.items(),
            key=lambda item: (-len(item[0]), item[0]),
        ):
            text = re.sub(rf"\b{re.escape(source)}\b", f" {target} ", text)
        return re.sub(r"\s+", " ", text).strip()

    def extract(self, question):
        """Backward-compatible entry point that delegates to the semantic parser."""
        plan = semantic_parser.parse(str(question or ""), allow_llm_fallback=False).to_dict()
        return self.candidates_from_plan(plan)

    def candidates_from_plan(self, parsed_question):
        """Return only plausible stored-value mentions from a parsed question."""
        plan = parsed_question.to_dict() if hasattr(parsed_question, "to_dict") else dict(parsed_question or {})
        original = str(plan.get("original_question") or "")
        selected = []
        search_from = 0
        for index, mention in enumerate(plan.get("candidate_entity_mentions") or []):
            text = mention.get("text") if isinstance(mention, dict) else mention
            cleaned = self._clean_candidate_text(text)
            if not cleaned or self._candidate_is_semantic(cleaned, plan, "semantic_parser"):
                continue
            position = original.lower().find(cleaned.lower(), search_from) if original else index
            if position < 0:
                position = original.lower().find(cleaned.lower()) if original else index
            position = max(index, position)
            search_from = position + len(cleaned)
            selected.append(EntityCandidate(
                text=cleaned,
                normalized_text=self.normalize(cleaned),
                source="semantic_parser",
                evidence=["semantic_role=entity_candidate"],
                confidence=0.96,
                position=position,
            ))
        for candidate in selected:
            candidate.predicted_types = self.predict_types_from_plan(candidate.text, plan)[:3]
        return [candidate.to_dict() for candidate in selected[:10]]

    def _add_candidate(self, candidates, text, original, source, evidence, confidence, position=None):
        cleaned = self._clean_candidate_text(text)
        if not cleaned:
            return
        position = original.lower().find(cleaned.lower()) if position is None else position
        candidates.append(EntityCandidate(
            text=cleaned,
            normalized_text=self.normalize(cleaned),
            source=source,
            evidence=evidence,
            confidence=confidence,
            position=max(0, position),
        ))

    def _clean_candidate_text(self, text):
        value = re.sub(r"^[\s,;:.!?]+|[\s,;:.!?]+$", "", str(text or "").strip())
        value = value.strip("'\"")
        value = re.sub(
            r"^(?:show|list|give|tell|find|compare|what|which|who|where|when|how|for|in|at|from|by|on)\s+(?:me\s+)?",
            "",
            value,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", value).strip(" -")

    def _candidate_is_semantic(self, text, plan, source=None):
        normalized = self.normalize(text)
        if not normalized or self._is_constraint_text(text):
            return True
        blocked_phrases = set()
        for key in ("metrics", "dimensions", "aggregations", "grouping_hints", "requested_output", "sub_operations"):
            blocked_phrases.update(self.normalize(value) for value in plan.get(key, []) if isinstance(value, str))
        for key in ("metric_terms", "dimension_terms", "requested_outputs"):
            for item in plan.get(key, []):
                if isinstance(item, dict):
                    blocked_phrases.add(self.normalize(item.get("raw_text")))
                    blocked_phrases.add(self.normalize(item.get("normalized_text")))
        for item in plan.get("status_constraints", []):
            blocked_phrases.update(self.normalize(value) for value in item.get("values", []))
        if normalized in blocked_phrases:
            return True
        words = set(normalized.split())
        if words and words <= self.STOPWORDS:
            return True
        semantic_tokens = set()
        for phrase in blocked_phrases:
            semantic_tokens.update(phrase.split())
        if words and words <= semantic_tokens:
            return True
        if re.search(r"\b(top|bottom|highest|lowest|total|average|monthly|weekly|yearly|trend|performance)\b", normalized):
            return True
        if re.search(r"\b(?:does|do|is|are|has|have|cover|contain|available)\b", normalized):
            return True
        if re.search(r"\b(last|past|this|current)\s+(?:\d+\s+)?(?:days?|weeks?|months?|quarters?|years?)\b", normalized):
            return True
        return False

    def _candidate_overlap(self, left, right):
        left_key = self.normalize(left)
        right_key = self.normalize(right)
        if not left_key or not right_key:
            return False
        return left_key in right_key or right_key in left_key

    def _canonical_type(self, value):
        return {"supplier": "vendor", "client": "customer", "site": "location", "order": "purchase_order"}.get(value.lower(), value.lower())

    def extract_constraints(self, question):
        text = str(question or "")
        dates = []
        for year in re.findall(r"\b(20\d{2}|19\d{2})\b", text):
            dates.append({
                "text": year,
                "type": "year",
                "start": f"{year}-01-01",
                "end": f"{year}-12-31",
                "confidence": 100,
            })
        relative_patterns = [
            (r"\blast\s+(\d+)\s+days?\b", "last_days"),
            (r"\bpast\s+(\d+)\s+days?\b", "last_days"),
        ]
        for pattern, kind in relative_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dates.append({
                    "text": match.group(0),
                    "type": kind,
                    "days": int(match.group(1)),
                    "confidence": 95,
                })
        status = sorted({word for word in re.findall(r"[A-Za-z]+", text.lower()) if word in self.STATUS_WORDS})
        limit = None
        limit_match = re.search(r"\btop\s+(\d{1,3})\b|\blimit\s+(\d{1,3})\b", text, re.IGNORECASE)
        if limit_match:
            limit = int(next(group for group in limit_match.groups() if group))
        return {
            "dates": dates,
            "filters": [{"type": "status", "values": status}] if status else [],
            "limit": limit,
        }

    def predict_types(self, mention, question=""):
        context = str(mention or "").lower()
        metric_context = re.sub(
            r"\b(billing|bill|invoices?|invoice|purchase orders?|payments?|revenue|material usage|vendors?|customers?|employees?|departments?|materials?|products?)\b",
            " ",
            str(question or "").lower(),
        )
        local_context = f"{metric_context} {context}"
        scores = []
        for entity_type, hints in self.ENTITY_HINTS.items():
            score = sum(2 for hint in hints if re.search(rf"\b{re.escape(hint)}s?\b", context))
            score += sum(1 for hint in hints if re.search(rf"\b{re.escape(hint)}s?\b", local_context))
            if score:
                scores.append((score, entity_type))
        if re.search(r"\binv(?:oice)?[-_\s]?\d+\b", mention, re.IGNORECASE):
            scores.append((5, "invoice"))
        if re.search(r"\bpo[-_\s]?\d+\b", mention, re.IGNORECASE):
            scores.append((5, "purchase_order"))
        if re.search(r"\bcompare\b|\bvs\b|\bversus\b", str(question or "").lower()):
            scores = [(score + (2 if entity_type == "project" else 0), entity_type) for score, entity_type in scores]
            if re.search(r"[A-Za-z].*[-_].*[A-Za-z]|\b[A-Z]{3,}\b", str(mention or "")):
                scores.append((3, "project"))
        question_lower = str(question or "").lower()
        mention_pattern = re.escape(str(mention or "").lower())
        if (
            re.search(rf"\b(?:for|on|at|in|from|by)\s+{mention_pattern}\b", question_lower)
            and re.search(r"\b(vendors?|materials?|billing|purchases?|invoices?|employees?)\b", question_lower)
        ):
            scores.append((4, "project"))
            scores.append((2, "location"))
        return [entity_type for _score, entity_type in sorted(scores, key=lambda item: (-item[0], item[1]))[:3]]

    def predict_types_from_plan(self, mention, parsed_question):
        plan = parsed_question.to_dict() if hasattr(parsed_question, "to_dict") else dict(parsed_question or {})
        normalized = self.normalize(mention)
        original_lower = str(plan.get("original_question") or "").lower()
        scores = []
        explicit_patterns = (
            (r"^(?:project|job)\b", "project", 10),
            (r"^site\b", "location", 10),
            (r"^(?:employee|emp)\b", "employee", 10),
            (r"^invoice\b|^inv[-/ ]?\d", "invoice", 10),
            (r"^(?:vendor|supplier)\b", "vendor", 10),
            (r"^(?:client|customer)\b", "customer", 10),
            (r"^department\b", "department", 10),
            (r"^(?:order|po)[-/ ]?\d", "purchase_order", 10),
        )
        for pattern, entity_type, score in explicit_patterns:
            if re.search(pattern, normalized):
                scores.append((score, entity_type))

        for item in plan.get("filters", []):
            if self.normalize(item.get("raw_value")) != normalized:
                continue
            field = self._canonical_type(str(item.get("field_concept") or ""))
            if field in self.ENTITY_HINTS:
                scores.append((8, field))

        if "purchase_value" in {
            self.normalize(value).replace(" ", "_")
            for value in plan.get("metrics") or []
        }:
            scores.append((10, "vendor"))

        mention_text = str(mention or "")
        mention_pattern = re.escape(mention_text.lower())
        if mention_pattern:
            if re.search(rf"\b{mention_pattern}\s+(?:project|site|job)\b", original_lower):
                scores.append((9, "project"))
            if re.search(rf"\b(?:project|site|job)\s+{mention_pattern}\b", original_lower):
                scores.append((9, "project"))
        if (
            re.search(r"[A-Za-z].*[-_].*[A-Za-z]|\b[A-Z]{3,}\b", mention_text)
            and (
                "project" in (plan.get("dimensions") or [])
                or re.search(r"\b(projects?|sites?|billing|materials?|issued|received|grn|workorders?)\b", original_lower)
                or plan.get("operation") == "comparison"
            )
        ):
            scores.append((9, "project"))
        if (
            re.search(r"\bpurchase\s+orders?\b|\bpo\b|\bpurchase\s+value\b", original_lower)
            and re.search(rf"\b(?:for|from|by)\s+{mention_pattern}\b", original_lower)
        ):
            scores.append((10, "vendor"))
        if re.search(r"\bvendors?\b", original_lower):
            if re.search(r"\b(?:vendor\s+)?code\s+", original_lower) or re.fullmatch(r"v\d+", normalized):
                scores.append((12, "vendor"))
            if re.search(r"\b(?:branches?|city|state|location)\b", original_lower):
                scores.append((10, "location"))
            if re.search(r"\b(?:category|type|msme|non\s+msme)\b", original_lower):
                scores.append((10, "vendor_category"))

        # Requested outputs describe what to fetch, not necessarily the type of
        # a filter value. Only use them when the mention explicitly carries the type.
        identifier = re.search(r"\b(inv|po|emp)[-/ ]?\d", normalized)
        if not scores and identifier:
            prefix = identifier.group(1)
            scores.append((8, {"inv": "invoice", "po": "purchase_order", "emp": "employee"}[prefix]))
        if not scores:
            contract = plan.get("question_contract") or {}
            concepts = [
                *(plan.get("dimensions") or []),
                *(plan.get("requested_output") or []),
                *(contract.get("required_dimensions") or []),
                *(contract.get("required_outputs") or []),
            ]
            for concept in concepts:
                canonical = self._canonical_type(str(concept or ""))
                if canonical in self.ENTITY_HINTS:
                    scores.append((4, canonical))
                if canonical == "project":
                    scores.extend([(3, "customer"), (2, "location")])
                if canonical in {"purchase_order", "invoice"}:
                    scores.extend([(3, "vendor"), (3, "customer"), (2, "project")])
                if canonical == "material":
                    scores.extend([(3, "vendor"), (2, "project")])
        return [entity_type for _score, entity_type in sorted(scores, key=lambda item: (-item[0], item[1]))[:3]]

    def role_for(self, mention, question, entity_type=None):
        lower = question.lower()
        if re.search(r"\bcompare\b|\bvs\b|\bversus\b", lower):
            return "comparison_item"
        if entity_type and re.search(rf"\b{re.escape(entity_type)}s?\b", lower):
            return f"filter_{entity_type}"
        return "anchor_entity"

    def role_for_plan(self, mention, parsed_question, entity_type=None):
        plan = parsed_question.to_dict() if hasattr(parsed_question, "to_dict") else dict(parsed_question or {})
        operations = {plan.get("operation"), *(plan.get("sub_operations") or [])}
        if "comparison" in operations:
            return "comparison_item"
        for item in plan.get("filters", []):
            if self.normalize(item.get("raw_value")) == self.normalize(mention):
                return f"filter_{item.get('field_concept') or entity_type or 'entity'}"
        return "anchor_entity"

    def clarification_decision(self, mention_results):
        candidates = []
        for mention_result in mention_results:
            matches = mention_result.get("matches", [])
            essential = mention_result.get("essential")
            if essential is None:
                essential = bool(matches)
            if not matches:
                if essential:
                    candidates.append({
                        "mention": mention_result.get("mention"),
                        "reason": "required_filter_value_not_found",
                        "options": [],
                    })
                continue
            close = self.equally_likely_matches(
                matches,
                predicted_types=mention_result.get("predicted_types", []),
            )
            if len(close) < 2 or not essential:
                continue
            candidates.append({
                "mention": mention_result.get("mention"),
                "reason": "multiple_database_values_equally_likely",
                "options": close[:5],
            })
        if not candidates:
            return ClarificationDecision(required=False)
        return ClarificationDecision(
            required=True,
            reason="A required entity filter is unresolved or has multiple equally likely database matches.",
            candidates=candidates,
        )

    def equally_likely_matches(self, matches, predicted_types=()):
        if len(matches) < 2:
            return []
        ranked = self.rank_matches(matches, predicted_types)
        best_score = self._match_effective_score(ranked[0], predicted_types)
        close = [item for item in ranked if best_score - self._match_effective_score(item, predicted_types) <= 2.0]
        distinct = []
        targets = set()
        first_match_by_value = {}
        for item in close:
            base_target = (
                item.get("entity_type"),
                self.normalize(item.get("value")),
            )
            previous = first_match_by_value.setdefault(base_target, item)
            if (
                previous is not item
                and previous.get("table") == item.get("table")
                and previous.get("primary_key") != item.get("primary_key")
            ):
                target = (
                    *base_target,
                    item.get("table"),
                    item.get("primary_key"),
                )
            else:
                target = base_target
            if target not in targets:
                targets.add(target)
                distinct.append(item)
        return distinct if len(distinct) > 1 else []

    def rank_matches(self, matches, predicted_types=()):
        return sorted(
            matches,
            key=lambda item: (
                -self._match_effective_score(item, predicted_types),
                -float(item.get("confidence", 0)),
                item.get("table", ""),
                str(item.get("primary_key")),
            ),
        )

    def _match_effective_score(self, match, predicted_types=()):
        predicted_types = set(predicted_types or [])
        type_bonus = 6 if predicted_types and match.get("entity_type") in predicted_types else 0
        anchor_bonus = min(8.0, float(match.get("anchor_score", 0) or 0) / 10.0)
        table = str(match.get("table") or "").lower()
        master_bonus = 3 if table.endswith(("_mst", "_master")) else 0
        return float(match.get("confidence", 0)) + type_bonus + anchor_bonus + master_bonus

    def should_clarify_matches(self, matches, predicted_types=()):
        return len(self.equally_likely_matches(matches, predicted_types)) > 1

    def build_multi_result(self, question, mention_results, relationships=None, semantic_plan=None):
        constraints = self._constraints_from_plan(semantic_plan) if semantic_plan else self.extract_constraints(question)
        clarification = self.clarification_decision(mention_results)
        ambiguity_by_mention = {}
        for item in mention_results:
            close = self.equally_likely_matches(item.get("matches", []), item.get("predicted_types", []))
            if close:
                ambiguity_by_mention[item.get("mention")] = close
        ambiguous_mentions = set(ambiguity_by_mention)
        entities = []
        unresolved = []
        ambiguities = []
        for mention_result in mention_results:
            mention = mention_result["mention"]
            matches = mention_result.get("matches", [])
            predicted = mention_result.get("predicted_types") or (
                self.predict_types_from_plan(mention, semantic_plan) if semantic_plan else self.predict_types(mention, question)
            )
            if not matches:
                unresolved.append({
                    "mention": mention,
                    "original_text": mention,
                    "normalized_text": self.normalize(mention),
                    "predicted_types": predicted,
                    "essential": bool(mention_result.get("essential")),
                    "reason": "No matching value was found in the searched database columns.",
                    "status": "unresolved",
                })
                continue
            if mention in ambiguous_mentions:
                ambiguities.append({
                    "mention": mention,
                    "essential": bool(mention_result.get("essential")),
                    "options": ambiguity_by_mention[mention],
                })
            ranked_matches = self.rank_matches(matches, predicted)
            best = ranked_matches[0]
            resolved = ResolvedEntity(
                original_text=mention,
                normalized_text=self.normalize(mention),
                entity_type=best["entity_type"],
                table=best["table"],
                column=best["column"],
                primary_key_column=best.get("primary_key_column"),
                primary_key_value=best.get("primary_key_value", best.get("primary_key")),
                matched_value=str(best.get("matched_value", best.get("value", ""))),
                confidence=float(best["confidence"]),
                match_type=best["match_type"],
            ).to_dict()
            entities.append({
                **resolved,
                # Compatibility keys used by existing prompt and relationship code.
                "mention": mention,
                "predicted_types": predicted,
                "resolved_type": best["entity_type"],
                "primary_key": resolved["primary_key_value"],
                "value": resolved["matched_value"],
                "role": self.role_for_plan(mention, semantic_plan, best["entity_type"]) if semantic_plan else self.role_for(mention, question, best["entity_type"]),
                "status": "ambiguous" if mention in ambiguous_mentions else "resolved",
            })
        relationships = relationships or self.relationships_for(entities)
        relationship_gaps = [
            relationship for relationship in relationships
            if relationship.get("validated") is False
            and relationship.get("relationship_type") != "same_entity_type_comparison"
        ]
        return {
            "entities": entities,
            "dates": constraints["dates"],
            "filters": constraints["filters"],
            "limit": constraints["limit"],
            "comparisons": [
                {"mentions": [entity["mention"] for entity in entities], "type": "same_type"}
            ] if self._is_comparison(semantic_plan, question, entities) else [],
            "relationships": relationships,
            "ambiguities": ambiguities,
            "unresolved": unresolved,
            "relationship_gaps": relationship_gaps,
            "needs_clarification": clarification.required,
            "clarification": clarification.to_dict(),
            "combined_confidence": self.combined_confidence(entities, relationships, ambiguities, unresolved),
        }

    def _constraints_from_plan(self, semantic_plan):
        plan = semantic_plan.to_dict() if hasattr(semantic_plan, "to_dict") else dict(semantic_plan or {})
        return {
            "dates": plan.get("date_constraints", []),
            "filters": plan.get("status_constraints", []),
            "limit": plan.get("limit"),
        }

    def _is_comparison(self, semantic_plan, question, entities):
        if semantic_plan:
            plan = semantic_plan.to_dict() if hasattr(semantic_plan, "to_dict") else semantic_plan
            operations = {plan.get("operation"), *(plan.get("sub_operations") or [])}
            return "comparison" in operations and len(entities) > 1 and len({entity.get("resolved_type") for entity in entities}) == 1
        return self._is_same_type_comparison(question, entities)

    def relationships_for(self, entities):
        relationships = []
        for index, left in enumerate(entities):
            for right in entities[index + 1:]:
                if left.get("resolved_type") == right.get("resolved_type"):
                    relationships.append({
                        "from_entity": left["mention"],
                        "to_entity": right["mention"],
                        "relationship_type": "same_entity_type_comparison",
                        "confidence": 100,
                    })
                else:
                    relationships.append({
                        "from_entity": left["mention"],
                        "to_entity": right["mention"],
                        "relationship_type": "requires_join_path_validation",
                        "confidence": 50,
                    })
        return relationships

    def combined_confidence(self, entities, relationships, ambiguities, unresolved):
        if unresolved:
            return 0
        if any(relationship.get("validated") is False for relationship in relationships):
            return 0
        if not entities:
            return 0
        lowest_entity = min(entity["confidence"] for entity in entities)
        relationship_confidence = min([rel["confidence"] for rel in relationships], default=100)
        penalty = len(ambiguities) * 20
        return max(0, min(100, min(lowest_entity, relationship_confidence) - penalty))

    def candidate_columns(
        self,
        schema,
        catalog=None,
        entity_type=None,
        max_columns=160,
        relevant_tables=None,
        relevant_columns=None,
    ):
        entity_types = [entity_type] if entity_type else []
        if not entity_types and catalog:
            entity_types = list(catalog.get("entities", {}).keys())
        allowed_tables = set(relevant_tables or [])
        preferred_columns = {
            (table, column)
            for table, columns in (relevant_columns or {}).items()
            for column in columns
        }

        candidates = []
        for table, info in schema.items():
            if allowed_tables and table not in allowed_tables:
                continue
            columns = info.get("columns", [])
            row_count = info.get("row_count")
            for column in columns:
                name = self.column_name(column)
                dtype = self.column_type(column)
                identifier_like = bool(
                    column.get("primary_key")
                    or column.get("unique")
                    or re.search(r"(?:^id$|_id$|code|number|_no$|ref)", name, re.IGNORECASE)
                )
                if not name or not self._is_value_type(dtype, identifier_like=identifier_like):
                    continue
                comment = str(column.get("comment") or column.get("COLUMN_COMMENT") or "")
                lowered = f"{table} {name} {comment}".lower()
                if (
                    not any(marker in lowered for marker in self.VALUE_COLUMN_MARKERS)
                    and (table, name) not in preferred_columns
                    and not identifier_like
                ):
                    continue
                inferred_type = self.infer_entity_type(table, name, entity_types)
                score = self._column_score(table, name, inferred_type, row_count)
                if entity_type and inferred_type != entity_type:
                    continue
                candidates.append({
                    "table": table,
                    "column": name,
                    "entity_type": inferred_type or "reference",
                    "score": score,
                    "row_count": row_count,
                })
        return sorted(candidates, key=lambda item: (-item["score"], item["table"], item["column"]))[:max_columns]

    def score_value(self, entity_text, value):
        raw_query = str(entity_text or "")
        raw_candidate = str(value or "")
        query = self.normalize(raw_query)
        candidate = self.normalize(raw_candidate)
        if not query or not candidate:
            return None
        if raw_query == raw_candidate:
            return 100, "exact_match"
        if raw_query.casefold() == raw_candidate.casefold():
            return 98, "case_insensitive_match"
        if raw_query.strip().casefold() == raw_candidate.strip().casefold():
            return 96, "trimmed_match"
        if query == candidate:
            return 95, "alias_match"
        if self.compact(entity_text) == self.compact(value):
            return 94, "alias_match"
        alias_query = self._alias_normalize(query, entity_type=None)
        alias_candidate = self._alias_normalize(candidate, entity_type=None)
        if alias_query and alias_query == alias_candidate:
            return 92, "alias_match"
        if self._acronym_match(alias_query or query, alias_candidate or candidate):
            return 90, "alias_match"
        if (
            len(query.split()) == len(candidate.split())
            and self._tokens_are_abbreviation(query.split(), candidate.split())
        ):
            return 84, "alias_match"
        if candidate.startswith(query):
            return 88, "partial_match"
        if candidate.endswith(query):
            return 82, "partial_match"
        if query in candidate:
            return 76, "partial_match"
        query_tokens = set(query.split())
        value_tokens = set(candidate.split())
        if query_tokens and query_tokens <= value_tokens:
            return 72, "partial_match"
        ratio = SequenceMatcher(None, query, candidate).ratio()
        compact_ratio = SequenceMatcher(None, self.compact(entity_text), self.compact(value)).ratio()
        best = max(ratio, compact_ratio)
        if best >= 0.82:
            return int(55 + best * 30), "fuzzy_match"
        return None

    def lookup_variants(self, entity_text, entity_type=None):
        """Return deterministic stored-value forms without losing the original mention."""
        original = str(entity_text or "").strip()
        variants = [original] if original else []
        alias = self._apply_entity_aliases(self.normalize(original), entity_type)
        if alias and alias != self.normalize(original):
            variants.append(alias)
        types = [entity_type] if entity_type in self.ENTITY_HINTS else list(self.ENTITY_HINTS)
        labels = {
            label
            for current_type in types
            for label in (current_type, *self.ENTITY_HINTS.get(current_type, ()))
        }
        for label in sorted(labels, key=lambda item: (-len(item), item)):
            stripped = re.sub(
                rf"^(?:the\s+)?{re.escape(label)}(?:\s+(?:number|no|code))?[\s:#-]+",
                "",
                original,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if stripped and stripped != original:
                variants.append(stripped)
                stripped_alias = self._apply_entity_aliases(self.normalize(stripped), entity_type)
                if stripped_alias and stripped_alias != self.normalize(stripped):
                    variants.append(stripped_alias)
        if entity_type == "vendor":
            stripped = re.sub(
                r"^(?:the\s+)?(?:vendor\s+)?code[\s:#-]+",
                "",
                original,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if stripped and stripped != original:
                variants.append(stripped)
        code_stripped = re.sub(
            r"^(?:the\s+)?(?:code|no|number|ref|reference)[\s:#-]+",
            "",
            original,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if code_stripped and code_stripped != original:
            variants.append(code_stripped)
        deduped = []
        seen = set()
        for variant in variants:
            key = variant.casefold()
            if key not in seen:
                seen.add(key)
                deduped.append(variant)
        return deduped

    def score_resolved_value(self, entity_text, value, entity_type=None):
        scored = self.score_value(entity_text, value)
        typed_score = self._typed_alias_score(entity_text, value, entity_type)
        if typed_score and (not scored or typed_score[0] > scored[0]):
            scored = typed_score
        if scored:
            return scored
        best = None
        for variant in self.lookup_variants(entity_text, entity_type)[1:]:
            variant_score = self.score_value(variant, value)
            variant_typed_score = self._typed_alias_score(variant, value, entity_type)
            if variant_typed_score and (
                not variant_score or variant_typed_score[0] > variant_score[0]
            ):
                variant_score = variant_typed_score
            if not variant_score:
                continue
            confidence, match_type = variant_score
            if match_type in {"exact_match", "case_insensitive_match", "trimmed_match", "alias_match"}:
                confidence, match_type = min(92, confidence), "alias_match"
            candidate = (confidence, match_type)
            if best is None or candidate[0] > best[0]:
                best = candidate
        return best

    def mention_is_essential(self, mention, parsed_question):
        plan = parsed_question.to_dict() if hasattr(parsed_question, "to_dict") else dict(parsed_question or {})
        normalized = self.normalize(mention)
        matching_filters = [
            item for item in plan.get("filters", [])
            if self.normalize(item.get("raw_value")) == normalized
        ]
        if matching_filters:
            return any(item.get("essential", True) for item in matching_filters)
        operations = {plan.get("operation"), *(plan.get("sub_operations") or [])}
        if "comparison" in operations or "relationship_lookup" in operations:
            return True
        return False

    def _score_value_for_type(self, entity_text, value, entity_type=None):
        scored = self.score_value(entity_text, value)
        if scored:
            return scored
        return self._typed_alias_score(entity_text, value, entity_type)

    def _typed_alias_score(self, entity_text, value, entity_type=None):
        alias_query = self._alias_normalize(entity_text, entity_type)
        alias_candidate = self._alias_normalize(value, entity_type)
        if alias_query and alias_candidate:
            if alias_query == alias_candidate:
                return 92, "alias_match"
            if self.compact(alias_query) == self.compact(alias_candidate):
                return 91, "alias_match"
            if self._acronym_match(alias_query, alias_candidate):
                return 90, "alias_match"
            query_tokens = set(alias_query.split())
            candidate_tokens = set(alias_candidate.split())
            if query_tokens and query_tokens <= candidate_tokens:
                return 90, "alias_match"
            ratio = SequenceMatcher(None, alias_query, alias_candidate).ratio()
            compact_ratio = SequenceMatcher(None, self.compact(alias_query), self.compact(alias_candidate)).ratio()
            best = max(ratio, compact_ratio)
            if best >= 0.78:
                return int(55 + best * 30), "fuzzy_match"
        return None

    def _alias_normalize(self, value, entity_type=None):
        return self.alias_key(value, entity_type)

    def _apply_entity_aliases(self, normalized_text, entity_type=None):
        text = self.normalize(normalized_text)
        if not text:
            return ""
        dictionaries = []
        if entity_type in self.ENTITY_ALIAS_DICTIONARY:
            dictionaries.append(self.ENTITY_ALIAS_DICTIONARY[entity_type])
        dictionaries.append(self.ENTITY_ALIAS_DICTIONARY.get("reference", {}))
        for aliases in dictionaries:
            for source, target in sorted(aliases.items(), key=lambda item: (-len(item[0]), item[0])):
                text = re.sub(rf"\b{re.escape(source)}\b", target, text)
        return re.sub(r"\s+", " ", text).strip()

    def _acronym_match(self, query, candidate):
        query_key = self.compact(query)
        candidate_tokens = [
            token for token in self.normalize(candidate).split()
            if token and token not in self.COMMON_ALIAS_REPLACEMENTS
        ]
        if not query_key or len(query_key) < 2 or not candidate_tokens:
            return False
        acronym = "".join(token[0] for token in candidate_tokens if token)
        return query_key == acronym or query_key in acronym

    def _tokens_are_abbreviation(self, query_tokens, candidate_tokens):
        if not query_tokens or not candidate_tokens or len(query_tokens) > len(candidate_tokens):
            return False
        candidate_index = 0
        for query_token in query_tokens:
            matched = False
            while candidate_index < len(candidate_tokens):
                candidate_token = candidate_tokens[candidate_index]
                candidate_index += 1
                shorter, longer = sorted((query_token, candidate_token), key=len)
                if len(shorter) >= 3 and longer.startswith(shorter):
                    matched = True
                    break
                if len(query_token) == 1 and candidate_token.startswith(query_token):
                    matched = True
                    break
            if not matched:
                return False
        return True

    def infer_entity_type(self, table, column, preferred=()):
        text = f"{table} {column}".lower()
        scores = []
        for entity, hints in self.ENTITY_HINTS.items():
            score = sum(1 for hint in hints if hint in text)
            if score and entity in preferred:
                score += 1
            if score:
                scores.append((score, entity))
        return sorted(scores, key=lambda item: (-item[0], item[1]))[0][1] if scores else None

    def format_prompt(self, resolution):
        if not resolution or not resolution.get("matches"):
            return "Resolved entities: none found."
        lines = ["Resolved entities from value lookup:"]
        for match in resolution["matches"][:8]:
            lines.append(
                "- "
                f"{match['entity']} -> {match['entity_type']} "
                f"{match['table']}.{match['column']}={match['value']!r}; "
                f"primary_key={match.get('primary_key')}; confidence={match['confidence']}; "
                f"match_type={match['match_type']}"
            )
        if resolution.get("ambiguous"):
            lines.append("Ambiguity: multiple plausible entity matches were found; avoid guessing if they conflict.")
        return "\n".join(lines)

    def format_multi_prompt(self, resolution):
        if not resolution or not resolution.get("entities"):
            return self.format_prompt(resolution)
        lines = ["Resolved entity plan:"]
        for entity in resolution["entities"]:
            lines.append(
                "- "
                f"{entity['mention']} -> {entity['resolved_type']} "
                f"{entity['table']}.{entity['column']}={entity.get('value')!r}; "
                f"primary_key={entity.get('primary_key')}; role={entity.get('role')}; "
                f"confidence={entity['confidence']}"
            )
        if resolution.get("dates"):
            lines.append(f"Date constraints: {json.dumps(resolution['dates'], default=str)}")
        if resolution.get("filters"):
            lines.append(f"Filters: {json.dumps(resolution['filters'], default=str)}")
        if resolution.get("relationships"):
            lines.append(f"Entity relationships: {json.dumps(resolution['relationships'], default=str)}")
        if resolution.get("needs_clarification"):
            lines.append("Clarification is needed because multiple stored values are equally likely; do not guess between them.")
        return "\n".join(lines)

    def _extract_list_mentions(self, question):
        text = re.sub(r"[?.!]+$", "", str(question or ""))
        segments = re.split(r"\bcompare\b|\bshow\b|\btell\b|\bgive\b", text, flags=re.IGNORECASE)
        candidates = []
        for segment in segments:
            segment = re.sub(
                r"\b(?:billing|invoices?|purchase orders?|employees?|materials?|details?|projects?|vendors?|customers?|departments?|during|in|for|from|on|by|with|between)\b",
                " ",
                segment,
                flags=re.IGNORECASE,
            )
            segment = re.sub(r"\b(?:20\d{2}|19\d{2})\b", " ", segment)
            for part in re.split(r"\s+(?:and|or|vs|versus)\s+|,", segment, flags=re.IGNORECASE):
                cleaned = self._clean_entity_text(part)
                if cleaned and not re.fullmatch(r"\d{4}", cleaned):
                    candidates.append(cleaned)
        return candidates[:8]

    def _is_constraint_text(self, text):
        normalized = self.normalize(text)
        if re.fullmatch(r"(20\d{2}|19\d{2})", normalized):
            return True
        if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
            return True
        return normalized in self.STATUS_WORDS

    def _is_same_type_comparison(self, question, entities):
        if not re.search(r"\bcompare\b|\bvs\b|\bversus\b", question.lower()):
            return False
        types = {entity.get("resolved_type") for entity in entities}
        return len(entities) >= 2 and len(types) == 1

    def _clean_entity_text(self, text):
        text = re.sub(r"[?.!,;:]+$", "", str(text or "").strip())
        text = re.sub(r"^(?:me|all|the|a|an)\s+", "", text, flags=re.IGNORECASE)
        tokens = []
        for token in re.split(r"\s+", text):
            lowered = token.lower()
            singular = lowered[:-1] if lowered.endswith("s") and len(lowered) > 3 else lowered
            if lowered.isdigit() or lowered in self.STOPWORDS or singular in self.STOPWORDS:
                continue
            tokens.append(token)
        cleaned = " ".join(tokens).strip()
        return cleaned if len(cleaned) >= 2 else ""

    def _is_value_type(self, dtype, *, identifier_like=False):
        dtype = str(dtype or "").lower()
        if any(marker in dtype for marker in ("char", "text", "string", "varchar", "nvarchar", "enum", "uuid")):
            return True
        return identifier_like and any(marker in dtype for marker in ("int", "numeric", "decimal", "number", "bigint"))

    def _column_score(self, table, column, entity_type, row_count):
        text = f"{table} {column}".lower()
        score = 10
        if any(marker in column.lower() for marker in ("name", "code", "no", "number")):
            score += 20
        if any(marker in column.lower() for marker in ("description", "title", "ref", "serial")):
            score += 10
        if entity_type:
            score += 15
        if table.lower().endswith(("_mst", "_master", "_table")):
            score += 10
        if re.search(r"(hist|history|log|attachment|notification)", text):
            score -= 15
        if row_count:
            score += min(15, int(row_count or 0).bit_length())
        return score

    @staticmethod
    def column_name(column):
        return column.get("column_name") or column.get("name") or column.get("COLUMN_NAME") or ""

    @staticmethod
    def column_type(column):
        return column.get("data_type") or column.get("type") or column.get("DATA_TYPE") or ""


entity_resolver = EntityResolver()
