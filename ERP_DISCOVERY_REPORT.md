# ERP Discovery Report

Date: 2026-07-21

Database: `rcerp_jul8_manual_20260714_094608`

Schema fingerprint: `998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5`

Reusable JSON report:

`backend/runtime/erp_discovery/998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5.json`

The discovery report was generated from the existing schema profiler, MySQL metadata, indexes, constraints, row counts, safe samples, business catalog evidence, inferred relationships, and successful query history where available.

## Database Size

| Metric | Value |
| --- | ---: |
| Tables | 1229 |
| Columns | 18239 |
| Approx populated tables | 252 |
| Approx empty tables | 977 |
| Populated ratio | 20.5% |

## Discovered Business Modules

- Billing
- Customer
- Department
- Employee
- Inventory
- Logistics
- Payment
- Product/material
- Project
- Vendor

## Important Tables Per Module

| Module | High-signal tables |
| --- | --- |
| Billing | `boq_unit_bill_raising`, `client_bills_boq_wisedetails`, `subcontract_workorder`, `boq_dpr_main_master`, `po_form` |
| Customer | `intend_quotes`, `client_bill_count`, `client_correspondance_reply_master`, `client_correspondans_group_master`, `client_correspondans_master` |
| Department | `employee_table`, `department`, `issues_clarification_mst`, `asset_cc_po_details`, `asset_goods_receipt_cum_insp_note` |
| Employee | `emp_attendance_register`, `employee_attendance_punching_time`, `employee_work_location_weekoff`, `project_assigned_employee`, `project_module_access` |
| Inventory | `grn_master`, `inventory_master_transaction`, `fabric_goods_receipt_cum_insp_note`, `inventory_master`, `asset_reg_details_mech` |
| Logistics | `po_items`, `store_inter_change_master`, `store_intra_change_master`, `final_request_transport_add_tax`, `final_request_transport_quote_amt` |
| Payment | `boq_dpr_machinery_items`, `store_material_usage_quantity`, `po_cc_advance`, `subcontract_payment_trems`, `cc_po_details` |
| Product/material | `sub_material`, `sub_store_po_items`, `central_store_po_items`, `po_items_to_cc`, `intend_items` |
| Project | `store_usage_quantity_master`, `boq_material_dpr`, `overall_boq_dpr_work_progress`, `intend_vendors`, `sub_master_boq` |
| Vendor | `grn_tax_percentages`, `po_advance_items`, `intend_final_price`, `vendor_material_details`, `vendor_branch_details` |

## Table Classification

The JSON stores the top candidates for each class. Counts below are the number of entries listed in the bounded report, not necessarily the full database total.

| Class | Listed | Example tables |
| --- | ---: | --- |
| Master/reference tables | 100 | `subcontract_workorder`, `store_material_usage_quantity`, `po_form_items`, `central_store_po_items` |
| Transaction header tables | 86 | `goods_receipt_cum_insp_note`, `grn_master`, `store_usage_quantity_master`, `boq_dpr_main_master` |
| Transaction detail tables | 96 | `client_bills_boq_wisedetails`, `grn_final_discription`, `boq_dpr_machinery_items`, `po_advance_items` |
| Lookup tables | 100 | `boq_unit_bill_raising`, `po_terms_conditions`, `subcontract_terms_conditions`, `intend_po_list_summary` |
| Mapping/bridge tables | 100 | `attachments_sub_store`, `mis_exp`, `po_final_discription`, `potaxes_ontotal` |
| History/archive/log/temp tables | 100 | `store_transaction_logs`, `boq_dpr_main_master_approval_hist`, `intend_quotes_approve_hist` |

## Column Discovery

The profile identifies:

- Primary keys
- Declared foreign-key columns
- Entity identifier/code columns
- Entity display-name columns
- Numeric metrics
- Quantity columns
- Amount columns
- Date columns
- Status columns

These are stored in `column_inventory` in the JSON report with table, column, type, confidence, evidence, and safe sample values where available.

## Relationships

The report stores:

- Declared foreign keys from MySQL metadata.
- Inferred relationships from primary-key and shared-identifier evidence.
- Relationship confidence and evidence.
- Low-confidence relationships that should not be trusted blindly.
- Duplicate-risk relationships where a common ID column may connect to many possible tables.

Examples of duplicate-risk join columns:

- `employee_id`: used across many HR/admin/transaction tables.
- `lc_table_id`: reused across LC, GRN, credit note, debit note, and amendment tables.
- `bank_master_id`: reused across bank/LC/commission tables.

## Ambiguous Mappings

Several business concepts map to many possible tables:

- Billing
- Project
- Employee
- Department
- Customer
- Vendor
- Product/material
- Payment
- Inventory
- Logistics

This means the SQL generator should prefer populated, high-confidence, schema-retrieved tables and preserve unresolved concepts instead of guessing a table when multiple candidates are close.

## Missing Business Definitions

- Some relationships are low confidence and need business validation before KPI use.
- Some business concepts map to many possible tables and need preferred source definitions.
- Profit/loss and true cost KPIs still require confirmed revenue, cost, expense, margin, and period rules.

## Normally Excluded From Retrieval

The JSON lists tables that should usually be deprioritized unless the question specifically asks for audit/history/files/logs:

- Empty tables
- History tables
- Archive tables
- Log tables
- Attachment tables
- Temporary tables
- Approval history tables

Examples:

- `account_section`
- `additional_dwa_approval_hist`
- `admin_email_config`
- `appointment_reschedule_hist`
- `approve_attatchments_wo`
- `asset_cc_po_details`

## Current Discovery Limitations

- MySQL `TABLE_ROWS` is approximate, so exact row counts require targeted `COUNT(*)`.
- Most ERP joins are inferred because declared FK metadata is limited.
- Similar column names are treated as relationship hints, not guaranteed business-equivalent fields.
- The report is bounded to keep it readable; the full JSON carries more detail than this summary.

