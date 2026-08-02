import re
import time
from dataclasses import dataclass
from pathlib import Path

from business_catalog import BusinessCatalog
from schema_retriever import SchemaRetriever
from semantic_parser import semantic_parser
from schema_profile_fixtures import (
    construction_schema,
    finance_schema,
    hr_schema,
    inventory_schema,
    legacy_schema,
    manufacturing_schema,
    minimal_fk_schema,
    obfuscated_schema,
)


@dataclass(frozen=True)
class BenchmarkScenario:
    name: str
    question: str
    schema_factory: object
    primary_table: str
    required_tables: frozenset[str]


SCENARIOS = [
    BenchmarkScenario("erp", "Top 5 projects by approved value", construction_schema, "ProjectMaster", frozenset({"ProjectMaster"})),
    BenchmarkScenario("hr", "Average overtime hours by department", hr_schema, "TimeSheetEntries", frozenset({"TimeSheetEntries", "EmployeeDirectory", "Departments"})),
    BenchmarkScenario("finance", "Outstanding balance by customer in 2025", finance_schema, "sales_invoices", frozenset({"sales_invoices", "customer_accounts"})),
    BenchmarkScenario("inventory", "Returned quantity by item last month", inventory_schema, "inventory_movements", frozenset({"inventory_movements", "catalog_items"})),
    BenchmarkScenario("manufacturing", "Average downtime hours by machine", manufacturing_schema, "machine_downtime_events", frozenset({"machine_downtime_events"})),
    BenchmarkScenario("legacy", "Top 3 projects by approved amount", legacy_schema, "t_prj_hdr", frozenset({"t_prj_hdr"})),
    BenchmarkScenario("obfuscated", "Top customers by invoice amount in 2025", obfuscated_schema, "z77", frozenset({"z77", "a01"})),
    BenchmarkScenario("minimal_fk", "Total hours by team", minimal_fk_schema, "work_log", frozenset({"work_log", "teams"})),
]


def _legacy_terms(text):
    words = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    expanded = set(words)
    for word in list(words):
        if word.endswith("s") and len(word) > 3:
            expanded.add(word[:-1])
        if word == "billing":
            expanded.update({"bill", "invoice", "revenue"})
        if word == "purchase":
            expanded.update({"po", "procurement", "spend"})
        if word == "vendor":
            expanded.add("supplier")
        if word == "client":
            expanded.add("customer")
        if word == "customer":
            expanded.add("client")
        if word == "material":
            expanded.update({"item", "product", "inventory", "store"})
        if word == "project":
            expanded.update({"site", "work", "job"})
        if word == "quantity":
            expanded.update({"qty", "units"})
    return expanded


def _legacy_rank(question, plan, profiles, limit=8):
    question_terms = _legacy_terms(question)
    values = []
    for key in ("metrics", "dimensions", "aggregations", "sort_fields", "requested_output"):
        values.extend(plan.get(key) or [])
    values.append(plan.get("operation") or "")
    semantic_terms = _legacy_terms(" ".join(str(value) for value in values))
    ranked = []
    for table, profile in profiles.items():
        table_terms = set(profile.business_terms) | _legacy_terms(table)
        module_terms = _legacy_terms(profile.module or "")
        column_terms = {term for column in profile.columns for term in column.business_terms}
        score = min(0.28, len(question_terms & table_terms) * 0.055)
        score += min(0.34, len(semantic_terms & (table_terms | module_terms | column_terms)) * 0.07)
        score += min(0.22, len(question_terms & column_terms) * 0.04)
        if profile.primary_keys:
            score += 0.05
        if profile.relationship_hints:
            score += 0.04
        if profile.row_count is not None:
            score += 0.08 if int(profile.row_count or 0) > 0 else -0.16
        if plan.get("operation") in {"ranking", "aggregation"} and any(
            column.semantic_type in {"amount", "quantity"} for column in profile.columns
        ):
            score += 0.06
        if score > 0:
            reasons = ["legacy lexical/semantic score"]
            if profile.primary_keys:
                reasons.append("contains primary key")
            if profile.row_count is not None and int(profile.row_count or 0) > 0:
                reasons.append("non-empty table")
            ranked.append((min(1.0, score), table, reasons))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = [table for _score, table, _reasons in ranked[:limit]]
    lines = [
        "Retrieved compact schema context:",
        f"- Retrieval confidence: {ranked[0][0] if ranked else 0}",
        f"- Tables included: {len(selected)}",
        "Ranked tables:",
    ]
    for score, table, reasons in ranked[:limit]:
        profile = profiles[table]
        columns = []
        for column in profile.columns[:14]:
            flags = []
            if column.primary_key:
                flags.append("pk")
            if column.indexed:
                flags.append("idx")
            if column.semantic_type:
                flags.append(column.semantic_type)
            flag_text = f" [{' '.join(flags)}]" if flags else ""
            columns.append(f"{column.column_name} {column.data_type}{flag_text}")
        row_note = f"~{profile.row_count:,} rows" if profile.row_count is not None else "row count unknown"
        lines.append(f"- {table} ({row_note}, module={profile.module or 'unknown'}, score={score}): {', '.join(columns)}")
        lines.append(f"  reasons: {'; '.join(reasons)}")
    context_size = len("\n".join(lines))
    return ranked, selected, context_size


