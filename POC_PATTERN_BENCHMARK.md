# POC Pattern Benchmark

This benchmark focuses only on the verified ERP POC modules. It is meant to
strengthen how managers ask questions, not to verify all 1,229 ERP tables.

Benchmark file:

- `backend/benchmarks/poc_pattern_benchmark_v1.json`

## Module Readiness

| Module | Done | POC enough? | Notes |
| --- | ---: | --- | --- |
| Projects | 88% | Yes | Counts, lists, details, project value, rankings, comparisons and state/date views are covered. |
| Billing | 84% | Yes | Gross billing and bill amount by project/client/date/status are covered at billing-header grain. |
| Purchase Orders | 86% | Yes | PO count/value, vendor/project summaries, item quantity/value and date views are covered. |
| Vendors | 78% | Yes | Vendor lookup, branch/category summaries and vendor-wise PO value/count are covered. |
| Store / Material Movement | 76% | Yes | Issued/requested quantity and issue-line value by project/material/date are covered. |
| Inventory | 72% | Yes | Current stock and stock by material/store are covered. Valuation and forecasting are not covered. |
| Safe Cross-Module | 66% | Limited | Project-level Billing + PO + material movement comparisons are stronger. Profit, margin, complete cost and performance still need clarification. |

## Benchmark Shape

The benchmark has 88 manager-style questions:

- Beginner lookups and counts
- Top/bottom rankings
- Comparisons
- Monthly/yearly/date-filtered questions
- Entity-specific filters
- Alias-heavy wording
- Clarification-required questions
- Safe-refusal questions
- Safe cross-module patterns

## Phase 2 Pattern Hardening

Implemented simple manager phrasing support for the current verified modules:

- Preserves plain list limits such as "show 5 projects", "name any 10 material",
  "list any five vendors" and "show latest 5 bills".
- Routes pure material master questions to `sub_material` instead of material
  movement tables.
- Supports material master count plus sample names in one answer.
- Keeps unsupported KPI words such as loss, margin, performance and full cost on
  the clarification/safe-refusal path.

## Phase 4 LLM Polish Hardening

Implemented optional manager-friendly LLM polishing for verified ERP answers:

- Runtime SQL remains deterministic: the verified compiler still chooses tables,
  columns, joins, metrics and grain before execution.
- The LLM only receives verified facts, the execution receipt and the user
  question, then rewrites the final explanation for readability.
- `LLM_POLISH_VERIFIED_ANSWERS=1` enables the polish path. If the model fails,
  the deterministic answer is returned.
- Redis cache identity now includes the polish flag, so deterministic and
  LLM-polished answers cannot reuse the wrong cached response.
- Manager presentation phrases such as "for demo", "management review" and
  "what a manager should notice" are ignored as entity candidates.
- Real LLM smoke, using `openrouter` with `deepseek/deepseek-v4-pro`, passed
  12/12 manager-style questions and used 29,750 tokens total.

## Cross-Module Pattern Hardening

Added safer project-level patterns for verified partial metrics:

- Project value + billing + PO value + material issue value in one result.
- Billing vs PO value gap columns for comparison questions.
- Billing/material and PO/material gap columns for comparison questions.
- Manager aliases such as billed amount, procurement spend, material spent,
  material utilised and project dashboard.
- Sort handling for "high PO but low billing" and "high material spend but low
  billing" questions.

## POC Rule

An answer is considered supported only when the metric, table, columns,
relationship and aggregation grain are verified. Otherwise the assistant should
ask a clarification question or clearly say the KPI is outside the verified POC.

## Strong Areas

- Project details and project value questions
- Billing by project/client/date
- PO value/count by vendor/project
- Vendor summaries using verified master and PO data
- Material issued/requested quantity by project/material/date
- Current stock and material-level inventory views

## Still Limited

- Basic simple-question coverage is not complete yet. The parser and binder may
  understand questions like "list any 10 materials", but the verified SQL
  compiler still needs reusable count/list/details patterns for every verified
  entity so it does not fall into generic clarification.
- Profit, loss, margin and full project cost
- Generic performance or risk score
- Collections and payment reconciliation
- Inventory valuation, aging and reorder forecasting
- Material returns/cancellations where status meanings are not verified
- Broad cross-module analytics unless both sides are already verified
