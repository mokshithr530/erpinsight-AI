# SQL Assistant POC

A proof-of-concept app for asking business questions about SQL data in natural language.

You can upload SQLite databases, import SQL dumps, or connect to an existing configured MySQL database. The backend inspects the schema, narrows the relevant tables, asks the configured LLM only when needed, validates generated SQL, runs read-only queries, and returns a manager-friendly answer with tables, bullets, limitations, and confidence.

The management demonstration uses a deliberately limited, verified ERP module
set. See [POC_SCOPE.md](POC_SCOPE.md) for supported, limited and out-of-scope
questions.

## Current POC Focus

This POC is optimized for one stable ERP product schema, not for arbitrary
unknown databases. The useful generic discovery work is now used as support,
while the main runtime path prefers verified ERP knowledge:

- verified business modules, metrics, aliases, and entity types
- approved table and column mappings
- approved relationship paths and duplicate-risk rules
- deterministic SQL compilation for supported management questions
- plan-aware SQL validation before read-only execution
- short manager-friendly answers with tables, confidence, and limitations

A printable 3-page overview is available at
[docs/ERP_AI_Assistant_POC_Overview.html](docs/ERP_AI_Assistant_POC_Overview.html).
It explains the POC architecture, how questions work, and what the assistant can
answer.

A printable supported-question list is available at
[docs/ERP_AI_Assistant_Question_List.html](docs/ERP_AI_Assistant_Question_List.html).

## Verified Demo Coverage

The strongest POC modules are:

- Projects: running projects, project details, project value, project comparisons
- Billing: total billing, billing by project, project billing comparisons
- Purchase Orders: total PO value, top POs, PO by project/vendor, PO comparisons
- Vendors: vendor lists, vendor details, top vendors by PO value, comparisons
- Store / Material Movement: material issued, top materials, recent movement
- Inventory: basic inventory and material availability style questions

Unsupported or ambiguous business KPIs such as profit, margin, loss,
performance, and risk score should ask for clarification or a verified formula
instead of guessing.

## Stack

- Frontend: React, TypeScript, Vite, Tailwind CSS
- Backend: FastAPI, SQLite, MySQL, SQL Server, Pandas
- Cache: Redis, optional
- LLM: configured through `backend/.env` with provider abstraction

## Architecture

High-level structure:

```text
Browser
  |
  v
React UI
  |
  v
src/services/api.ts
  |
  v
FastAPI backend
  |
  +--> Upload / connect / schema / session
  |
  +--> Chat pipeline
          |
          +--> Business catalog + schema narrowing
          |
          +--> LLM provider layer
          |
          +--> SQL validator
          |
          +--> SQLite / MySQL / SQL Server executor
          |
          v
        Table + summary + confidence returned to UI
```

Upload flow:

```text
User chooses data source
  |
  v
POST /upload or /mysql/attach
  |
  v
Validate file type or configured database
  |
  v
SQLite: save and query directly
MySQL .sql: stream import into MySQL
SQL Server .sql: import into SQL Server when enabled
Existing MySQL: attach without re-importing
  |
  v
Read tables, columns, relationships
  |
  v
Store safe session metadata
```

Chat flow:

```text
User asks question
  |
  v
POST /chat
  |
  v
Read session database
  |
  v
Classify intent and narrow schema
  |
  v
Generate SQL only when needed
  |
  v
Validate SQL safety
  |
  v
Execute SQL
  |
  v
Format result into answer, table, limitations, confidence
  |
  v
Return answer + SQL + rows + confidence
```

LLM and MCP design:

```text
chat_pipeline.py
  |
  v
LLMService
  |
  v
Provider
  |
  +--> Anthropic
  |
  +--> Gemini
  |
  +--> OpenRouter
  |
  +--> OpenAI-compatible APIs

MCP client (Claude Desktop, Claude Code, or another host)
  |
  v
backend/mcp_server.py
  |
  +--> inspect_schema
  |
  +--> analyze_question
  |
  +--> validate_sql
  |
  +--> dry_run_query
  |
  +--> execute_read_query (read-only)
```

The frontend never calls the LLM directly. All provider calls happen inside the FastAPI backend.

## First-Time Setup

Run these commands from the project root:

```powershell
npm install
pip install -r requirements.txt
```

Create your private environment file:

```powershell
copy backend\.env.example backend\.env
```

Open `backend/.env` and replace the placeholder key:

```env
LLM_PROVIDER=auto
LLM_API_KEYS=paste_your_real_api_key_here
LLM_MODEL=deepseek/deepseek-v4-pro
LLM_FALLBACK_MODELS=
LLM_BASE_URL=
GEMINI_TIMEOUT_SECONDS=20
QUERY_ROW_LIMIT=100
MAX_UPLOAD_BYTES=5368709120
```

