import calendar
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Callable


SEMANTIC_PARSER_VERSION = "semantic-parser:v15"


@dataclass
class SemanticTerm:
    raw_text: str
    normalized_text: str
    role: str
    confidence: float


@dataclass
class AggregationSpec:
    function: str
    target: str | None = None
    distinct: bool = False
    confidence: float = 0.9


@dataclass
class FilterSpec:
    field_concept: str | None
    operator: str
    raw_value: str
    normalized_value: str | int | float | bool | None
    essential: bool = True
    confidence: float = 0.85


@dataclass
class NumericConstraint:
    field_concept: str | None
    operator: str
    value: float
    unit: str | None = None
    confidence: float = 0.9


@dataclass
class OrderingSpec:
    target_concept: str | None
    direction: str
    nulls: str | None = None
    confidence: float = 0.9


@dataclass
class RelationshipHint:
    requested_source_concept: str | None
    requested_target_concept: str
    relation_phrase: str
    confidence: float = 0.8


@dataclass
class QuestionContract:
    required_outputs: list[str] = field(default_factory=list)
    required_metrics: list[str] = field(default_factory=list)
    required_dimensions: list[str] = field(default_factory=list)
    required_filters: list[str] = field(default_factory=list)
    required_entity_mentions: list[str] = field(default_factory=list)
    required_date_constraints: list[str] = field(default_factory=list)
    required_statuses: list[str] = field(default_factory=list)
    required_grouping: list[str] = field(default_factory=list)
    required_ordering: list[str] = field(default_factory=list)
    required_limit: int | None = None
    comparison_cardinality: int | None = None


@dataclass
class SemanticQueryPlanInput:
    original_question: str
    normalized_question: str
    operation: str
    intent_category: str
    sub_operations: list[str] = field(default_factory=list)

    # Legacy scalar lists remain stable for the existing prompt and retriever.
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    aggregations: list[str] = field(default_factory=list)
    sort_fields: list[str] = field(default_factory=list)
    sort_direction: str | None = None
    limit: int | None = None
    date_constraints: list[dict] = field(default_factory=list)
    status_constraints: list[dict] = field(default_factory=list)
    candidate_entity_mentions: list[str] = field(default_factory=list)
    requested_output: list[str] = field(default_factory=list)
    confidence: float = 0.0

    requested_outputs: list[SemanticTerm] = field(default_factory=list)
    metric_terms: list[SemanticTerm] = field(default_factory=list)
    dimension_terms: list[SemanticTerm] = field(default_factory=list)
    aggregation_specs: list[AggregationSpec] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    numeric_constraints: list[NumericConstraint] = field(default_factory=list)
    ordering: list[OrderingSpec] = field(default_factory=list)
    offset: int | None = None
    comparison_entities: list[str] = field(default_factory=list)
    grouping_hints: list[str] = field(default_factory=list)
    relationship_hints: list[RelationshipHint] = field(default_factory=list)
    ambiguity_flags: list[str] = field(default_factory=list)
    clarification_required: bool = False
    clarification_reason: str | None = None
    parser_confidence: float = 0.0
    parser_reasons: list[str] = field(default_factory=list)
    question_contract: QuestionContract = field(default_factory=QuestionContract)
    fallback_used: bool = False

    def to_dict(self):
        payload = asdict(self)
        payload["version"] = SEMANTIC_PARSER_VERSION
        return payload


# New name for callers that want the compiler-style API. The old name remains
# available because it is already used by the running chat pipeline.
ParsedQuestion = SemanticQueryPlanInput


