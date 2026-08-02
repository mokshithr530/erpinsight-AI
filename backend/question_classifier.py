import re


QUESTION_CLASSIFIER_VERSION = "question-classifier:v2"


class QuestionClassifier:
    PATTERNS = {
        "metadata": (
            r"\bwhat is this database about\b",
            r"\bwhat is the database about\b",
            r"\bdescribe this database\b",
            r"\bdescribe the database\b",
            r"\bwhat modules\b",
            r"\bbusiness modules\b",
            r"\bwhat data is available\b",
            r"\bwhat tables\b",
        ),
        "count": (r"\bhow many\b", r"\bcount\b", r"\bnumber of\b"),
        "ranking": (r"\btop\b", r"\bhighest\b", r"\blowest\b", r"\bbest\b", r"\bworst\b"),
        "comparison": (r"\bcompare\b", r"\bversus\b", r"\bvs\b", r"\bbetween\b"),
        "trend": (r"\btrend\b", r"\bmonthly\b", r"\byearly\b", r"\bover time\b", r"\b202[0-9]\b"),
        "status": (r"\bpending\b", r"\bopen\b", r"\boutstanding\b", r"\bunpaid\b", r"\bstatus\b"),
        "business_insight": (r"\binsight", r"\bopportunit", r"\bperform", r"\brisk\b"),
        "missing_kpi": (r"\bprofit\b", r"\bloss\b", r"\bmargin\b", r"\broi\b", r"\befficiency\b"),
        "relationship": (r"\bjoin\b", r"\blink\b", r"\brelat", r"\bassigned\b", r"\bresponsible\b"),
    }

    METRIC_WORDS = {
        "billing": ("billing", "bill", "invoice", "invoicing", "billed"),
        "revenue": ("revenue", "sales", "turnover", "income"),
        "purchase": ("purchase", "po", "procurement", "spend"),
        "headcount": ("employee", "staff", "worker", "headcount"),
        "quantity": ("quantity", "qty", "units", "usage", "consumption"),
        "payment": ("payment", "paid", "received", "outstanding"),
        "profit_loss": ("profit", "loss", "margin", "roi"),
    }

    DIMENSION_WORDS = {
        "project": ("project", "job", "site", "work"),
        "client": ("client", "customer"),
        "vendor": ("vendor", "supplier", "contractor"),
        "department": ("department", "dept", "team", "division"),
        "employee": ("employee", "staff", "worker"),
        "product": ("product", "item", "material"),
        "date": ("month", "year", "date", "daily", "monthly", "yearly"),
    }

    def classify(self, question: str) -> dict:
        normalized = re.sub(r"\s+", " ", question.strip().lower())
        matched = []
        for intent, patterns in self.PATTERNS.items():
            if any(re.search(pattern, normalized) for pattern in patterns):
                matched.append(intent)
        if not matched:
            matched = ["general_complex_sql"]

        metrics = [
            metric
            for metric, aliases in self.METRIC_WORDS.items()
            if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases)
        ]
        dimensions = [
            dimension
            for dimension, aliases in self.DIMENSION_WORDS.items()
            if any(re.search(rf"\b{re.escape(alias)}s?\b", normalized) for alias in aliases)
        ]
        primary = self._primary_intent(matched)
        ambiguous = len(metrics) > 1 and "profit_loss" not in metrics
        if primary == "general_complex_sql" and not metrics and not dimensions:
            ambiguous = True
        return {
            "version": QUESTION_CLASSIFIER_VERSION,
            "question": question,
            "normalized_question": normalized,
            "intent": primary,
            "all_intents": matched,
            "metrics": metrics,
            "dimensions": dimensions,
            "needs_clarification": ambiguous,
            "confidence_score": 85 if not ambiguous else 45,
        }

    def _primary_intent(self, matched: list[str]) -> str:
        priority = [
            "metadata",
            "missing_kpi",
            "trend",
            "comparison",
            "ranking",
            "count",
            "status",
            "business_insight",
            "relationship",
            "general_complex_sql",
        ]
        for intent in priority:
            if intent in matched:
                return intent
        return matched[0]


question_classifier = QuestionClassifier()