`auto` recognizes Gemini (`AIza...`), Anthropic (`sk-ant-...`), OpenRouter
(`sk-or-...`), and OpenAI-compatible (`sk-...`) keys. Set `LLM_PROVIDER`
explicitly for unusual key formats. For OpenRouter or another OpenAI-compatible
service, also set `LLM_BASE_URL`.

## What The Answer Looks Like

The UI is designed for project managers, so answers avoid raw technical noise.

For multi-row results, the app shows:

- A short summary
- A table with the most useful business columns
- Key findings in bullets
- Limitations when the data cannot support a stronger answer
- Confidence score with a short "Why?" explanation

Example:

| Department | Employee Count |
| --- | ---: |
| TECHNICAL DEPARTMENT | 200 |
| Execution | 145 |
| NON-TECHNICAL | 105 |
| STORES | 62 |

The explanation tells the manager what the table means, such as staffing
concentration, possible workload/cost follow-up, and whether the result is
headcount only or true performance.

## MCP Server

The MCP server is a tool interface for external agent hosts such as Claude
Desktop, Claude Code, or any MCP-compatible LLM host. The normal browser chat
still uses `POST /chat`; MCP is for agents that need to inspect, plan, validate,
and execute verified ERP queries step by step.

Start the stdio MCP server from the project root:

```powershell
python backend\mcp_server.py
```

Configure your MCP host to run that command from this project directory. MCP is
independent of the LLM provider, so Claude, OpenAI, Gemini, OpenRouter, or
another MCP host can use the same verified tools.

MCP can resolve a database from:

1. an explicit `database_path`
2. a browser `session_id`
3. the fallback `MCP_DATABASE_PATH` in `backend/.env`

```env
MCP_DATABASE_PATH=C:\absolute\path\to\database.sqlite
```

For the verified ERP path, MCP exposes small JSON-only tools that can be chained
by an agent:

```text
inspect_verified_modules
  -> detect_required_modules
  -> resolve_business_terms / resolve_entities / resolve_dates
  -> get_verified_metric / get_verified_dimension / get_verified_join_path
  -> plan_verified_erp_query
  -> compile_verified_sql
  -> validate_sql / estimate_query_cost
  -> execute_verified_sql
  -> format_result
```

There is also a convenience tool, `execute_verified_erp_query`, for one-shot
demo checks, but the composable tools are better for careful agent reasoning.

MCP safety rules:

- Tools return structured JSON, not natural-language answers.
- Tools include `status`, `confidence`, warnings, reasons, and recommended next
  tools.
- Ambiguous requests return `clarification_required` with options instead of
  running SQL.
- Only read-only `SELECT` / `WITH` queries can execute.
- SQL is validated against the verified ERP catalog, approved relationships,
  canonical DDL context, and plan-aware safety checks before execution.
- MCP never exposes unrestricted raw SQL execution.

MCP also keeps the lower-level database tools for inspection and safe querying:
`inspect_schema`, `analyze_question`, `validate_sql`, `dry_run_query`, and
`execute_read_query`.

## Redis Cache

Redis is optional. It is used only for final answer caching, so repeated
questions on the same database can skip the LLM call.

Install/start Redis locally with Docker:

```bash
docker run -d --name sql-assistant-redis --restart unless-stopped -p 6379:6379 redis:7
```

Verify Redis is running:

```bash
docker ps
docker exec sql-assistant-redis redis-cli ping
```

Expected output:

```text
PONG
```

Then set this in `backend/.env`:

```env
REDIS_URL=redis://localhost:6379/0
CACHE_TTL_SECONDS=3600
```

When Redis is unavailable or `REDIS_URL` is blank, the app continues without
caching. Cache entries are separated by question, database fingerprint,
provider, and model.

Backend logs show the cache behavior:

```text
Redis connected
Cache MISS
Cache STORE
Cache HIT
```

To verify caching:

1. Upload a database.
2. Ask a question. The backend should log `Cache MISS`, call the LLM, then log
   `Cache STORE`.
3. Ask the same question again. The backend should log `Cache HIT`; no LLM call
   is needed.
4. Upload a different database and ask the same question. It should log
   `Cache MISS` because the database fingerprint changed.

Important:

- Put real keys only in `backend/.env`.
- Do not put real keys in `backend/.env.example`.
- `backend/.env` is ignored by Git.

## MySQL And SQL Server

The Docker setup can run Redis, MySQL 8, and SQL Server 2022:

```powershell
docker compose up -d
docker compose ps
```