def run_benchmark(profile_dir):
    profile_dir = Path(profile_dir)
    totals = {
        "legacy_top1": 0,
        "new_top1": 0,
        "legacy_top3": 0,
        "new_top3": 0,
        "legacy_precision": 0.0,
        "new_precision": 0.0,
        "legacy_recall": 0.0,
        "new_recall": 0.0,
        "legacy_context_chars": 0,
        "new_context_chars": 0,
        "legacy_latency_ms": 0.0,
        "new_latency_ms": 0.0,
    }
    details = []
    for scenario in SCENARIOS:
        schema = scenario.schema_factory()
        row_counts = {name: info.get("row_count") for name, info in schema.items()}
        fingerprint = f"benchmark-{scenario.name}"
        catalog = BusinessCatalog().build(
            schema,
            source_type="synthetic",
            row_counts=row_counts,
            fingerprint=fingerprint,
        )
        plan = semantic_parser.parse(scenario.question, allow_llm_fallback=False).to_dict()
        retriever = SchemaRetriever(profile_dir=profile_dir / scenario.name)
        profiles = retriever.profiles_for(
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=fingerprint,
        )

        legacy_started = time.perf_counter()
        legacy_ranked, legacy_selected, legacy_context = _legacy_rank(scenario.question, plan, profiles)
        legacy_latency = (time.perf_counter() - legacy_started) * 1000

        retriever.retrieve(
            question=scenario.question,
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=fingerprint,
            semantic_plan=plan,
            candidate_limit=8,
        )
        new_started = time.perf_counter()
        result = retriever.retrieve(
            question=scenario.question,
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=fingerprint,
            semantic_plan=plan,
            candidate_limit=8,
        )
        new_latency = (time.perf_counter() - new_started) * 1000

        legacy_top = [table for _score, table, _reasons in legacy_ranked]
        new_top = [item.table for item in result.ranked_tables]
        expected = set(scenario.required_tables)
        legacy_set = set(legacy_selected)
        new_set = set(result.selected_tables)
        row = {
            "name": scenario.name,
            "legacy_top1": int(bool(legacy_top) and legacy_top[0] == scenario.primary_table),
            "new_top1": int(bool(new_top) and new_top[0] == scenario.primary_table),
            "legacy_top3": int(scenario.primary_table in legacy_top[:3]),
            "new_top3": int(scenario.primary_table in new_top[:3]),
            "legacy_precision": len(legacy_set & expected) / max(1, len(legacy_set)),
            "new_precision": len(new_set & expected) / max(1, len(new_set)),
            "legacy_recall": len(legacy_set & expected) / len(expected),
            "new_recall": len(new_set & expected) / len(expected),
            "legacy_context_chars": legacy_context,
            "new_context_chars": result.context_character_count,
            "legacy_latency_ms": legacy_latency,
            "new_latency_ms": new_latency,
        }
        details.append(row)
        for key in totals:
            totals[key] += row[key]

    count = len(SCENARIOS)
    summary = {
        "top1_accuracy": {"legacy": totals["legacy_top1"] / count, "new": totals["new_top1"] / count},
        "top3_accuracy": {"legacy": totals["legacy_top3"] / count, "new": totals["new_top3"] / count},
        "precision": {"legacy": totals["legacy_precision"] / count, "new": totals["new_precision"] / count},
        "recall": {"legacy": totals["legacy_recall"] / count, "new": totals["new_recall"] / count},
        "average_context_chars": {"legacy": totals["legacy_context_chars"] / count, "new": totals["new_context_chars"] / count},
        "average_latency_ms": {"legacy": totals["legacy_latency_ms"] / count, "new": totals["new_latency_ms"] / count},
    }
    return {"summary": summary, "details": details}
