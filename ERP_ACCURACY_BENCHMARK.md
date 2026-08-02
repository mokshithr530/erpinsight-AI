# ERP Accuracy Benchmark

Date: 2026-07-21

Benchmark version: `erp-accuracy-benchmark:v2`

Current production-model results and the infrastructure limitation are recorded
in `ERP_POC_BENCHMARK_REPORT.md`.

Target database:

`rcerp_jul8_manual_20260714_094608`

Schema fingerprint:

`998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5`

## Benchmark Size

| Split | Count |
| --- | ---: |
| Development | 152 |
| Holdout | 50 |
| Total | 202 |

The holdout split is deterministic and should remain unchanged during optimization.

## Coverage

Modules covered:

- Projects
- Clients
- Billing
- Invoices
- Collections and receipts
- Procurement
- Purchase orders
- Vendors
- GRN
- Materials
- Inventory
- Stores
- BOQ
- Employees
- Departments
- Attendance
- Timesheets
- Payroll

Question categories covered:

- Counts
- Totals
- Averages
- Minimum and maximum
- Top and bottom N
- Grouping
- Comparisons
- Entity-specific filters
- Multiple entities
- Date ranges
- Calendar years
- Financial years
- Status filters
- Multiple filters
- Trends
- Zero-result cases
- Cross-module questions
- Missing-data questions
- Ambiguous questions
- Unsupported business definitions

## Stored Item Contract

Every item stores:

- `question`
- `category`
- `module`
- `complexity`
- `answerable`
- `expected_intent`
- `expected_entities`
- `expected_tables`
- `expected_columns`
- `expected_joins`
- `expected_filters`
- `expected_aggregation`
- `expected_ordering`
- `reference_sql`
- `expected_result`
- `expected_clarification`
- `safe_failure`

All 174 answerable cases contain manually reviewable reference SQL and a
verified expected result. Reference SQL was validated and executed against the
target ERP database before the benchmark was frozen. The benchmark does not
use system-generated SQL as ground truth.

## Runner

Runner:

`backend/erp_benchmark_runner.py`

Example component-only run:

```bash
cd backend
python erp_benchmark_runner.py --database rcerp_jul8_manual_20260714_094608 --split development --limit 10
```

Optional full-chat run, which uses the LLM:

```bash
cd backend
python erp_benchmark_runner.py --database rcerp_jul8_manual_20260714_094608 --split development --limit 10 --run-chat
```

Optional manually written reference SQL execution:

```bash
cd backend
python erp_benchmark_runner.py --database rcerp_jul8_manual_20260714_094608 --split development --limit 10 --execute-reference-sql
```

Trace files are written to:

`backend/runtime/benchmark_traces/`

## Metrics Calculated

- End-to-end accuracy
- Parser accuracy
- Semantic-binding accuracy
- Entity-resolution accuracy
- Schema-retrieval accuracy
- Metric-selection accuracy
- Column-selection accuracy
- Relationship-selection accuracy
- Query-plan accuracy
- SQL-generation accuracy
- SQL execution success
- Final-answer accuracy against the verified result
- Clarification accuracy
- Safe-failure accuracy
- Mean, P50, P95 and maximum latency
- Accuracy by module
- Accuracy by complexity
- Accuracy by category

## Frozen Artifact

The immutable benchmark is stored at:

`backend/benchmarks/erp_management_benchmark_v2.json`

Its benchmark and holdout checksums are validated whenever it is loaded.

## Current Limitation

The holdout set must not be edited while improving parser, resolver, retrieval,
SQL generation, or repair logic. Regenerating the frozen artifact requires an
explicit ground-truth review.

## Ground-Truth Review

Frozen benchmark verification completed:

- `174` answerable cases with executed reference SQL
- `13` cases expecting clarification
- `28` total clarification or safe-failure cases
- `0` reference SQL validation or execution failures

`ERP_GROUND_TRUTH_REVIEW.md` is the earlier development-set review. The v2
frozen artifact is authoritative for the current benchmark.