class SemanticParser:
    """Schema-independent natural-language compiler front-end."""

    METRIC_ALIASES = {
        "purchase_value": (
            "purchase value", "purchase amount", "order value", "order amount",
            "ordered value", "po value", "po total", "purchase order value",
            "purchase order total", "procurement value", "procurement spend",
            "purchase spend", "vendor spend", "supplier spend",
            "vendor purchase value", "supplier purchase value",
            "buying value", "buying cost", "purchases",
        ),
        "billing": (
            "billing", "billed amount", "billed value", "bill amount",
            "bill value", "billing value", "client billing", "gross billing",
            "invoice amount", "invoice value", "invoicing", "revenue booked",
            "bills raised", "raised bill value", "sales billing",
            "customer billing",
        ),
        "project_value": (
            "project value", "project amount", "contract value",
            "contract amount", "agreement value", "project worth",
            "project size", "site value", "work value", "work order value",
            "value of project", "estimated project value",
            "project commercial value",
        ),
        "revenue": ("revenue", "sales value", "turnover", "income"),
        "collection": ("collection", "collected amount", "receipt amount", "payments received"),
        "outstanding_amount": ("outstanding amount", "outstanding balance", "overdue balance", "balance amount", "working capital"),
        "payment": ("payment", "payments", "paid amount"),
        "quantity": ("quantity", "qty", "units"),
        "ordered_quantity": ("ordered quantity", "requested quantity"),
        "received_quantity": ("received quantity", "receipt quantity"),
        "material_consumption": (
            "material consumption", "material usage", "materials used",
            "materials consumed", "material used", "material issued",
            "material issue", "material outflow", "site consumption",
            "store issue quantity", "issued material quantity",
            "usage quantity", "consumption quantity", "consumption",
        ),
        "material_issues": ("material issues", "materials issued", "issue quantity", "issued quantity"),
        "material_returns": ("material returns", "materials returned", "return quantity", "returned quantity"),
        "material_receipts": ("material receipts", "materials received", "receipt quantity", "received quantity"),
        "stock": (
            "stock", "closing stock", "current stock", "available stock",
            "stock quantity", "available inventory quantity",
            "available inventory", "available material", "available quantity",
            "inventory balance", "inventory quantity", "stock balance",
            "material balance", "stock on hand", "on hand quantity",
            "stocked",
        ),
        "cost": ("cost", "expense", "expenses", "labour cost", "payroll cost", "overtime cost"),
        "approved_value": ("approved value", "approved amount", "approved budget"),
        "budget": ("budget", "budgeted amount"),
        "profit_loss": ("profit", "loss", "profit margin", "margin", "cost overrun"),
        "headcount": ("employee count", "headcount", "staff count", "workforce", "team size"),
        "overtime": ("overtime", "overtime hours"),
        "salary": ("salary", "wages", "payroll amount"),
        "attendance": ("attendance", "absenteeism", "absence"),
        "completion": ("completion", "completion percentage", "planned completion", "actual completion", "progress"),
        "duration": ("duration", "approval time", "conversion time", "downtime", "ageing", "age"),
        "rate": ("rate", "price", "pricing", "rejection rate", "failure rate", "turnover rate"),
        "amount_value": ("value", "amount", "total value", "gross value", "grand total", "final value", "final price", "net amount"),
        "performance": ("performance", "project performance", "employee performance", "vendor performance", "financial performance"),
        "variance": ("variance", "price variance", "consumption variance"),
        "quality_failures": ("quality failure", "quality failures", "rejection count", "defect count"),
        "tax": ("tax", "tax amount"),
        "target": ("target", "targets"),
        "grn_value": ("grn value", "goods receipt value"),
        "grn_shortage": ("grn shortage", "goods receipt shortage", "shortage"),
        "boq_total": ("boq total", "boq total amount"),
        "boq_raising": ("boq raising", "boq raising amount"),
        "purchase_order_subtotal": ("purchase order subtotal", "po subtotal"),
        "requested_quantity": (
            "requested quantity", "requested qty", "requested material",
            "material requested", "material request quantity",
            "indent quantity", "demand quantity",
        ),
        "inventory_quantity": ("inventory quantity", "inventory transaction quantity"),
        "workorder_value": ("workorder value", "work order value", "after-tax total"),
        "workorder_total": ("subcontract grand total", "workorder grand total"),
        "employee_ctc": ("employee ctc", "staff ctc"),
        "material_issue_value": (
            "material issue value", "issued material value",
            "material consumption value", "material usage value",
            "material used value", "store issue value", "site material value",
            "issue amount", "material cost", "material value",
        ),
    }
    DIMENSION_ALIASES = {
        "project": ("project", "projects", "job", "jobs"),
        "site": ("site", "sites", "location", "locations"),
        "vendor": ("vendor", "vendors", "supplier", "suppliers", "contractor", "contractors"),
        "client": ("client", "clients", "customer", "customers", "account", "accounts"),
        "employee": ("employee", "employees", "staff", "worker", "workers"),
        "department": ("department", "departments", "dept", "team", "teams", "division", "business unit"),
        "manager": ("manager", "managers", "project manager", "project managers"),
        "sales_representative": ("sales representative", "sales representatives", "sales rep", "sales reps"),
        "material": ("material", "materials", "product", "products", "item", "items", "raw material"),
        "purchase_order": ("purchase order", "purchase orders", "order", "orders", "po", "purchase request", "purchase requests"),
        "purchase_order_item": ("purchase order item", "purchase order items", "po item", "po items", "order item", "order items"),
        "invoice": ("invoice", "invoices", "bill", "bills"),
        "payment": ("payment", "payments"),
        "inventory": ("inventory", "inventory transaction", "inventory transactions", "stock transaction", "stock transactions"),
        "store": ("store", "stores", "warehouse", "warehouses"),
        "machine": ("machine", "machines", "asset", "assets"),
        "activity": ("activity", "activities", "milestone", "milestones"),
        "approval": ("approval", "approvals", "workflow", "workflows"),
        "contract": ("contract", "contracts"),
        "timesheet": ("timesheet", "timesheets"),
        "payroll": ("payroll", "payrolls"),
        "production_order": ("production order", "production orders"),
        "quality": ("quality", "quality check", "quality checks"),
        "goods_receipt": ("grn", "goods receipt", "goods receipt note"),
        "boq_billing_line": ("boq", "boq billing line", "boq billing lines"),
        "subcontract_workorder": ("workorder", "workorders", "work order", "work orders", "subcontract workorder", "subcontract workorders"),
        "vendor_branch": ("vendor branch", "vendor branches"),
        "vendor_material": ("vendor material", "vendor materials", "vendor material mapping", "vendor material mappings"),
        "attendance_punch": ("punch record", "punch records", "attendance punch", "attendance punches"),
        "attendance_record": ("attendance record", "attendance records"),
        "material_issue_line": ("material issue line", "material issue lines"),
        "store_issue": ("store issue", "store issues"),
        "region": ("region", "regions", "territory", "territories"),
        "location": ("location", "locations", "state", "states", "city", "cities", "country", "countries"),
        "category": ("category", "categories", "designation", "designations", "position", "positions", "region", "regions"),
        "status": ("status", "statuses", "stage", "stages"),
        "type": ("type", "types", "flag", "flags"),
        "date": ("date", "day", "week", "month", "quarter", "year"),
        "database": ("database", "schema", "table", "tables", "column", "columns", "module", "modules", "business area", "business areas"),
    }
    STATUS_WORDS = {
        "active", "inactive", "open", "closed", "pending", "approved", "rejected",
        "delayed", "late", "overdue", "paid", "unpaid", "completed", "incomplete",
        "cancelled", "canceled", "damaged", "missing", "posted", "unposted",
        "received", "issued", "returned", "expired", "expiring", "blocked",
        "negative", "available", "unavailable", "awaiting",
    }
    GROUPING_ALIASES = {
        "day": ("daily", "day-wise", "by day", "per day"),
        "week": ("weekly", "week-wise", "by week", "per week"),
        "month": ("monthly", "month-wise", "by month", "per month"),
        "quarter": ("quarterly", "quarter-wise", "by quarter", "per quarter"),
        "year": ("yearly", "annual", "year-wise", "by year", "per year"),
    }
    OPERATION_TYPES = {
        "lookup", "relationship_lookup", "count", "distinct_count", "sum", "average",
        "minimum", "maximum", "aggregation", "ranking", "comparison", "trend", "change",
        "growth", "distribution", "breakdown", "existence_check", "exception_detection",
        "threshold_check", "status_summary", "ageing", "overdue", "top_n", "bottom_n",
        "metadata", "schema_question", "business_insight", "missing_kpi", "unknown",
    }
    QUESTION_WORDS = {
        "what", "which", "who", "where", "when", "how", "show", "list", "give",
        "tell", "compare", "find", "display", "get", "are", "is", "the", "a", "an",
    }
    BUSINESS_ONLY_WORDS = {
        "value", "amount", "total", "average", "highest", "lowest", "maximum", "minimum",
        "top", "bottom", "first", "last", "current", "performance", "trend", "count",
        "quantity", "status", "wise", "records", "details", "data", "database", "table",
        "tables", "module", "modules", "month", "quarter", "year", "days", "day",
        "manager", "management", "review", "demo", "notice", "simple", "crisp",
        "quick", "check", "explain",
    }
    GENERIC_ENTITY_PHRASES = {
        "it", "that", "this", "these", "those", "this database", "the database",
        "database", "the system", "system", "the right place", "right place",
        "anything useful", "something useful", "technical table names",
        "any", "any one", "any project", "any site",
        "demo", "for demo", "management review", "for management review",
        "quick vendor check", "manager should notice", "what a manager should notice",
        "keep it simple", "keep the answer crisp", "system check",
    }
    NON_ENTITY_BUSINESS_ACRONYMS = {
        "boq",
        "crm",
        "ctc",
        "erp",
        "grn",
        "hr",
        "kpi",
        "mcp",
        "po",
        "sql",
        "uom",
    }

    def __init__(self, llm_fallback: Callable[[str, dict], str | dict] | None = None):
        self.llm_fallback = llm_fallback

    def parse(self, question: str, *, allow_llm_fallback: bool = True) -> SemanticQueryPlanInput:
        original = str(question or "").strip()
        normalized = self._normalize_question(original)
        metric_matches = self._matches_with_raw(normalized, self.METRIC_ALIASES)
        dimension_matches = self._matches_with_raw(normalized, self.DIMENSION_ALIASES)
        metric_matches = self._contextual_metrics(normalized, metric_matches, dimension_matches)
        dimension_matches = self._contextual_dimensions(
            normalized,
            metric_matches,
            dimension_matches,
        )
        metrics = [canonical for canonical, _raw in metric_matches]
        dimensions = [canonical for canonical, _raw in dimension_matches]
        if re.search(r"\bmanager\s+should\s+notice\b|\bfor\s+(?:a\s+)?manager\b|\bmanagement\s+review\b", normalized):
            dimensions = [dimension for dimension in dimensions if dimension != "manager"]

        limit, offset, sort_direction = self._limit_order(normalized)
        dates = self._date_constraints(original)
        statuses = self._status_words(normalized, metric_matches)
        if "stock" in {canonical for canonical, _raw in metric_matches} or re.search(
            r"\bavailable\s+inventory\s+quantity\b|\bcurrent\s+inventory\s+quantity\b",
            normalized,
        ):
            statuses = [
                status for status in statuses
                if status not in {"negative", "available", "unavailable"}
            ]
        numeric_constraints = self._numeric_constraints(normalized, metrics, dimensions)
        grouping = self._grouping_hints(normalized, dimensions)
        if grouping and not metrics and not any(item in {"count", "distinct_count"} for item in self._aggregations(normalized, metrics)[0]):
            if re.search(r"\bhow many\b.*\b(?:by|per|each)\b", normalized):
                sort_direction = sort_direction or "desc"
            elif re.search(r"\b(?:show|list|give|display)\b", normalized):
                sort_direction = sort_direction or "desc"
                limit = limit or 20
        if sort_direction and metrics:
            ranked_dimension = next((item for item in dimensions if item not in {"date", "material"}), None)
            if ranked_dimension and ranked_dimension not in grouping:
                grouping.append(ranked_dimension)
        aggregations, aggregation_specs = self._aggregations(normalized, metrics)
        grouped_listing = bool(grouping and re.search(r"\b(?:show|list|give|display)\b", normalized))
        event_count_grouping = grouped_listing and (
            not metrics
            or set(metrics) <= {"material_issues"}
        ) and any(
            re.search(pattern, normalized)
            for pattern in (
                r"\bissues?\b",
                r"\bfailures?\b",
                r"\bexceptions?\b",
                r"\bapprovals?\b",
                r"\btransactions?\b",
            )
        )
        if grouping and (not metrics or event_count_grouping) and "count" not in aggregations:
            aggregations.insert(0, "count")
            aggregation_specs.insert(0, AggregationSpec(function="count"))
        if grouping and "count" in aggregations and not metrics and re.search(r"\bhow many\b.*\b(?:by|per|each)\b", normalized):
            sort_direction = sort_direction or "desc"
        mentions = self._candidate_mentions(original)
        filters = self._filters(original, normalized, statuses, mentions)
        ordering = self._ordering(normalized, metrics, sort_direction)
        issue_record_question = bool(
            re.search(r"\b(?:show|list|display)\s+material\s+issues?\s+in\b", normalized)
            and not re.search(r"\b(?:how much|quantity|qty|value|cost|price|pricing|rate)\b", normalized)
        )
        if issue_record_question:
            metrics = ["material_issues"]
            dimensions = list(dict.fromkeys([
                *(item for item in dimensions if item != "material"),
                "store_issue",
            ]))
            aggregations = ["count"]
            aggregation_specs = [AggregationSpec(function="count")]
            if dates and "month" not in grouping:
                grouping.append("month")
            ordering = []
        issued_pricing_question = bool(
            re.search(r"\bmaterials?\s+issued\s+with\s+pricing\b", normalized)
        )
        if issued_pricing_question:
            metrics = list(dict.fromkeys([
                "material_issues",
                "rate",
                *metrics,
            ]))
            dimensions = list(dict.fromkeys([*dimensions, "project", "material"]))
            aggregations = ["sum"]
            aggregation_specs = [AggregationSpec(function="sum")]
            sort_direction = sort_direction or "desc"
            limit = limit or 10
            ordering = [OrderingSpec(target_concept="material_issues", direction=sort_direction)]
        operation, sub_operations = self._operations(
            normalized,
            metrics=metrics,
            dimensions=dimensions,
            aggregations=aggregations,
            grouping=grouping,
            dates=dates,
            statuses=statuses,
            limit=limit,
            sort_direction=sort_direction,
            numeric_constraints=numeric_constraints,
            mentions=mentions,
        )
        requested_output = self._requested_output(normalized, dimensions, operation)
        comparison_entities = mentions[:] if "comparison" in {operation, *sub_operations} else []
        relationship_hints = self._relationship_hints(normalized, dimensions, mentions)
        ambiguity_flags, clarification_required, clarification_reason = self._ambiguity(
            normalized, operation, metrics, dimensions, mentions
        )
        confidence, reasons = self._confidence(
            operation, metrics, dimensions, mentions, dates, statuses, ambiguity_flags
        )

        plan = SemanticQueryPlanInput(
            original_question=original,
            normalized_question=normalized,
            operation=operation,
            intent_category=self._intent_category(operation),
            sub_operations=sub_operations,
            metrics=metrics,
            dimensions=dimensions,
            aggregations=aggregations,
            sort_fields=metrics[:1],
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
            date_constraints=dates,
            status_constraints=[{"type": "status", "values": statuses}] if statuses else [],
            candidate_entity_mentions=mentions,
            requested_output=requested_output,
            confidence=confidence,
            requested_outputs=self._semantic_terms(requested_output, "output"),
            metric_terms=[
                SemanticTerm(raw_text=raw, normalized_text=canonical, role="metric", confidence=0.92)
                for canonical, raw in metric_matches
            ],
            dimension_terms=[
                SemanticTerm(raw_text=raw, normalized_text=canonical, role="dimension", confidence=0.92)
                for canonical, raw in dimension_matches
            ],
            aggregation_specs=aggregation_specs,
            filters=filters,
            numeric_constraints=numeric_constraints,
            ordering=ordering,
            comparison_entities=comparison_entities,
            grouping_hints=grouping,
            relationship_hints=relationship_hints,
            ambiguity_flags=ambiguity_flags,
            clarification_required=clarification_required,
            clarification_reason=clarification_reason,
            parser_confidence=confidence,
            parser_reasons=reasons,
        )
        plan.question_contract = self._question_contract(plan)

        if allow_llm_fallback and self.llm_fallback and self._needs_fallback(plan):
            plan = self._apply_llm_fallback(plan)
        return plan

    def _contextual_dimensions(self, normalized, metric_matches, dimension_matches):
        metrics = {canonical for canonical, _raw in metric_matches}
        if "project_value" in metrics:
            dimension_matches = [
                (canonical, raw)
                for canonical, raw in dimension_matches
                if canonical != "contract"
            ]
        if re.search(r"\b(?:purchase\s+order|po|order)\s+items?\b", normalized):
            filtered = [
                (canonical, raw)
                for canonical, raw in dimension_matches
                if canonical not in {"purchase_order", "material"}
            ]
            return [("purchase_order_item", "purchase order items"), *filtered]
        if "billing" not in metrics:
            return dimension_matches
        explicit_invoice_output = bool(re.search(
            r"\b(?:list|show|which|what|count|how many)\s+(?:the\s+)?(?:invoice|invoices|bill|bills)\b",
            normalized,
        ))
        if explicit_invoice_output:
            return dimension_matches
        return [
            (canonical, raw)
            for canonical, raw in dimension_matches
            if canonical != "invoice"
        ]

    def _normalize_question(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        normalized = normalized.translate(str.maketrans({
            "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u2013": "-", "\u2014": "-", "\u2212": "-",
        }))
        normalized = re.sub(r"(?<=\d),(?=\d{3}\b)", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip().lower()
        return normalized

    def _matches_with_raw(self, normalized: str, aliases: dict) -> list[tuple[str, str]]:
        matches = []
        for canonical, values in aliases.items():
            found = None
            position = len(normalized) + 1
            for value in sorted(values, key=len, reverse=True):
                match = re.search(rf"\b{re.escape(value)}(?:s)?\b", normalized)
                if match:
                    found = match.group(0)
                    position = match.start()
                    break
            if found:
                matches.append((position, canonical, found))
        return [(canonical, found) for _position, canonical, found in sorted(matches)]

    def _contextual_metrics(self, normalized: str, metric_matches, dimension_matches):
        if re.search(
            r"\bhow many\b|\bnumber of\b|\bcount\b|"
            r"\b(?:is|are) there any\b|\bexists?\b",
            normalized,
        ):
            dimension_phrases = {
                raw
                for _canonical, raw in dimension_matches
            }
            metric_matches = [
                (canonical, raw)
                for canonical, raw in metric_matches
                if not any(
                    raw != dimension_phrase
                    and raw in dimension_phrase
                    for dimension_phrase in dimension_phrases
                )
            ]
        existing = {canonical for canonical, _raw in metric_matches}
        if "workorder_value" in existing:
            metric_matches = [
                (canonical, raw)
                for canonical, raw in metric_matches
                if canonical != "tax"
            ]
            existing.discard("tax")
        if "amount_value" in existing and len(existing - {"amount_value"}) > 0:
            metric_matches = [
                (canonical, raw)
                for canonical, raw in metric_matches
                if canonical != "amount_value"
            ]
            existing.discard("amount_value")
        if "billing" in existing and "revenue" in existing:
            metric_matches = [
                (canonical, raw)
                for canonical, raw in metric_matches
                if canonical != "revenue"
            ]
            existing.discard("revenue")
        dimensions = {canonical for canonical, _raw in dimension_matches}
        inventory_context = bool(dimensions & {"store", "material"}) or "stock" in existing
        additions = []
        if inventory_context:
            for canonical, pattern in (
                ("material_receipts", r"\breceipts?\b|\breceived\b"),
                ("material_issues", r"\bissues?\b|\bissued\b"),
                ("material_returns", r"\breturns?\b|\breturned\b"),
            ):
                match = re.search(pattern, normalized)
                if match and canonical not in existing:
                    additions.append((match.start(), canonical, match.group(0)))
        positioned = [(normalized.find(raw), canonical, raw) for canonical, raw in metric_matches]
        positioned.extend(additions)
        return [(canonical, raw) for _position, canonical, raw in sorted(positioned)]

    def _aggregations(self, normalized: str, metrics: list[str]):
        functions = []
        if re.search(r"\bhow many\b|\bnumber of\b|\bcount\b|\b(?:is|are) there any\b|\bexists?\b", normalized):
            functions.append("count")
        if metrics and re.search(r"\bhow much\b", normalized):
            functions.append("sum")
        if re.search(r"\bdistinct\b|\bunique\b", normalized) and "count" in functions:
            functions = ["distinct_count"]
        if re.search(r"\btotal\b|\bsum\b|\bhighest\b|\blowest\b|\btop\b|\bbottom\b|\bmost\b|\bleast\b", normalized):
            functions.append("sum")
        if re.search(r"\baverage\b|\bavg\b|\bmean\b", normalized):
            functions.append("avg")
        if re.search(r"\bmaximum\b|\bmax\b", normalized):
            functions.append("max")
        if re.search(r"\bminimum\b|\bmin\b", normalized):
            functions.append("min")
        functions = list(dict.fromkeys(functions))
        target = metrics[0] if metrics else None
        specs = [
            AggregationSpec(
                function={"distinct_count": "count", "avg": "average"}.get(function, function),
                target=target,
                distinct=function == "distinct_count",
            )
            for function in functions
        ]
        return functions, specs

    def _limit_order(self, normalized: str):
        limit = None
        offset = None
        number_word = r"\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten"
        match = re.search(
            rf"\b(?:top|bottom|first|last|limit)\s+({number_word})\b"
            r"(?!\s+(?:days?|weeks?|months?|quarters?|years?))",
            normalized,
        )
        if not match:
            match = re.search(
                rf"\b(?:show|list|name|give(?:\s+me)?|tell(?:\s+me)?)\s+"
                rf"(?:any\s+|latest\s+|recent\s+|newest\s+)?({number_word})\b"
                r"(?!\s+(?:days?|weeks?|months?|quarters?|years?))",
                normalized,
            )
        if not match:
            match = re.search(
                rf"\b(?:latest|recent|newest)\s+({number_word})\b"
                r"(?!\s+(?:days?|weeks?|months?|quarters?|years?))",
                normalized,
            )
        if not match:
            match = re.search(
                rf"\bany\s+({number_word})\b"
                r"(?!\s+(?:days?|weeks?|months?|quarters?|years?))",
                normalized,
            )
        if match:
            limit = self._number(match.group(1))
        elif re.search(r"\b(highest|lowest|maximum|minimum|most|least|largest|smallest|best|worst)\b", normalized):
            limit = 1
        offset_match = re.search(r"\b(?:skip|offset)\s+(\d{1,6})\b", normalized)
        if offset_match:
            offset = int(offset_match.group(1))
        direction = None
        if re.search(r"\b(top|highest|maximum|max|most|largest|biggest|best)\b", normalized):
            direction = "desc"
        elif re.search(r"\b(bottom|lowest|minimum|min|least|smallest|worst)\b", normalized):
            direction = "asc"
        return limit, offset, direction

    def _date_constraints(self, original: str) -> list[dict]:
        text = self._normalize_question(original)
        constraints = []
        consumed = []
        today = date.today()

        financial_year = re.search(
            r"\b(?:financial\s+year|fy)\s+"
            r"((?:19|20)\d{2})\s*[-/]\s*(\d{2}|(?:19|20)\d{2})\b",
            text,
        )
        if financial_year:
            start_year = int(financial_year.group(1))
            end_token = financial_year.group(2)
            end_year = (
                int(end_token)
                if len(end_token) == 4
                else (start_year // 100) * 100 + int(end_token)
            )
            if end_year <= start_year:
                end_year += 100
            constraints.append(
                {
                    "type": "financial_year",
                    "value": f"{start_year}-{str(end_year)[-2:]}",
                    "start": f"{start_year}-04-01",
                    "end": f"{end_year}-03-31",
                }
            )
            consumed.append(financial_year.span())

        explicit = re.finditer(
            r"\b(?:between|from)\s+(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\s+(?:and|to|through|until)\s+(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
            text,
        )
        for match in explicit:
            constraints.append({"type": "between", "start": match.group(1), "end": match.group(2)})
            consumed.append(match.span())

        month_names = "january|february|march|april|may|june|july|august|september|october|november|december"
        month_range = re.search(
            rf"\b(?:from\s+)?({month_names})\s+(?:to|through|until|-)\s+({month_names})\s+(19\d{{2}}|20\d{{2}})\b",
            text,
        )
        if month_range:
            start_month = self._month_number(month_range.group(1))
            end_month = self._month_number(month_range.group(2))
            year = int(month_range.group(3))
            constraints.append({
                "type": "month_range",
                "start": date(year, start_month, 1).isoformat(),
                "end": date(year, end_month, calendar.monthrange(year, end_month)[1]).isoformat(),
                "start_month": month_range.group(1),
                "end_month": month_range.group(2),
                "year": year,
            })
            consumed.append(month_range.span())

        for match in re.finditer(r"\b(?:last|past)\s+(\d{1,4})\s+days?\b", text):
            days = int(match.group(1))
            constraints.append({"type": "last_days", "value": days, "days": days})
            consumed.append(match.span())

        for match in re.finditer(r"\b(?:last|past)\s+(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+months?\b", text):
            months = self._number(match.group(1))
            constraints.append({"type": "last_months", "value": months, "months": months})
            consumed.append(match.span())

        for match in re.finditer(r"\b(?:for|during)\s+(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(days?|weeks?|months?|years?)\b", text):
            amount = self._number(match.group(1))
            constraints.append({"type": "duration", "value": amount, "unit": match.group(2)})
            consumed.append(match.span())

        for phrase, kind in (("current financial year", "current_financial_year"), ("this financial year", "current_financial_year"), ("current fy", "current_financial_year"), ("this fy", "current_financial_year")):
            match = re.search(rf"\b{re.escape(phrase)}\b", text)
            if match:
                constraints.append({"type": kind})
                consumed.append(match.span())
                break

        relative_ranges = {
            "this month": self._month_bounds(today.year, today.month, "current_month"),
            "current month": self._month_bounds(today.year, today.month, "current_month"),
            "last month": self._previous_month(today),
            "this quarter": self._quarter_bounds(today, 0),
            "current quarter": self._quarter_bounds(today, 0),
            "last quarter": self._quarter_bounds(today, -1),
            "this year": {"type": "current_year", "start": f"{today.year}-01-01", "end": f"{today.year}-12-31"},
            "last year": {"type": "last_year", "start": f"{today.year - 1}-01-01", "end": f"{today.year - 1}-12-31"},
            "today": {"type": "day", "start": today.isoformat(), "end": today.isoformat()},
            "this week": {"type": "current_week", "start": (today - timedelta(days=today.weekday())).isoformat(), "end": (today + timedelta(days=6 - today.weekday())).isoformat()},
        }
        for phrase, value in relative_ranges.items():
            match = re.search(rf"\b{re.escape(phrase)}\b", text)
            if match and not self._overlaps(match.span(), consumed):
                constraints.append(value)
                consumed.append(match.span())

        single_month = re.search(rf"\b({month_names})\s+(19\d{{2}}|20\d{{2}})\b", text)
        if single_month and not self._overlaps(single_month.span(), consumed):
            month = self._month_number(single_month.group(1))
            year = int(single_month.group(2))
            constraints.append({
                "type": "month", "value": single_month.group(1), "year": year,
                "start": date(year, month, 1).isoformat(),
                "end": date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
            })
            consumed.append(single_month.span())

        if financial_year:
            return constraints
        for match in re.finditer(r"\b(19\d{2}|20\d{2})\b", text):
            if self._overlaps(match.span(), consumed):
                continue
            year = int(match.group(1))
            constraints.append({"type": "year", "value": year, "start": f"{year}-01-01", "end": f"{year}-12-31"})
        return constraints

    def _status_words(self, normalized: str, metric_matches: list[tuple[str, str]]) -> list[str]:
        metric_spans = []
        for _canonical, raw in metric_matches:
            metric_spans.extend(match.span() for match in re.finditer(rf"\b{re.escape(raw)}\b", normalized))
        found = []
        for match in re.finditer(r"[a-z]+", normalized):
            token = match.group(0)
            if token in {"available", "unavailable"} and self._availability_is_existence_word(normalized):
                continue
            if token == "available" and re.search(r"\bavailable\b.{0,40}\b(?:the\s+)?database\b", normalized):
                continue
            consumed_as_metric = token in {"approved", "received", "issued", "returned", "paid"} and self._overlaps(match.span(), metric_spans)
            if token in self.STATUS_WORDS and token not in found and not consumed_as_metric:
                found.append(token)
        return found

    def _availability_is_existence_word(self, normalized: str) -> bool:
        return bool(
            re.search(r"\b(?:how many|count|number of|list|show|are there any|do we have)\b", normalized)
            or re.search(
                r"\b(?:records?|data|tables?|columns?|orders?|projects?|vendors?|customers?|employees?|materials?|transactions?)\s+(?:are\s+)?available\b",
                normalized,
            )
        )

    def _numeric_constraints(self, normalized: str, metrics: list[str], dimensions: list[str]):
        constraints = []
        patterns = (
            (r"\b(?:more than|greater than|above|over|older than)\s+([\d,.]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(%|percent|days?|hours?|months?|years?)?", ">"),
            (r"\b(?:less than|below|under)\s+([\d,.]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(%|percent|days?|hours?|months?|years?)?", "<"),
            (r"\b(?:at least|minimum of)\s+([\d,.]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(%|percent|days?|hours?|months?|years?)?", ">="),
            (r"\b(?:at most|maximum of)\s+([\d,.]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(%|percent|days?|hours?|months?|years?)?", "<="),
        )
        target = metrics[0] if metrics else dimensions[0] if dimensions else None
        for pattern, operator in patterns:
            for match in re.finditer(pattern, normalized):
                unit = match.group(2)
                tail = normalized[match.end():match.end() + 50]
                nearby_dimension = next(
                    (
                        dimension
                        for dimension in dimensions
                        if any(re.search(rf"\b{re.escape(alias)}s?\b", tail) for alias in self.DIMENSION_ALIASES.get(dimension, (dimension,)))
                    ),
                    None,
                )
                target = nearby_dimension or target
                field = "age" if unit and unit.startswith(("day", "month", "year")) and ("older" in match.group(0) or "old" in normalized[max(0, match.start() - 12):match.start()]) else target
                raw_value = match.group(1)
                value = float(raw_value.replace(",", "")) if re.match(r"\d", raw_value) else float(self._number(raw_value))
                constraints.append(NumericConstraint(field, operator, value, unit))
        return constraints

    def _grouping_hints(self, normalized: str, dimensions: list[str]) -> list[str]:
        grouping = []
        for canonical, aliases in self.GROUPING_ALIASES.items():
            if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases):
                grouping.append(canonical)
        for dimension in dimensions:
            aliases = self.DIMENSION_ALIASES.get(dimension, (dimension,))
            type_grouping = dimension == "type" and re.search(
                r"\bby\s+(?:transaction|movement|issue|order|record|workorder)\s+type\b|\bby\s+[\w/ -]+?\s+flags?\b",
                normalized,
            )
            if type_grouping or any(re.search(rf"\b(?:by|per|each)\s+{re.escape(alias)}s?\b|\b{re.escape(alias)}-wise\b", normalized) for alias in aliases):
                if any(re.search(rf"\bby\s+{re.escape(metric_alias)}\b", normalized) for metric_aliases in self.METRIC_ALIASES.values() for metric_alias in metric_aliases):
                    continue
                grouping.append(dimension)
        return list(dict.fromkeys(grouping))

    def _ordering(self, normalized: str, metrics: list[str], direction: str | None):
        if not direction:
            return []
        target = metrics[0] if metrics else None
        return [OrderingSpec(target_concept=target, direction=direction)]

    def _operations(self, normalized: str, **context):
        operations = []
        if re.search(r"\bwhat (?:is|are) (?:this|the) database about\b|\bdescribe (?:this|the) database\b|\bwhat (?:business areas|modules|data) (?:are|is) (?:available|covered)\b", normalized):
            operations.append("metadata")
        if re.search(r"\bwhich tables?\b|\bwhich columns?\b|\bwhere is .+ stored\b|\bmain relationships?\b|\bschema\b", normalized):
            operations.append("schema_question")
        if re.search(r"\bcompare\b|\bversus\b|\bvs\.?\b|\bplanned (?:and|versus|vs) actual\b", normalized):
            operations.append("comparison")
        explicit_ranking = re.search(r"\b(?:top|bottom|highest|lowest|maximum|minimum|most|least|largest|smallest|best|worst)\b", normalized)
        default_distribution_order = bool(
            context["grouping"]
            and re.search(r"\b(?:show|list|give|display)\b|\bhow many\b.*\b(?:by|per|each)\b", normalized)
            and not explicit_ranking
        )
        if context["sort_direction"] and not default_distribution_order:
            operations.append("ranking")
            operations.append("top_n" if context["sort_direction"] == "desc" else "bottom_n")
        if (
            context.get("limit")
            and context["metrics"]
            and context["dimensions"]
            and re.search(r"\b(?:show|list|give|display|name)\b", normalized)
        ):
            operations.append("ranking")
        if re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", normalized):
            operations.append("distinct_count" if re.search(r"\bdistinct\b|\bunique\b", normalized) else "count")
        if re.search(r"\btrend\b|\bover time\b|\bmonthly\b|\bweekly\b|\byearly\b", normalized):
            operations.append("trend")
        if re.search(r"\bgrowth\b|\bgrew\b|\bincrease(?:d)?\b", normalized):
            operations.append("growth")
        if re.search(r"\bwhat changed\b|\bchange in\b|\bchanged significantly\b", normalized):
            operations.append("change")
        if re.search(r"\bbreakdown\b|\bdistribution\b|\bby\s+(?:department|site|project|vendor|supplier|client|customer|category|region|warehouse|month|year)\b|\b\w+-wise\b", normalized):
            operations.append("breakdown")
        if re.search(r"\b(is there|are there|do any|does any|exist(?:s)?)\b", normalized) and not re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", normalized):
            operations.append("existence_check")
        if re.search(r"\b(exception|anomal|unusual|risk|immediate attention|mismatch|missing|negative)\b", normalized):
            operations.append("exception_detection")
        if context["numeric_constraints"] or re.search(r"\b(?:below|above|under|over)\s+(?:reorder|safety|target|budget|approved)\b", normalized):
            operations.append("threshold_check")
        if re.search(r"\bageing\b|\baging\b|\bolder than\b", normalized):
            operations.append("ageing")
        if "overdue" in context["statuses"]:
            operations.append("overdue")
        if context["statuses"]:
            operations.append("status_summary")
        if re.search(r"\binsights?\b|\bopportunit|\bperformance overview\b|\boperational risks?\b", normalized):
            operations.append("business_insight")
        if "profit_loss" in context["metrics"] and not re.search(r"\b(?:revenue|income).*(?:cost|expense)|(?:cost|expense).*(?:revenue|income)\b", normalized):
            operations.append("missing_kpi")
        for aggregation in context["aggregations"]:
            mapped = {"avg": "average", "min": "minimum", "max": "maximum", "distinct_count": "distinct_count"}.get(aggregation, aggregation)
            if mapped == "count" and default_distribution_order:
                continue
            operations.append(mapped if mapped in self.OPERATION_TYPES else "aggregation")
        if context["aggregations"]:
            operations.append("aggregation")
        if context["grouping"] and not context["metrics"]:
            operations.append("distribution")
        if context["grouping"] and "breakdown" not in operations:
            operations.append("breakdown")
        operations = list(dict.fromkeys(operations))

        priority = (
            "metadata", "schema_question", "business_insight", "missing_kpi", "comparison",
            "ranking", "trend", "growth", "change", "exception_detection", "ageing",
            "threshold_check", "overdue", "existence_check", "distinct_count", "count", "average", "sum", "maximum",
            "minimum", "distribution", "breakdown", "status_summary",
        )
        primary = next((item for item in priority if item in operations), None)
        if not primary:
            if context["mentions"] and context["dimensions"]:
                primary = "relationship_lookup"
            elif context["metrics"]:
                primary = "aggregation"
            elif context["dimensions"]:
                primary = "lookup"
            else:
                primary = "unknown"
        sub_operations = [item for item in operations if item != primary]
        if context["dates"]:
            sub_operations.append("date_filtering")
        if context["statuses"] or context["numeric_constraints"]:
            sub_operations.append("filtering")
        if context["grouping"]:
            sub_operations.append("grouping")
        return primary, list(dict.fromkeys(sub_operations))

    def _requested_output(self, normalized: str, dimensions: list[str], operation: str):
        if operation == "metadata":
            return ["database_summary"]
        if operation == "schema_question":
            if "column" in normalized:
                return ["columns"]
            if "relationship" in normalized:
                return ["relationships"]
            return ["tables"]
        if re.search(r"\bdetails?\b|\beverything\b", normalized):
            return ["details", *[item for item in dimensions if item != "date"]]
        return [item for item in dimensions if item not in {"date", "database"}][:6]

    def _candidate_mentions(self, original: str) -> list[str]:
        candidates = []
        between_entities = re.search(r"\bcompare\b.+?\bbetween\s+(.+)$", original, re.I)
        if between_entities:
            for part in re.split(r"\s+(?:and|versus|vs\.?)\s+|,", between_entities.group(1), flags=re.I):
                part = re.split(
                    r"\s+(?:billing|revenue|purchases?|payments?|materials?|invoices?|during|in\s+(?:19|20)\d{2})\b",
                    part,
                    maxsplit=1,
                    flags=re.I,
                )[0]
                self._append_candidate(candidates, part, strong=False)
        for match in re.finditer(r"['\"]([^'\"]{1,100})['\"]", original):
            self._append_candidate(candidates, match.group(1), strong=True)
        typed_pattern = re.compile(
            r"\b(?i:project|site|employee|invoice|vendor|supplier|client|customer|department|material|order|ticket)\s+"
            r"([A-Z0-9][A-Za-z0-9&.'/-]*(?:\s+[A-Z0-9][A-Za-z0-9&.'/-]*){0,4})",
        )
        for match in typed_pattern.finditer(original):
            self._append_candidate(candidates, match.group(0), strong=True)
        for match in re.finditer(r"\bM/s\.?\s+[A-Za-z0-9][A-Za-z0-9 &.'/-]{1,80}", original, re.I):
            self._append_candidate(candidates, match.group(0), strong=True)
        code_pattern = r"\b(?:[A-Z]{2,}[A-Z0-9]*(?:[-/.][A-Z0-9]+)+|[A-Za-z]+[-/.][A-Za-z0-9/-]*\d[A-Za-z0-9/-]*|[A-Z]{2,}\d+[A-Z0-9/-]*)\b|\b[A-Z]{2,}\b"
        for match in re.finditer(code_pattern, original):
            if not re.fullmatch(r"(?:19|20)\d{2}", match.group(0)):
                self._append_candidate(candidates, match.group(0), strong=True)
        for match in re.finditer(r"\b[A-Z][A-Za-z0-9&.'/-]*(?:\s+[A-Z][A-Za-z0-9&.'/-]*){1,6}\b", original):
            self._append_candidate(candidates, match.group(0), strong=False)

        relation_pattern = re.compile(
            r"\b(?:for|at|under|from|named|called|about|in)\s+(.+?)(?=\s+(?:show|list|tell|give|display|during|within|where|whose|with|by|from|between|in\s+(?:the\s+)?(?:last|past|this|current)|last\s+\d+|this\s+(?:month|quarter|year)|current\s+(?:month|quarter|year|financial)|$)|[?.!,;]|$)",
            re.I,
        )
        for match in relation_pattern.finditer(original):
            value = match.group(1).strip()
            for part in re.split(r"\s+(?:and|versus|vs\.?)\s+|,", value, flags=re.I):
                self._append_candidate(candidates, part, strong=False)
        return self._dedupe_mentions(candidates)

    def _append_candidate(self, candidates: list[str], value: str, *, strong: bool):
        cleaned = self._clean_mention(value)
        if cleaned and self._is_entity_candidate(cleaned, strong=strong):
            candidates.append(cleaned)

    def _clean_mention(self, value: str) -> str:
        value = re.sub(r"^[\s,;:.!?]+|[\s,;:.!?]+$", "", str(value or ""))
        value = value.strip("'\"")
        value = re.sub(r"^(?:show|list|give|tell|find|compare)\s+(?:me\s+)?", "", value, flags=re.I)
        value = re.split(
            r"\s+(?:including|with|where|whose|that|which|and\s+anything|so\s+i|not\s+technical)\b",
            value,
            maxsplit=1,
            flags=re.I,
        )[0]
        value = re.sub(r"^(?:supplier|vendor|client|customer|manager)\s+(?=[A-Z0-9])", "", value, flags=re.I)
        value = re.sub(r"\s+(?:project|vendor|client|customer|employee)\s*$", "", value, flags=re.I)
        return re.sub(r"\s+", " ", value).strip(" -'\"")

    def _is_entity_candidate(self, value: str, *, strong: bool) -> bool:
        normalized = self._normalize_question(value)
        if not normalized or re.fullmatch(r"(?:19|20)\d{2}|\d+(?:\.\d+)?%?", normalized):
            return False
        words = set(re.findall(r"[a-z0-9]+", normalized))
        if normalized in self.GENERIC_ENTITY_PHRASES:
            return False
        if re.search(r"\b(?:the\s+)?database\b", normalized) and (
            words & self.BUSINESS_ONLY_WORDS
            or any(alias in normalized for values in self.METRIC_ALIASES.values() for alias in values)
        ):
            return False
        if re.fullmatch(r"(?:the|this|that|these|those)\s+\w+", normalized):
            return False
        if words <= self.QUESTION_WORDS or words <= self.BUSINESS_ONLY_WORDS or words <= self.STATUS_WORDS:
            return False
        if self._looks_like_date_phrase(normalized) and not strong:
            return False
        if re.fullmatch(
            r"(?:any|some|first|latest|recent|newest)?\s*"
            r"(?:\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)?\s*"
            r"(?:materials?|items?|products?|projects?|sites?|vendors?|suppliers?|purchase orders?|pos?|bills?|invoices?)",
            normalized,
        ):
            return False
        semantic_aliases = [
            alias for values in (*self.METRIC_ALIASES.values(), *self.DIMENSION_ALIASES.values()) for alias in values
        ]
        if normalized in semantic_aliases:
            return False
        parts = normalized.split()
        if (
            len(parts) >= 2
            and parts[-1] in self.NON_ENTITY_BUSINESS_ACRONYMS
            and " ".join(parts[:-1]) in semantic_aliases
        ):
            return False
        if re.search(r"\b(top|bottom|highest|lowest|total|average|monthly|weekly|yearly|pending|active|delayed|overdue)\b", normalized):
            return False
        if re.match(r"^(?:each|per|by)\s+", normalized):
            return False
        if any(normalized == f"{status} {alias}" for status in self.STATUS_WORDS for alias in semantic_aliases):
            return False
        if strong:
            return normalized not in self.NON_ENTITY_BUSINESS_ACRONYMS
        return bool(
            re.search(r"\d|[-/.&]", value)
            or re.search(r"\b[A-Z]{2,}\b", value)
            or re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][A-Za-z&.'/-]*)+\b", value)
            or len(words) <= 5
        )

    def _filters(self, original: str, normalized: str, statuses: list[str], mentions: list[str]):
        filters = [FilterSpec("status", "equals", status, status, True, 0.95) for status in statuses]
        concept_threshold = re.search(r"\b(below|above|under|over)\s+([a-z][a-z ]{1,40}?)(?=[?.!,;]|$)", normalized)
        if concept_threshold and not re.search(r"\d", concept_threshold.group(2)):
            operator = "less_than_concept" if concept_threshold.group(1) in {"below", "under"} else "greater_than_concept"
            value = concept_threshold.group(2).strip()
            filters.append(FilterSpec(None, operator, value, value, True, 0.82))
        missing = re.search(r"\b(?:without|with no|no)\s+([a-z][a-z ]{1,35}?)(?=\s+(?:in|for|during|this|last)|[?.!,;]|$)", normalized)
        if missing:
            value = missing.group(1).strip()
            filters.append(FilterSpec(value, "missing", value, None, True, 0.9))
        for mention in mentions:
            if self._normalize_question(mention) in {"any", "all", "any one", "any project", "any site"}:
                continue
            match = re.search(rf"\b(?:in|at|for|under|from)\s+{re.escape(mention)}\b", original, re.I)
            field = None
            typed = re.search(
                rf"\b(project|site|vendor|supplier|client|customer|employee|department|manager|invoice|order)\s+{re.escape(mention)}\b",
                original,
                re.I,
            )
            if typed:
                field = {"supplier": "vendor", "client": "customer", "site": "location", "order": "purchase_order"}.get(
                    typed.group(1).lower(),
                    typed.group(1).lower(),
                )
            prefix = original[max(0, match.start() - 25):match.start()].lower() if match else ""
            if field is None and re.search(r"\b(project|site|vendor|supplier|client|customer|employee|department|manager)\s*$", prefix):
                field = re.search(r"(project|site|vendor|supplier|client|customer|employee|department|manager)\s*$", prefix).group(1)
            elif field is None and match and match.group(0).lower().startswith(("in ", "at ")):
                field = "location"
            filters.append(FilterSpec(field, "equals", mention, mention, True, 0.78))
        return filters

    def _relationship_hints(self, normalized: str, dimensions: list[str], mentions: list[str]):
        if not mentions or not dimensions:
            return []
        relation = next((word for word in ("for", "under", "at", "from", "by", "of") if re.search(rf"\b{word}\b", normalized)), "related to")
        return [RelationshipHint(None, dimension, relation, 0.8) for dimension in dimensions[:3]]

    def _ambiguity(self, normalized: str, operation: str, metrics: list[str], dimensions: list[str], mentions: list[str]):
        flags = []
        if normalized in {"show performance", "show performance.", "how is performance", "show records", "show data"}:
            flags.append("underspecified_business_concept")
        if operation == "unknown":
            flags.append("unknown_operation")
        clarification = "underspecified_business_concept" in flags
        reason = "The requested concept has multiple plausible meanings and no scope was provided." if clarification else None
        return flags, clarification, reason

    def _confidence(self, operation, metrics, dimensions, mentions, dates, statuses, ambiguity_flags):
        score = 0.48
        reasons = []
        if operation != "unknown":
            score += 0.22
            reasons.append(f"recognized operation: {operation}")
        if metrics:
            score += 0.08
            reasons.append("recognized metric concepts")
        if dimensions:
            score += 0.08
            reasons.append("recognized output dimensions")
        if mentions:
            score += 0.05
            reasons.append("preserved plausible entity mentions")
        if dates or statuses:
            score += 0.05
            reasons.append("preserved explicit filters")
        score -= min(0.3, len(ambiguity_flags) * 0.15)
        return round(max(0.1, min(0.98, score)), 2), reasons

    def _question_contract(self, plan: SemanticQueryPlanInput):
        filters = [f"{item.field_concept or 'entity'} {item.operator} {item.raw_value}" for item in plan.filters]
        filters.extend(f"{item.field_concept or 'metric'} {item.operator} {item.value:g}{(' ' + item.unit) if item.unit else ''}" for item in plan.numeric_constraints)
        dates = [self._date_contract_text(item) for item in plan.date_constraints]
        statuses = [status for item in plan.status_constraints for status in item.get("values", [])]
        ordering = [f"{item.target_concept or 'requested metric'} {item.direction}" for item in plan.ordering]
        comparison_cardinality = len(plan.comparison_entities) if plan.comparison_entities else None
        return QuestionContract(
            required_outputs=plan.requested_output[:],
            required_metrics=plan.metrics[:],
            required_dimensions=plan.dimensions[:],
            required_filters=filters,
            required_entity_mentions=plan.candidate_entity_mentions[:],
            required_date_constraints=dates,
            required_statuses=statuses,
            required_grouping=plan.grouping_hints[:],
            required_ordering=ordering,
            required_limit=plan.limit,
            comparison_cardinality=comparison_cardinality,
        )

    def _date_contract_text(self, item: dict):
        if item.get("start") and item.get("end"):
            return f"{item.get('type')}: {item['start']} to {item['end']}"
        return f"{item.get('type')}: {item.get('value', item.get('days', 'specified'))}"

    def _semantic_terms(self, values: list[str], role: str):
        return [SemanticTerm(value, self._normalize_question(value), role, 0.9) for value in values]

    def _intent_category(self, operation: str):
        if operation in {"metadata", "schema_question"}:
            return "metadata"
        if operation in {"business_insight", "comparison", "trend", "growth", "change", "exception_detection"}:
            return "analysis"
        if operation == "unknown":
            return "unknown"
        return "data_query"

    def _needs_fallback(self, plan: SemanticQueryPlanInput):
        return plan.operation == "unknown" or plan.parser_confidence < 0.5

    def _apply_llm_fallback(self, plan: SemanticQueryPlanInput):
        contract = {
            "allowed_operations": sorted(self.OPERATION_TYPES),
            "rules": [
                "Classify semantic roles only.",
                "Do not return SQL, tables, columns, joins, or invented entities.",
                "Preserve all dates, limits, statuses, comparisons, metrics, and grouping.",
            ],
        }
        try:
            raw = self.llm_fallback(plan.original_question, contract)
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(payload, dict):
                return plan
            forbidden = {"sql", "tables", "columns", "joins", "table", "column"}
            if forbidden & set(payload):
                return plan
            operation = payload.get("operation")
            if operation not in self.OPERATION_TYPES:
                return plan
            plan.operation = operation
            plan.sub_operations = [item for item in payload.get("sub_operations", []) if item in self.OPERATION_TYPES]
            plan.fallback_used = True
            plan.parser_reasons.append("validated semantic-only LLM fallback")
            plan.parser_confidence = max(plan.parser_confidence, min(float(payload.get("confidence", 0.6)), 0.8))
            plan.confidence = plan.parser_confidence
            plan.question_contract = self._question_contract(plan)
        except Exception:
            return plan
        return plan

    def _dedupe_mentions(self, mentions: list[str]):
        result = []
        for mention in mentions:
            key = self._normalize_question(mention)
            if any(key == self._normalize_question(existing) for existing in result):
                continue
            if any(key in self._normalize_question(existing) and key != self._normalize_question(existing) for existing in mentions):
                continue
            result.append(mention)
        return result[:10]

    def _looks_like_date_phrase(self, value: str):
        return bool(re.search(
            r"\b(?:the\s+)?(?:last|past|this|current|previous|next)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)?\s*(?:days?|weeks?|months?|quarters?|years?|financial year)\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b|\b(?:19|20)\d{2}\b",
            value,
        ))

    def _month_number(self, name: str):
        return {month.lower(): index for index, month in enumerate(calendar.month_name) if month}[name.lower()]

    def _month_bounds(self, year: int, month: int, kind: str):
        return {"type": kind, "start": date(year, month, 1).isoformat(), "end": date(year, month, calendar.monthrange(year, month)[1]).isoformat()}

    def _previous_month(self, today: date):
        month = today.month - 1 or 12
        year = today.year if today.month > 1 else today.year - 1
        return self._month_bounds(year, month, "last_month")

    def _quarter_bounds(self, today: date, shift: int):
        quarter_index = (today.month - 1) // 3 + shift
        year = today.year + quarter_index // 4
        quarter_index %= 4
        start_month = quarter_index * 3 + 1
        end_month = start_month + 2
        return {
            "type": "current_quarter" if shift == 0 else "last_quarter",
            "start": date(year, start_month, 1).isoformat(),
            "end": date(year, end_month, calendar.monthrange(year, end_month)[1]).isoformat(),
        }

    def _overlaps(self, span: tuple[int, int], existing: list[tuple[int, int]]):
        return any(span[0] < other[1] and other[0] < span[1] for other in existing)

    def _number(self, value: str):
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
        return words.get(value, int(value) if value.isdigit() else None)


semantic_parser = SemanticParser()
