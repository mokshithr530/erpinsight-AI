# 🏢 ERPInsight-AI

[![CI Test Suite](https://img.shields.io/github/actions/workflow/status/mokshithr530/erpinsight-AI/ci.yml?branch=main&label=CI&style=flat-square)](https://github.com/mokshithr530/erpinsight-AI/actions/workflows/ci.yml)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Frontend-React%2018%20%2F%20Vite-61DAFB?style=flat-square&logo=react)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.0%2B-blue?style=flat-square&logo=typescript)](https://www.typescriptlang.org/)
[![Model Context Protocol](https://img.shields.io/badge/Protocol-Model%20Context%20Protocol%20(MCP)-purple?style=flat-square)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)

**ERPInsight-AI** is a production-grade enterprise Text-to-SQL natural language assistant and Business Intelligence (BI) copilot tailored for Enterprise Resource Planning (ERP) databases. 

It allows executives, project managers, and operations leads to query complex transactional databases (Projects, Billing, Purchase Orders, Vendors, and Inventory) using plain natural language—delivering verified data tables, key findings, and confidence scores while strictly preventing SQL hallucinations and injection.

---

## ⚡ Key Enterprise Features

- **🎯 Deterministic SQL Compilation:** Pre-verified business catalog mappings for standard executive KPIs, avoiding stochastic LLM hallucinations for mission-critical numbers.
- **🛡️ AST-Level Read-Only Query Guard:** Strict AST query validation ensuring all generated queries are strictly read-only (`SELECT`), enforcing row limits and rejecting mutating commands (`UPDATE`, `DELETE`, `DROP`).
- **🔍 Intelligent Schema Pruning & Entity Resolution:** Semantic binder inspects schema metadata and resolves ambiguous entity names (e.g. vendors, project codes, materials) into database foreign keys before SQL generation.
- **🔌 Model Context Protocol (MCP) Server:** Native MCP server implementation (`backend/mcp_server.py`) enabling external AI agents (Claude Desktop, Cursor, Claude Code) to inspect schemas, plan queries, and safely query the ERP database.
- **⚡ Redis Semantic Response Cache:** Caches verified query responses to provide sub-10ms latency for frequent management questions.
- **📊 Executive-Ready Structured Reporting:** Returns clean tabular views, key bulleted observations, analytical limitations, and calibrated confidence scores instead of raw technical SQL dumps.

---

## 🏗️ System Architecture

```text
 ┌────────────────────────────────────────────────────────┐
 │       Web Browser (React 18 + Vite + Tailwind)         │
 └───────────────────────────┬────────────────────────────┘
                             │ REST / SSE API
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │               FastAPI Gateway & Router                 │
 └─────────────┬────────────────────────────┬─────────────┘
               │                            │
               ▼                            ▼
 ┌───────────────────────────┐    ┌───────────────────────┐
 │ Business Catalog & Entity │    │ Redis Response Cache  │
 │ Resolution Engine         │    └───────────────────────┘
 └─────────────┬─────────────┘
               │
               ▼
 ┌────────────────────────────────────────────────────────┐
 │  LLM Provider Router (Gemini / Claude / OpenAI / MCP)  │
 └─────────────┬──────────────────────────────────────────┘
               │
               ▼
 ┌────────────────────────────────────────────────────────┐
 │  Deterministic SQL Compiler + AST Validation Guard     │
 │  (Read-Only Enforcer • Schema Pruner • Hallucination)  │
 └─────────────┬──────────────────────────────────────────┘
               │
               ▼
 ┌────────────────────────────────────────────────────────┐
 │   Database Adapters (MySQL / SQLite / SQL Server)      │
 └────────────────────────────────────────────────────────┘
```

---

## 💼 Verified ERP Business Modules

The system comes pre-configured with approved entity mappings and calculation paths across core ERP operational modules:

| Module | Typical Management Queries |
| :--- | :--- |
| **📁 Projects** | Active running projects, total contract values, project cost comparisons, completion status |
| **💳 Billing** | Total billed amounts, milestone billing by project, unbilled work-in-progress, billing trends |
| **📦 Purchase Orders** | Total PO commitments, top POs by value, open POs per project/vendor, line item breakdowns |
| **🤝 Vendors** | Top vendors by spend, active vendor directories, vendor delivery comparisons |
| **🏭 Store & Materials** | Material issued tracking, inventory stock availability, recent store ledger movement |

---

## 📊 Sample Executive Answer Walkthrough

When a manager asks:
> *"What are the top 5 projects by billed amount?"*

The engine outputs:

```text
Executive Summary:
Top projects are concentrated in Infrastructure and Technical expansion, representing 68% of total fiscal billing.

┌───┬───────────────────────────────────┬──────────────────────┬─────────────┐
│ # │ Project Name                      │ Total Billed (USD)   │ Status      │
├───┼───────────────────────────────────┼──────────────────────┼─────────────┤
│ 1 │ Metro Rail Phase II Signaling     │ $4,250,000           │ Running     │
│ 2 │ Terminal 3 Airside Electrical     │ $3,180,000           │ Running     │
│ 3 │ Highway Corridor Surveillance     │ $2,450,000           │ Completed   │
│ 4 │ Central Data Center Tier-4        │ $1,890,000           │ Running     │
│ 5 │ Solar Microgrid Substation        │ $1,420,000           │ In-Progress │
└───┴───────────────────────────────────┴──────────────────────┴─────────────┘

Key Findings:
• Metro Rail Phase II represents the largest active billing commitment.
• Highway Corridor has concluded billing with final clearance pending.

Confidence Score: 98% (Exact entity match on projects & billing ledger tables).
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- Node.js 18+
- (Optional) Docker & Docker Compose

### 1. Local Development Setup

```bash
# Clone the repository
git clone https://github.com/mokshithr530/erpinsight-AI.git
cd erpinsight-AI

# Install backend & frontend dependencies
make install
```

### 2. Configure Environment Variables

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` to configure your preferred LLM provider:

```env
LLM_PROVIDER=auto
LLM_API_KEYS=your_api_key_here
LLM_MODEL=deepseek/deepseek-v4-pro
QUERY_ROW_LIMIT=100
MAX_UPLOAD_BYTES=5368709120
```

### 3. Launch Services

In separate terminal windows:

```bash
# Start FastAPI backend (http://localhost:8000)
make run-backend

# Start React frontend (http://localhost:5173)
make run-frontend
```

---

## 🐳 Docker Deployment

To launch the full stack (FastAPI backend, React frontend, Redis caching, and MySQL database) in one command:

```bash
make docker-up
```

To stop all services:
```bash
make docker-down
```

---

## 🔌 Running the MCP Server (Model Context Protocol)

To attach ERPInsight-AI as an MCP tool for external agent hosts (such as Claude Desktop or Claude Code):

```bash
python backend/mcp_server.py
```

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "erpinsight-ai": {
      "command": "python",
      "args": ["/path/to/erpinsight-AI/backend/mcp_server.py"]
    }
  }
}
```

---

## 🧪 Testing & Verification

Run the comprehensive pytest suite (35+ test modules validating the schema profiler, query plan guard, MCP server, and SQL compiler):

```bash
make test
```

---

## 👨‍💻 Author

**Mokshith Reddy Nallaballe**  
- GitHub: [@mokshithr530](https://github.com/mokshithr530)  
- Email: `mokshithr530@gmail.com`  

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for more information.
