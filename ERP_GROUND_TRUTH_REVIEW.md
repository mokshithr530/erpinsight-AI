# ERP Benchmark Ground-Truth Review

Date: 2026-07-21

Database: `rcerp_jul8_manual_20260714_094608`

Schema fingerprint: `998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5`

Review JSON:

`backend/runtime/ground_truth_reviews/ground_truth_review_998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5.json`

## Scope

Reviewed the development benchmark split only.

| Item | Count |
| --- | ---: |
| Development questions reviewed | 152 |
| Reference SQL executed | 103 |
| Reference SQL failures | 0 |
| Removed duplicate questions | 0 |

## Review Status

| Status | Count |
| --- | ---: |
| Answerable and verified | 125 |
| Answerable with risky join | 13 |
| Ambiguous | 2 |
| Missing business definition | 12 |

Ambiguous and missing-definition questions remain in the benchmark, but they belong to the safe-failure evaluation set. They should not be counted as normal answerable questions.

## Corrected Questions

The review found incorrect inventory ground truth in the benchmark. These were corrected before producing the final review:

| Issue | Correction |
| --- | --- |
| `inventory_master_transaction.amount` does not exist | Replaced inventory amount cases with real quantity fields |
| `inventory_master_transaction.qty` does not exist | Replaced with `inventory_master_transaction.quantity` |
| `inventory_master_transaction.transaction_type` does not exist | Replaced with `inventory_master_transaction.is_it_inward` |
| `inventory_master_transaction.transaction_date` does not exist | Replaced with `inventory_master_transaction.trans_date` |
| `inventory_master_transaction.rate` does not exist | Marked average inventory rate as unsupported by the database |
| `inventory_master.amount/rate/quantity` do not exist | Replaced with `inventory_master.available_quantity` where appropriate |

After correction:

- All development expected tables exist.
- All development expected columns exist in at least one expected table.
- All expected join columns exist on their respective tables.
- All provided reference SQL validated and executed.

## Verified Questions

`125` development questions are fully verified as normal answerable questions.

Examples:

- `erp-001-project-count`: How many projects are listed in the ERP?
- `erp-002-project-top-value`: Show the top 5 projects by project value.
- `erp-006-billing-by-project`: Compare billing across projects and show the top 10.
- `erp-010-vendor-po-value`: Which vendor has the highest purchase value?
- `erp-016-department-headcount`: Show employee count by department.

## Risky Joins

`13` answerable questions are valid but have join-multiplication risk. These should be evaluated separately or require careful aggregation grain.

Main pattern:

- Fact/detail tables joined to project, employee, material, or workorder tables.
- Joining before grouping can multiply rows if the relationship is not one-to-one at the selected grain.

Examples:

- `erp-014-material-issued-last-30`: store material issue details joined to issue master and project.
- `erp-015-material-by-name`: issue details joined to material and project.
- `erp-019-workorder-project-value`: workorders joined to projects.
- `erp-153-project-po-vs-billing`: purchase values and billing values compared by project.
- `erp-155-employee-attendance-department`: attendance joined through employee to department.
- `erp-177-project-rdss-vizag-material-receipts`: GRN material receipt values joined to project/material.
- `erp-181-material-price-variance`: issue detail pricing joined to material.

Expected handling:

- Aggregate at the source-table grain first when needed.
- Join dimension/master tables after aggregation where possible.
- Do not sum values after a many-to-many style join unless the grain is verified.

## Ambiguous Questions

These should be safe-failure or clarification cases, not normal answerable cases:

- `erp-021-ambiguous-performance`: Which department performs best?
- `erp-182-billing-duplicate-risk`: Show billing by project location.

Reason:

- The requested metric or mapping is not uniquely defined by the database metadata.

## Missing Business Definitions

These should stay in the safe-failure set:

- `erp-163-inventory-rate-average`: inventory rate column is unavailable.
- `erp-169-payroll-no-net-pay`: net payroll requires a confirmed payroll run/source table.
- `erp-170-collection-no-rule`: collection/outstanding definition must confirm receipt columns.
- `erp-183-assets-underutilized`: asset utilization definition is missing.
- `erp-185-safe-profit-by-project`: profit requires cost definition.
- `erp-186-safe-loss-last-year`: loss requires cost/revenue period rules.
- `erp-187-safe-margin-vendor`: margin requires cost and sales definition.
- `erp-189-safe-employee-productivity`: productivity metric is undefined.
- `erp-190-safe-project-risk-score`: risk score definition is missing.
- `erp-191-safe-customer-satisfaction`: satisfaction metric is missing.
- `erp-193-safe-cash-collected`: cash collection source must be confirmed.
- `erp-194-safe-timesheet-hours`: timesheet hours source is not confirmed.

## Unavailable Data

No development questions remain marked as unavailable because of missing tables or missing expected columns after correction.

## Uncertain Metrics

One answerable question still needs special attention:

- `erp-181-material-price-variance`: the benchmark expects price variance from `base_charge`. The metric is available, but the SQL should compute `MAX(base_charge) - MIN(base_charge)` or equivalent and group by material. This is sensitive to aggregation grain.

## Ground-Truth Rule Going Forward

For optimization:

- Count `answerable_verified` as normal answerable ground truth.
- Count `answerable_with_risky_join` separately and inspect aggregation grain.
- Count `ambiguous` and `missing_business_definition` only in safe-failure accuracy.
- Do not optimize against the holdout split by changing its expected answers.

