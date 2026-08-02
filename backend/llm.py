import os
import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

try:
    import certifi_win32  # noqa: F401
except ImportError:
    certifi_win32 = None

try:
    import certifi
    os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", certifi.where())
except ImportError:
    certifi = None

try:
    import truststore
except ImportError:
    truststore = None

if truststore:
    truststore.inject_into_ssl()

from google import genai

from config import (
    GEMINI_API_KEYS,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
    LLM_API_KEYS,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TIMEOUT_SECONDS,
)
from session_memory import session_memory
from metrics import token_metrics
import logging

logger = logging.getLogger(__name__)


def _clean_sql(sql):
    cleaned = sql.replace("```sql", "")
    cleaned = cleaned.replace("```", "")
    return cleaned.strip()


def _clean_answer(answer):
    cleaned = answer.replace("**", "")
    cleaned = cleaned.replace("`", "")
    return cleaned.strip()


class BaseLLMProvider(ABC):
    def __init__(self, provider_name, model_name):
        self.provider_name = provider_name
        self.model_name = model_name

    @abstractmethod
    def generate(self, prompt, call_type="generate"):
        raise NotImplementedError


class GeminiProvider(BaseLLMProvider):
    def __init__(self, model_name, fallback_models, api_keys, timeout_seconds):
        super().__init__("gemini", model_name)
        self.model_names = list(dict.fromkeys([model_name, *fallback_models]))
        self.api_keys = api_keys
        self.timeout_seconds = timeout_seconds
        self.active_key_index = 0
        self.client = None

        if self.api_keys:
            self._configure_key(0)

    def _configure_key(self, key_index):
        self.active_key_index = key_index
        self.client = genai.Client(
            api_key=self.api_keys[key_index],
            http_options={"timeout": self.timeout_seconds * 1000},
        )

    def _should_try_next_key(self, error):
        message = str(error).lower()
        retry_markers = [
            "quota",
            "rate",
            "resource exhausted",
            "429",
            "permission",
            "api key",
            "authentication",
        ]
        return any(marker in message for marker in retry_markers)

    def _should_try_next_model(self, error):
        message = str(error).lower()
        retry_markers = [
            "503",
            "504",
            "unavailable",
            "deadline_exceeded",
            "deadline exceeded",
            "high demand",
            "overloaded",
            "temporarily",
            "timed out",
            "timeout",
        ]
        return any(marker in message for marker in retry_markers)

    def generate(self, prompt, call_type="generate"):
        if not self.api_keys or self.client is None:
            raise RuntimeError(
                "LLM API key is not configured. Set LLM_API_KEY, LLM_API_KEYS, GEMINI_API_KEY, or GEMINI_API_KEYS in backend/.env."
            )

        last_error = None

        for offset in range(len(self.api_keys)):
            key_index = (self.active_key_index + offset) % len(self.api_keys)
            self._configure_key(key_index)

            for model_name in self.model_names:
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )

                    # Track token usage
                    try:
                        if hasattr(response, 'usage_metadata'):
                            usage = response.usage_metadata
                            input_tokens = (
                                getattr(usage, "input_token_count", None)
                                or getattr(usage, "prompt_token_count", None)
                                or 0
                            )
                            output_tokens = (
                                getattr(usage, "output_token_count", None)
                                or getattr(usage, "candidates_token_count", None)
                                or 0
                            )
                        elif hasattr(response, 'usage'):
                            input_tokens = response.usage.prompt_tokens or 0
                            output_tokens = response.usage.candidates_tokens or 0
                        else:
                            input_tokens = 0
                            output_tokens = 0

                        token_metrics.add_tokens(call_type, input_tokens, output_tokens)
                    except Exception as token_error:
                        logger.warning(f"Could not capture token metrics: {token_error}")

                    return response.text.strip()
                except Exception as error:
                    last_error = error

                    if self._should_try_next_model(error):
                        continue

                    if not self._should_try_next_key(error):
                        break

            if last_error and not self._should_try_next_key(last_error):
                break

        raise RuntimeError(
            "LLM request failed after trying "
            f"{len(self.api_keys)} configured key(s) and "
            f"{len(self.model_names)} model(s): {last_error}"
        )


