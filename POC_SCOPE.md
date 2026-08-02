# SQL Assistant POC Scope

This POC targets the fixed RC ERP schema. It does not promise coverage for all
1,229 tables. Runtime answers use verified catalog mappings and read-only SQL.

## Supported For The Demonstration

| Module | Supported questions |
| --- | --- |
| Projects | Count/list projects, project value rankings, state breakdowns, project dates and verified statuses |
| Billing | Gross billing and bill amount totals, monthly/yearly breakdowns, billing by project and verified billing statuses |
| Purchase Orders | Count/list POs, total-value rankings, vendor/project breakdowns, PO dates and verified item statuses |
| Vendors | Vendor lists and rankings using verified purchase-order data |
| Inventory and Stock | Current available quantities using verified inventory fields |
| Materials and Stores | Issued quantities, material prices and project/material/date breakdowns |
| GRN | Received quantities by project/material/date using verified receipt fields |
| Employees and Departments | Employee counts/lists, department breakdowns and verified employee statuses |
| Attendance | Basic attendance records and counts by verified employee/date relationships |

## Limited Or Clarification-First

| Module | Current boundary |
| --- | --- |
| BOQ | Basic billing-line values are available, but complex progress, balance and cross-module formulas require verification |
| Clients | Project-linked client lists and project-value views are supported; standalone CRM behaviour is not |
| Project or department performance | Requires the user to select a verified metric; no generic performance score is invented |

## Out Of Scope

- Payroll and salary calculations
- Timesheet analytics
- Collections, receipts and cash reconciliation
- Accounting-grade profit, loss, margin or project cost
- Forecasts, risk scores and unsupported recommendations
- Write operations or changes to ERP data
- Unverified tables, formulas, statuses or relationship paths
- Support guarantees for unrelated database schemas

## Accuracy Contract

An answer is supported only when the required metric, fields and relationships
are verified in the ERP catalog. Otherwise the assistant must ask for
clarification or explain that the requested definition is outside the POC.

The assistant must never replace a missing KPI with a related value, infer a
business formula from a column name, or execute write SQL.

## Verified Definition Contract

Every supported metric is defined in
`backend/semantic_catalogs/rcerp_business_definitions.json` with its physical
source columns, formula, aggregation, grain, date and status fields, observed
status values, null policy, approved joins, duplicate-risk rules, executable
reference SQL and expected output shape.

Numeric project, billing, employee and attendance status codes are observed but
their business labels are not verified. Named filters such as "active",
"approved" or "completed" therefore require clarification. The text PO-item
values `open` and `close` are verified.

Profit, loss, margin, generic performance, risk score and complete project cost
remain formally unresolved. The assistant must ask for a supported definition
instead of substituting a related metric.

## Relationship Contract

Runtime joins are restricted to relationships backed by
`backend/semantic_catalogs/rcerp_relationship_evidence.json`. Each edge records
observed cardinality, left/right uniqueness, null rates, key overlap, orphan
rows, join amplification and pre-aggregation requirements.

All currently approved edges are observed many-to-one relationships with unique
right-side keys and `1.0` left-to-right join amplification. The PO item/header
edge has 41 unmatched line rows, and the employee/department edge has 2
unmatched employee rows. Queries that must preserve these records should use a
left join. Header or master measures must never be summed after expanding into
detail rows without pre-aggregation.
