# ERP Accuracy Baseline

Date: 2026-07-21

This baseline documents the current Natural Language to SQL flow before the next accuracy changes. It intentionally does not refactor the chat pipeline.

## Verification Results

| Check | Result |
| --- | --- |
| `python -m pytest -q` | Passed, `287 passed`, 1 existing Starlette deprecation warning |
| `python -m compileall backend` | Passed |
| `npm run build` | Passed |
| `npm run lint` | Passed |
| `docker compose ps` | MySQL, Redis, and SQL Server containers are healthy |

Existing failure notes:

- No failing tests were found in this baseline run.
- Existing warning: `StarletteDeprecationWarning` from `fastapi.testclient`.

## Connected ERP Database

Current browser/session state points to MySQL database:

`rcerp_jul8_manual_20260714_094608`

Measured from MySQL metadata:

| Metric | Value |
| --- | ---: |
| Tables | 1229 |
| Columns | 18239 |
| Populated tables, approximate | 252 |
| Schema fingerprint | `998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5` |

Largest active tables by MySQL metadata:

| Table | Approx Rows |
| --- | ---: |
| `client_bills_boq_wisedetails` | 140694 |
| `subcontract_workorder` | 109124 |
| `store_transaction_logs` | 104129 |
| `uesr_login_details` | 87819 |
| `store_material_usage_quantity` | 74053 |
| `boq_unit_bill_raising` | 55361 |
| `sub_material` | 32742 |
| `attachments_sub_store` | 28416 |

Detected business modules:

- Billing
- Project
- Employee
- Department
- Customer
- Vendor
- Product/material
- Payment
- Inventory/store
- Logistics

Important tables currently surfaced by the catalog:

- `employee_table`
- `goods_receipt_cum_insp_note`
- `grn_master`
- `intend_quotes`
- `intend_po_list`
- `employee_rejoin_hist`
- `subcontract_workorder_details`
- `spare_po_final_price`
- `bank_transaction`
- `asset_po_final_price`
- `project_mst`
- `po_final_price`

## Current Request Flow

1. Frontend stores `session_id` in localStorage and sends it with `/upload`, `/chat`, `/clear`, and MySQL attach calls.
2. `backend/main.py` receives `/chat` and checks casual messages before database routing.
3. Session source is loaded from `backend/runtime/sessions/<session_id>.json`.
4. MySQL sessions dispatch to `chat_pipeline.ask_mysql(question, database, identity)`.
5. `ask_mysql` validates the question, starts token metrics, and builds LLM/cache identity.
6. `semantic_parser.parse()` creates the semantic query plan and question contract.
7. `question_classifier.classify()` assigns the high-level intent.
8. `mysql_adapter.analyze_question()` uses the business catalog for schema/business matches.
9. `mysql_adapter.resolve_entities()` resolves only semantic candidate mentions against actual database values.
10. Redis checks for a final response cache hit using database/schema/provider/model/resolution identity.
11. Metadata and unsupported KPI paths may answer without SQL generation.
12. Schema retrieval runs through `mysql_adapter.retrieve_schema_context()` and `schema_retriever.retrieve()`.
13. `chat_pipeline._mysql_schema_prompt()` builds the compact SQL-generation prompt.
14. `llm.generate_mysql_sql()` generates one read-only MySQL query.
15. `mysql_adapter.validate_sql()` validates syntax, tables, columns, safety, row limits, and dialect rules.
16. `mysql_adapter.execute_read_query()` executes the validated read-only query.
17. Empty-result, alias/column, and execution failures may trigger one bounded repair/retry path.
18. `llm.generate_answer()` summarizes the result; local fallback formatting is used if the LLM answer step fails.
19. `confidence_engine.build()` computes confidence from backend signals.
20. `response_cache.set_by_identity()` stores only successful final responses.
21. `main._present_chat_response()` removes token metrics before returning to the frontend.

SQLite follows the same `/chat` API but uses `chat_pipeline.ask()`, `SchemaReader`, `validator`, and `sql_executor`.

SQL Server uses `chat_pipeline.ask_sqlserver()` with `sqlserver_adapter`.

MCP tools in `backend/mcp_server.py` are LLM-free and expose schema inspection, question analysis, entity resolution, SQL validation, dry-run, and read-only execution.

## Trace Capture Points

Recommended trace fields and capture locations:

| Trace Field | Current Capture Location |
| --- | --- |
| original question | `main.chat()` or start of `ChatPipeline.ask_mysql()` |
| parsed intent | `semantic_parser.parse()` and `question_classifier.classify()` |
| question contract | `semantic_plan["question_contract"]` |
| extracted entity candidates | `entity_resolver.candidates_from_plan()` through `mysql_adapter.resolve_entities()` |
| resolved entities | `entity_resolution["entities"]` and `entity_resolution["matches"]` |
| selected tables | `schema_context["result"]["selected_tables"]` |
| selected columns | `schema_context["result"]["relevant_columns"]` |
| relationship path | `schema_context["result"]["relationship_paths"]` plus entity relationship validation |
| query plan | SQL-generation inputs and retry/repair path metadata |
| generated SQL | `sql` inside `ask_mysql()` |
| validation result | `mysql_adapter.validate_sql()` |
| execution result | `mysql_adapter.execute_read_query()` dataframe payload |
| final response | response dict before `_public_response()` |
| latency | around `ask_mysql()` or per stage with `TraceTimer` |
| confidence | `confidence_engine.build()` output |
| error category | validation/execution/response classification |

Minimum tracing infrastructure added:

- `backend/evaluation_trace.py`
- `EvaluationTrace` dataclass covering every required trace field.
- `TraceTimer` helper for latency.
- `classify_error_category()` helper for consistent error buckets.

This helper is intentionally not wired into `/chat` yet, so the runtime behavior is unchanged.

## Current Limitations

- Accuracy still depends on schema retrieval picking the right compact context before SQL generation.
- The ERP has many similarly named tables, history tables, attachment tables, and empty lookalike tables.
- MySQL metadata row counts are approximate unless exact counts are explicitly queried.
- Foreign-key metadata appears limited, so join confidence often depends on inferred relationships.
- Redis can still return cached successful responses, though cache identity now includes schema, provider/model, resolver, catalog, and result-shaping versions.
- MCP SQL Server entity value lookup remains limited compared with MySQL and SQLite.
- No baseline question set has been executed in this step; this pass establishes infrastructure and repository/database status only.

## Recommended Evaluation Framework Location

Add the next evaluation harness under:

- `backend/evaluation_trace.py` for trace schema and helpers.
- `tests/evaluation/` for deterministic fixtures and regression questions.
- `backend/runtime/evaluation_traces/` for local trace JSONL output ignored from source control.

Suggested next file:

- `backend/evaluation_runner.py`

It should run a fixed question set against the active ERP session/database, capture `EvaluationTrace` records, and write JSONL for comparison between parser/retriever/prompt changes.