class JsonAPIProvider(BaseLLMProvider):
    def __init__(self, provider_name, model_name, api_keys, timeout_seconds, base_url=""):
        super().__init__(provider_name, model_name)
        self.api_keys = api_keys
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url

    def _request(self, url, headers, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {error.code}: {body}") from error

    def generate(self, prompt, call_type="generate"):
        if not self.api_keys:
            raise RuntimeError("LLM API key is not configured in backend/.env.")

        last_error = None
        for api_key in self.api_keys:
            try:
                if self.provider_name == "anthropic":
                    data = self._request(
                        self.base_url or "https://api.anthropic.com/v1/messages",
                        {
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                        },
                        {
                            "model": self.model_name or "claude-sonnet-4-20250514",
                            "max_tokens": 2048,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                    usage = data.get("usage", {})
                    token_metrics.add_tokens(
                        call_type,
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                    )
                    return data["content"][0]["text"].strip()

                if self.provider_name == "openrouter":
                    base_url = self.base_url or "https://openrouter.ai/api/v1"
                else:
                    base_url = self.base_url or "https://api.openai.com/v1"
                data = self._request(
                    f"{base_url}/chat/completions",
                    {"Authorization": f"Bearer {api_key}"},
                    {
                        "model": self.model_name or "gpt-4.1-mini",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                usage = data.get("usage", {})
                token_metrics.add_tokens(
                    call_type,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, RuntimeError, urllib.error.URLError) as error:
                last_error = error

        raise RuntimeError(f"{self.provider_name} request failed: {last_error}")


def _resolved_provider():
    if LLM_PROVIDER != "auto":
        return LLM_PROVIDER
    key = LLM_API_KEYS[0] if LLM_API_KEYS else ""
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    return "gemini"


def create_provider():
    provider = _resolved_provider()
    if provider == "gemini":
        return GeminiProvider(
            model_name=GEMINI_MODEL,
            fallback_models=GEMINI_FALLBACK_MODELS,
            api_keys=GEMINI_API_KEYS,
            timeout_seconds=GEMINI_TIMEOUT_SECONDS,
        )

    if provider in {"anthropic", "openai", "openai-compatible", "openrouter"}:
        return JsonAPIProvider(
            provider_name=provider if provider in {"anthropic", "openrouter"} else "openai",
            model_name=LLM_MODEL or ("deepseek/deepseek-v4-pro" if provider == "openrouter" else ""),
            api_keys=LLM_API_KEYS,
            timeout_seconds=LLM_TIMEOUT_SECONDS,
            base_url=LLM_BASE_URL,
        )

    raise RuntimeError(f"Unsupported LLM provider: {provider}")


class LLMService:
    def __init__(self, provider):
        self.provider = provider

    def _generate(self, prompt, call_type="generate"):
        return self.provider.generate(prompt, call_type)

    def _conversation_history(self):
        history = session_memory.get_chat_history()
        conversation = ""

        for message in history:
            conversation += f"{message['role']}: {message['content']}\n"

        return conversation

    def generate_sql(self, question, schema_prompt):
        prompt = f"""
You are a senior data analyst and SQL engineer.

Task:
Convert the user's natural-language question into one safe SQLite SELECT query.

Database schema:
{schema_prompt}

Recent conversation:
{self._conversation_history()}

Current question:
{question}

Rules:
1. Return only executable SQL. No markdown, comments, explanation, or code fences.
2. Use only SELECT or WITH queries.
3. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, DETACH, or multiple statements.
4. Use only tables and columns that exist in the schema.
5. Prefer explicit column names over SELECT *.
6. Add a reasonable LIMIT when the question asks for lists, rankings, examples, or broad results.
7. Use SQLite-compatible syntax only.
8. If the question is ambiguous, choose the most likely interpretation using the schema and conversation.
9. Infer joins from shared ID columns when foreign keys are not declared, such as order_id, product_id, customer_id, and seller_id.
10. For "sold most", "top product", or "best product" questions, rank by item count unless the user explicitly asks for revenue.
11. For "what should I focus on" or business recommendation questions, return useful comparison metrics, usually units/count, revenue, average price, and order count when those columns exist.

Common ecommerce patterns:
- Product/category sold most by units:
  SELECT t.product_category_name_english AS product_category, COUNT(*) AS units_sold, SUM(oi.price) AS revenue
  FROM order_items oi
  JOIN products p ON oi.product_id = p.product_id
  LEFT JOIN product_category_name_translation t ON p.product_category_name = t.product_category_name
  GROUP BY t.product_category_name_english
  ORDER BY units_sold DESC
  LIMIT 10
- Product/category to focus on:
  SELECT t.product_category_name_english AS product_category, COUNT(*) AS units_sold, SUM(oi.price) AS revenue, AVG(oi.price) AS average_price
  FROM order_items oi
  JOIN products p ON oi.product_id = p.product_id
  LEFT JOIN product_category_name_translation t ON p.product_category_name = t.product_category_name
  GROUP BY t.product_category_name_english
  ORDER BY revenue DESC
  LIMIT 10
"""

        return _clean_sql(self._generate(prompt, "generate_sql"))

    def generate_sqlserver_sql(self, question, schema_prompt):
        prompt = f"""
You are a senior data analyst and SQL Server engineer.

Task:
Convert the user's natural-language question into one safe SQL Server SELECT query.

Database schema:
{schema_prompt}

Recent conversation:
{self._conversation_history()}

Current question:
{question}

Rules:
1. Return only executable SQL. No markdown, comments, explanation, or code fences.
2. Use only SELECT or WITH queries.
3. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, MERGE, EXEC, EXECUTE, stored procedures, USE, or multiple statements.
4. Use only tables and columns that exist in the schema.
5. Use SQL Server syntax. Use TOP, not LIMIT.
6. Do not use cross-database names like database.schema.table.
7. Prefer explicit column names over SELECT *.
8. Add TOP for broad lists or rankings.
9. If the schema prompt is marked AUTHORITATIVE ERP SQL CONTEXT, use only its approved relationships and never infer another join. Otherwise infer joins cautiously from schema evidence.
"""

        return _clean_sql(self._generate(prompt, "generate_sql"))

    def generate_mysql_sql(self, question, schema_prompt):
        prompt = f"""
You are a senior data analyst and MySQL engineer.

Task:
Interpret the user's natural-language question carefully, map vague business words to the most relevant tables/columns, then write one safe MySQL SELECT query.

Database schema:
{schema_prompt}

Recent conversation:
{self._conversation_history()}

Current question:
{question}

Rules:
1. Return only executable SQL. No markdown, comments, explanation, or code fences.
2. Use only SELECT or WITH queries.
3. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, REPLACE, LOAD, LOCK, UNLOCK, USE, or multiple statements.
4. Use only tables and columns that exist in the schema.
5. Use MySQL syntax. Use LIMIT, not TOP.
6. Do not use cross-database names like database.table.
7. Prefer explicit column names over SELECT *.
8. Add LIMIT for broad lists or rankings.
9. Infer joins from shared ID columns when foreign keys are not declared.
10. For "how many", "count", or "are there only N" questions, return a COUNT query, not a sample list.
11. For "top/highest/largest by value/amount", rank by the relevant value/amount column from the entity table when it exists.
12. When multiple tables look relevant, compare their names, columns, row-count hints, and master-table patterns such as _mst/_master before choosing.
13. Prefer populated master/entity tables over empty lookalike tables.
14. If date columns are varchar/text and appear to store DD-MM-YYYY values, compare dates with STR_TO_DATE(NULLIF(column, ''), '%%d-%%m-%%Y').
15. If the prompt includes a resolved entity with a primary_key, anchor the query around that table/key instead of searching a different table by name.
16. For manager-style vague wording, choose the strongest schema-supported interpretation; if no safe interpretation exists, return a harmless SELECT that shows zero rows from the most relevant table rather than inventing unavailable fields.
17. Preserve the required query plan contract exactly: requested entity filters, date filters, status filters, grouping, ordering direction, aggregation, and LIMIT must appear in the SQL when present.
18. Do not answer a related question by dropping a requested project, site, vendor, client, employee, material, status, date range, or top/bottom limit.
19. In authoritative ERP context, business definitions and aggregation grain override guesses based on table or column names.
20. In authoritative ERP context, use only the canonical DDL tables and columns and obey every duplicate-safety or pre-aggregation rule.
"""

        return _clean_sql(self._generate(prompt, "generate_sql"))

    def generate_metadata_answer(self, question, metadata):
        prompt = f"""
You are explaining an ERP/database to a manager.

User question:
{question}

Compact database metadata:
{metadata}

Write a concise manager-friendly answer.

Rules:
1. Do not write SQL.
2. Do not invent business facts that are not in the metadata.
3. Use a short summary first.
4. Then use bullets for modules, important tables, and available analysis areas.
5. Mention limitations briefly if the metadata is incomplete.
6. Do not claim profit, loss, or profitability analysis is available unless the metadata clearly shows cost, expense, margin, or profit fields.
7. Keep it under 180 words.
"""

        return _clean_answer(self._generate(prompt, "metadata_answer"))

    def generate_answer(self, question, dataframe):
        prompt = f"""
You are a helpful data analyst speaking to a business user.

User question:
{question}

SQL result:
{dataframe.to_string(index=False)}

Write the final answer in a natural, manager-friendly style.

Rules:
1. Start with a short summary.
2. Use the actual values from the SQL result.
3. If there are multiple rows, use concise bullets or a compact markdown table.
4. For recommendation questions like "what should I focus on", explain why the top option is attractive and mention one tradeoff if the data supports it.
5. If columns include revenue and units/count, compare both instead of only naming the first row.
6. If columns include revenue_at_risk, explain that this is a proxy for possible loss, not true profit/loss, unless actual cost or margin columns are present.
7. If the result is empty, say that no matching records were found and suggest what the user can try next.
8. Do not mention "SQL result", "dataframe", "snippet", or internal execution details.
9. Do not sound robotic. Be clear, warm, and concise.
10. Do not invent anything outside the result.
11. Add "Limitations" only when the data cannot support the requested KPI.
12. Format money as Indian rupees using ₹ or INR unless the result explicitly contains a different currency.
"""

        return _clean_answer(self._generate(prompt, "generate_answer"))

    def generate_verified_answer(
        self,
        question,
        facts,
        execution_receipt,
    ):
        prompt = f"""
You are explaining verified ERP query results to a manager.

User question:
{question}

Verified facts:
{json.dumps(facts, indent=2, sort_keys=True, default=str)}

Execution receipt:
{json.dumps(execution_receipt, indent=2, sort_keys=True, default=str)}

Rules:
1. Answer only from the verified facts. Never add a number, entity, trend, cause, KPI, or recommendation that is absent from those facts.
2. Preserve exact counts, ordering, labels, dates, and values.
3. If row_count is zero, clearly say no matching records were returned. Do not claim the database is empty and do not suggest different facts exist.
4. Keep the tone natural, short, and human.
5. Start with one direct sentence, not a formal report heading.
6. Use 2 to 4 short bullets for what matters.
7. Use a compact markdown table when multiple rows are present.
8. Add limitations only when they matter for interpretation.
9. Recommendations are allowed only when directly supported by the returned rows; state the supporting fact.
10. Do not reinterpret one metric as another. Billing is not profit, purchase value is not cost, and revenue is not cash collected unless the verified columns explicitly say so.
11. Format monetary values as Indian rupees using INR unless the facts explicitly identify another currency.
12. Do not mention prompts, tokens, dataframes, internal agents, or hidden implementation.
13. Do not expose table names, column names, SQL, hashes, or the execution receipt in the manager-facing answer.
14. Avoid long paragraphs and avoid headings like "Short summary" unless they are already present in the facts.
"""

        return _clean_answer(
            self._generate(prompt, "generate_verified_answer")
        )

    def rewrite_failed_sql(self, question, schema_prompt, failed_sql, error_message):
        prompt = f"""
You are repairing a failed SQLite query.

Original question:
{question}

Database schema:
{schema_prompt}

Failed SQL:
{failed_sql}

Validation or database error:
{error_message}

Use only tables and columns from the schema above. If the user asks about products, orders, customers, or sales, infer the likely joins from foreign keys and column names.

Return only one corrected SQLite SELECT or WITH query.
Do not explain, do not use markdown, and do not include unsafe SQL.
"""

        return _clean_sql(self._generate(prompt, "rewrite_failed_sql"))

    def rewrite_failed_mysql_sql(self, question, schema_prompt, failed_sql, error_message):
        prompt = f"""
You are repairing a failed MySQL query.

Original question:
{question}

Database schema:
{schema_prompt}

Failed SQL:
{failed_sql}

Validation or database error:
{error_message}

Return only one corrected MySQL SELECT or WITH query.
Do not explain, do not use markdown, and do not include unsafe SQL.
Use LIMIT for broad result sets.
Use the exact table aliases and columns from the schema. If an error says an unknown column is on one alias, move that column to the joined table where it actually exists.
Preserve every required query plan constraint in the schema prompt, including entity filters, date filters, status filters, grouping, ordering direction, aggregation, and LIMIT.
If the prompt is marked AUTHORITATIVE ERP SQL CONTEXT, use only its canonical DDL and approved relationships. Do not invent a different table, column, metric, or join while repairing.
"""

        return _clean_sql(self._generate(prompt, "rewrite_failed_sql"))

    def summarize_schema(self, schema_prompt):
        prompt = f"""
                Summarize this database.
                {schema_prompt}
                Keep it under 80 words."""

        return self._generate(prompt)

    def suggest_followup(self, question, answer):
        prompt = f"""
                User Question
                {question}
                Answer
                {answer}
                Suggest three useful follow-up questions.
                """

        return self._generate(prompt)


llm = LLMService(create_provider())