MySQL is used for MySQL `.sql` dumps and for connecting to an existing allowed
database. Upload mode streams the dump without loading the full file into
memory. Connect mode attaches an already populated configured database within
seconds.

Set MySQL values in `backend/.env`:

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USERNAME=root
MYSQL_PASSWORD=ChangeThisStrongPassword123!
MYSQL_ALLOWED_DATABASES=
```

Leave `MYSQL_ALLOWED_DATABASES` blank in local development to list available
databases. In a manager/demo environment, set it to a comma-separated allowlist.

## SQL Server Live Imports

Large SQL Server `.sql` dumps can be imported once into SQL Server and queried
live instead of being converted to SQLite.

Set these in `backend/.env`:

```env
SQLSERVER_IMPORT_SQL=1
MSSQL_SA_PASSWORD=ChangeThisStrongPassword123!
MSSQL_DATABASE=SqlAssistant
SQLSERVER_HOST=127.0.0.1
SQLSERVER_PORT=1433
SQLSERVER_DATABASE=SqlAssistant
SQLSERVER_USERNAME=sa
SQLSERVER_PASSWORD=ChangeThisStrongPassword123!
SQLSERVER_ENCRYPT=yes
SQLSERVER_TRUST_CERT=yes
REDIS_URL=redis://localhost:6379/0
```

For local backend runs, install Microsoft ODBC Driver 18 for SQL Server. Docker
provides the SQL Server database; Python still needs the local ODBC driver to
connect through `pyodbc`.

When `SQLSERVER_IMPORT_SQL=1`, uploaded `.sql` files are treated as SQL Server
dumps. The importer handles `GO` batch separators and rejects obvious
MySQL/PostgreSQL/SQLite dump syntax. Duplicate dumps are skipped by file
fingerprint.

## How To Run

Open two terminals.

**Terminal 1: backend**

```powershell
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Check backend status:

```text
http://127.0.0.1:8000/status
```

**Terminal 2: frontend**

From the project root:

```powershell
npm run dev
```

Open the Vite URL shown in the terminal. Usually:

```text
http://localhost:5173
```

Then:

1. Click the chat button.
2. Choose `Upload SQL Dump` or `Connect Existing`.
3. Upload a `.db`, `.sqlite`, `.sqlite3`, or `.sql` file, or attach an existing MySQL database.
4. Ask a question about the database.

Example:

```text
How many orders are there?
```

## Tests

Run backend tests:

```powershell
python -m pytest -q
```

Run frontend checks:

```powershell
npm run build
npm run lint
```

Optional backend syntax check:

```powershell
python -m compileall backend
```

## Common Errors

**`Uploaded file is too large`**

The default upload limit is 5 GB.

Fix:

- Increase `MAX_UPLOAD_BYTES` in `backend/.env` if your machine has enough disk
  space.
- Restart the backend after editing `.env`.

**`LLM API key is not configured`**

Fix:

- Make sure `backend/.env` exists.
- Make sure it contains `LLM_API_KEYS=your_real_key`.
- Restart the backend after editing `.env`.

**`503 UNAVAILABLE` or model high demand**

This means the LLM provider is temporarily busy.

Fix:

- Try again after a short wait.
- Keep fallback models in `LLM_FALLBACK_MODELS`.

**Frontend opens, but chat cannot connect**

Fix:

- Make sure the backend is running on `http://127.0.0.1:8000`.
- Open `http://127.0.0.1:8000/status` and confirm it says `backend: online`.

**`127.0.0.1:5173` does not open**

Vite may be listening on `localhost`.

Fix:

- Open `http://localhost:5173`.
- Or restart frontend with:

```powershell
npm run dev -- --host 127.0.0.1
```

**Uploaded database disappeared after switching browser/session**

Fix:

- Each browser session is isolated by `session_id`.
- Use the same browser session, re-upload the database, or connect to the existing MySQL database again.

**SSL or certificate error when calling the LLM**

Fix:

```powershell
pip install -r requirements.txt
```

The project includes `python-certifi-win32` and `truststore` so Python can use trusted Windows certificates.

**Port 8000 already in use**

Fix:

- Stop the old backend terminal.
- Or run the backend on another port and update `src/services/api.ts`.

## Notes

- The frontend never calls the LLM directly.
- Only the FastAPI backend talks to the LLM provider.
- Gemini, Anthropic, OpenRouter, and OpenAI-compatible providers are supported.
- MCP exposes the database tools and is intentionally separate from provider selection.
- Redis caches only final successful answers, not SQL generation.
- SQL execution is read-only and validated before running.

## Author

Mokshith Reddy Nallaballe
