import re


class ConfidenceEngine:
    def build(
        self,
        *,
        intent="data_query",
        question="",
        sql=None,
        selected_tables=None,
        selected_columns=None,
        validation_ok=True,
        execution_ok=True,
        row_count=None,
        limitations=None,
        missing_kpi=False,
        assumptions=0,
        foreign_key_evidence=False,
        verified_join=False,
        inferred_join=False,
        metadata_only=False,
    ):
        selected_tables = selected_tables or []
        selected_columns = selected_columns or []
        limitations = limitations or []
        reasons = []
        score = 0

        grounding = 0
        if metadata_only:
            grounding = 24
            reasons.append("Used compact database metadata instead of guessing from raw schema.")
        else:
            if selected_tables:
                grounding += 14
                reasons.append("Matched the question to relevant database tables.")
            if selected_columns:
                grounding += 10
                reasons.append("Matched the question to relevant columns.")
            if self._entity_match(question, selected_tables, selected_columns):
                grounding += 6
                reasons.append("Business terms were grounded to schema names.")
        score += min(30, grounding)

        if metadata_only:
            score += 18
            reasons.append("No join was required for this metadata answer.")
        elif foreign_key_evidence:
            score += 25
            reasons.append("Join path has foreign-key evidence.")
        elif verified_join:
            score += 25
            reasons.append("Join path uses a verified ERP relationship.")
        elif inferred_join:
            score += 14
            reasons.append("Some join logic was inferred from shared ID columns.")
            limitations.append("Join path is inferred because the database has limited declared foreign keys.")
        elif sql and re.search(r"\bJOIN\b", sql, re.IGNORECASE):
            score += 10
            reasons.append("Join path exists but is weakly verified.")
            limitations.append("Join path could not be fully verified from foreign keys.")
        else:
            score += 20
            reasons.append("No complex join path was needed.")

        if validation_ok and sql:
            score += 15
            reasons.append("SQL passed read-only validation.")
        elif not validation_ok:
            limitations.append("SQL validation failed.")
        elif missing_kpi:
            score += 8
            reasons.append("Answered by identifying missing KPI data instead of guessing.")

        if execution_ok and sql:
            score += 10
            reasons.append("Query executed successfully.")
        elif not execution_ok:
            limitations.append("Query execution failed.")
        elif metadata_only:
            score += 8

        if metadata_only:
            score += 8
        elif row_count is None:
            score += 4
        elif row_count > 0:
            score += 10
            reasons.append("Data was returned successfully.")
        else:
            score += 4
            limitations.append("The query returned no matching rows.")

        penalty = 0
        if missing_kpi:
            penalty += 28
            limitations.append("Required KPI fields are missing or not clearly available.")
        if assumptions:
            penalty += min(20, assumptions * 8)
            limitations.append("Answer depends on assumptions from schema naming.")
        score -= penalty

        if not validation_ok or not execution_ok:
            score = min(score, 59)
        if missing_kpi:
            score = min(score, 49)
        if row_count == 0 and not metadata_only:
            score = min(score, 64)

        score = max(0, min(100, int(round(score))))
        level = "high" if score >= 75 else "medium" if score >= 50 else "low"

        return {
            "confidence_score": score,
            "confidence_level": level,
            "confidence_reasons": self._dedupe(reasons)[:5],
            "limitations": self._dedupe(limitations)[:5],
        }

    def _entity_match(self, question, tables, columns):
        words = set(re.findall(r"[a-z0-9]+", question.lower()))
        names = " ".join([*tables, *columns]).lower().replace("_", " ")
        return any(word in names for word in words if len(word) > 3)

    def _dedupe(self, values):
        seen = []
        for value in values:
            if value and value not in seen:
                seen.append(value)
        return seen


confidence_engine = ConfidenceEngine()
