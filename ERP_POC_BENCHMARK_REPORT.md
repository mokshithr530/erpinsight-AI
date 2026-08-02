# ERP POC Benchmark Report

Date: 2026-07-25

## Frozen Benchmark

- Version: `erp-accuracy-benchmark:v2`
- Database: `rcerp_jul8_manual_20260714_094608`
- Schema fingerprint: `998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5`
- Benchmark SHA-256: `92f4425fcb37bd5b3dde3be9fa51e2fa3cd2dc0625fb5e769506aa7357983476`
- Holdout IDs SHA-256: `eb0b91fab00fd0c0dfb3c8421e7e16b419a2ed24160eaa1480ddee6ae38ac391`
- Total questions: 202
- Development: 152
- Holdout: 50
- Answerable with executed reference SQL: 174
- Clarification or safe-failure cases: 28

The frozen item contract stores the expected interpretation, verified query
plan, reference SQL, verified expected result, and clarification expectation.
All 174 answerable reference queries passed read-only validation and executed
against the target ERP database before the benchmark was frozen.

## Coverage

The benchmark covers simple lookups, counts, totals, averages, minimum and
maximum, rankings, grouping, comparisons, entity filters, date ranges,
calendar and financial years, status filters, trends, cross-module questions,
zero-result cases, ambiguous questions, and unsupported definitions.

## Production Run

- Provider: OpenRouter
- Model: `deepseek/deepseek-v4-pro`
- Completed: 202 of 202
- Status: complete
- Complete holdout: 50 of 50
- Completed development: 152 of 152

The interrupted development shards were resumed from their checkpoints after
the Docker data disk was rebuilt and the verified ERP dump was restored. The
restored schema fingerprint exactly matched the frozen benchmark fingerprint.

## Measured Accuracy

These figures describe all 202 production-model questions.

| Stage | Accuracy |
| --- | ---: |
| Semantic parsing | 83.99% |
| Semantic binding | 75.29% |
| Entity resolution | 99.01% |
| Schema retrieval | 44.31% |
| Metric selection | 54.46% |
| Column selection | 37.54% |
| Relationship selection | 88.61% |
| Query planning | 45.79% |
| SQL generation | 54.46% |
| SQL execution | 86.63% |
| Final answer | 16.34% |
| Clarification | 61.88% |
| Safe failure | 95.54% |
| Strict end to end | 8.42% |

The complete 50-question holdout strict end-to-end accuracy was 8.00%.

## Latency

| Measurement | Latency |
| --- | ---: |
| Mean | 37.61 seconds |
| P50 | 17.04 seconds |
| P95 | 156.42 seconds |
| Maximum | 415.79 seconds |

The 90-second frontend budget is exceeded at the measured P95. Several long
calls were model-provider latency outliers; the infrastructure failure was
recorded separately.

## Main Failures

| Failure category | Questions |
| --- | ---: |
| Result interpretation/exact result mismatch | 167 |
| Column selection | 133 |
| Query planning | 122 |
| Schema retrieval | 114 |
| SQL generation | 98 |

The key finding is that SQL execution success is not answer accuracy:
86.63% of questions executed SQL, but only 16.34% returned the verified result
and 8.42% passed every required stage.

## Artifacts

- Frozen benchmark: `backend/benchmarks/erp_management_benchmark_v2.json`
- Ground-truth additions: `backend/erp_benchmark_ground_truth.py`
- Freezer: `backend/erp_benchmark_freezer.py`
- Runner: `backend/erp_benchmark_runner.py`
- Shard merger: `backend/erp_benchmark_merge.py`
- Partial trace: `backend/runtime/benchmark_traces/poc_step4_production_partial_190.json`
- Complete holdout trace: `backend/runtime/benchmark_traces/poc_step4_production_holdout_v2.json`
- Complete development trace: `backend/runtime/benchmark_traces/poc_step4_production_development_complete.json`
- Complete 202-question trace: `backend/runtime/benchmark_traces/poc_step4_production_complete_202.json`

## Completion Status

The frozen benchmark and production-model baseline are complete. The next work
should target the measured retrieval, column-selection, planning, and result
interpretation failures rather than changing the benchmark ground truth.
