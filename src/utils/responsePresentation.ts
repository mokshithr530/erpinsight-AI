import type { ChatResponse } from "../types/chat";

export type CellValue = string | number | boolean | null | undefined | object;

export type DisplayTable = {
  columns: string[];
  rows: Record<string, unknown>[];
};

export type ResponsePresentation = {
  summary: string;
  bullets: string[];
  table: DisplayTable | null;
  limitations: string[];
};

const MONEY_WORDS = ["revenue", "price", "value", "amount", "sales", "loss", "risk", "bill", "billing"];
const COUNT_WORDS = ["count", "orders", "units", "sold", "quantity", "row_count"];

function titleize(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isMoneyColumn(column: string): boolean {
  const lower = column.toLowerCase();
  return MONEY_WORDS.some((word) => lower.includes(word));
}

function isCountColumn(column: string): boolean {
  const lower = column.toLowerCase();
  return COUNT_WORDS.some((word) => lower.includes(word));
}

export function isNumericColumn(column: string): boolean {
  return isMoneyColumn(column) || isCountColumn(column) || /(qty|quantity|rate|total|balance|stock|issued|received|pending|gap|difference)/i.test(column);
}

function formatIndianCurrencyShort(amount: number): string {
  const sign = amount < 0 ? "-" : "";
  const absolute = Math.abs(amount);
  if (absolute >= 10_000_000) {
    return `${sign}₹${(absolute / 10_000_000).toFixed(2)} Cr`;
  }
  if (absolute >= 100_000) {
    return `${sign}₹${(absolute / 100_000).toFixed(2)} Lakh`;
  }
  return `${sign}₹${new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: 2,
  }).format(absolute)}`;
}

export function formatCell(column: string, value: CellValue): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "number") {
    if (isMoneyColumn(column)) {
      return formatIndianCurrencyShort(value);
    }
    if (isCountColumn(column) || Number.isInteger(value)) {
      return new Intl.NumberFormat("en-IN", {
        maximumFractionDigits: 0,
      }).format(value);
    }
    return new Intl.NumberFormat("en-IN", {
      maximumFractionDigits: 2,
    }).format(value);
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function displayColumns(columns: string[]): string[] {
  const priority = [
    "department_name",
    "employee_count",
    "client_name",
    "project_id",
    "project_name",
    "project_value",
    "project_start_date",
    "project_end_date",
    "project_state",
    "status",
    "purchase_rows",
    "total_purchase_value",
    "source_table",
    "value_source",
    "bill_count",
    "gross_bill_value",
    "bill_amount",
    "total_billing",
    "receivable_amount",
    "purchase_value",
    "purchase_minus_billing_value",
    "billing_minus_material_issue_value",
    "purchase_minus_material_issue_value",
    "material_name",
    "uom",
    "issued_quantity",
    "avg_base_charge",
    "min_base_charge",
    "max_base_charge",
    "issued_value",
    "from_date",
    "to_date",
    "product_category",
    "product",
    "category",
    "units_sold",
    "revenue",
    "order_status",
    "affected_orders",
    "revenue_at_risk",
    "average_order_item_value",
    "average_price",
  ];
  const ordered = [
    ...priority.filter((column) => columns.includes(column)),
    ...columns.filter((column) => !priority.includes(column)),
  ];
  return ordered.slice(0, 8);
}

function extractSection(answer: string, heading: string): string[] {
  const pattern = new RegExp(`${heading}\\s*\\n([\\s\\S]*?)(?:\\n\\n[A-Z][A-Za-z ]+\\n|$)`, "i");
  const match = answer.match(pattern);
  if (!match) return [];
  return match[1]
    .split("\n")
    .map((line) => line.trim().replace(/^- /, ""))
    .filter(Boolean);
}

function summaryFromAnswer(answer: string): string {
  const summary = extractSection(answer, "Short summary")[0];
  if (summary) return summary;
  return answer.split("\n")[0] || "Here is what the data shows.";
}

function naturalLinesAfterMarkdownTable(answer: string): string[] {
  const lines = answer.split("\n");
  const tableLineIndexes = lines
    .map((line, index) => ({ line: line.trim(), index }))
    .filter(({ line }) => line.startsWith("|") && line.endsWith("|"))
    .map(({ index }) => index);

  if (tableLineIndexes.length === 0) return [];

  const afterTable = lines.slice(Math.max(...tableLineIndexes) + 1);
  return afterTable
    .map((line) => line.trim().replace(/^- /, ""))
    .filter(Boolean)
    .filter((line) => !/^(note|limitations?|result|key findings|how to read this):?$/i.test(line))
    .slice(0, 5);
}

export function buildResponsePresentation(response?: ChatResponse): ResponsePresentation | null {
  if (!response?.success || !response.result) return null;

  const records = Array.isArray(response.result.records)
    ? response.result.records
    : [];
  if (records.length === 0) {
    return {
      summary: response.answer || "No matching rows were found.",
      bullets: [],
      table: null,
      limitations: ["No rows were returned for this question."],
    };
  }

  const columns = displayColumns(response.result.columns || Object.keys(records[0] ?? {}));
  const first = records[0] ?? {};
  const sectionBullets = [
    ...extractSection(response.answer, "Key findings"),
    ...extractSection(response.answer, "How to read this"),
    ...naturalLinesAfterMarkdownTable(response.answer),
  ];
  const bullets = sectionBullets.length > 0 ? sectionBullets : columns.slice(0, 4).map((column) => (
    `${titleize(column)}: ${formatCell(column, first[column] as CellValue)}`
  ));
  const limitations = extractSection(response.answer, "Limitations");

  return {
    summary: summaryFromAnswer(response.answer),
    bullets,
    table: records.length > 1
      ? {
          columns: ["rank", ...columns],
          rows: records.slice(0, 10).map((record, index) => ({
            rank: index + 1,
            ...record,
          })),
        }
      : null,
    limitations,
  };
}

export function labelColumn(column: string): string {
  return column === "rank" ? "Rank" : titleize(column);
}
