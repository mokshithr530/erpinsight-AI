import copy
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


VERIFIED_CATALOG_DIR = Path(__file__).resolve().parent / "semantic_catalogs"
ERP_CATALOG_CONTRACT_VERSION = "erp-catalog-contract:v1"
SUPPORTED_VERIFICATION_STATUSES = {
    "candidate",
    "unresolved",
    "verified",
    "verified_with_context",
}
CATALOG_SECTIONS = (
    "business_concepts",
    "entity_types",
    "important_tables",
    "metrics",
    "dimensions",
    "date_fields",
    "status_fields",
    "join_paths",
    "business_formulas",
    "duplicate_risk_warnings",
    "excluded_from_retrieval",
    "unresolved_definitions",
)
COMMON_MAPPING_FIELDS = (
    "source_of_evidence",
    "confidence",
    "verification_status",
    "applicable_module",
    "alternative_candidates",
    "ambiguity_notes",
)
BUSINESS_DEFINITION_REQUIRED_FIELDS = (
    "concept_id",
    "source_columns",
    "metric_definition",
    "formula",
    "aggregation",
    "data_grain",
    "date_field",
    "status_field",
    "valid_status_values",
    "null_handling",
    "approved_joins",
    "duplicate_risk_rules",
    "reference_sql",
    "expected_output",
)


class VerifiedCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogIssue:
    severity: str
    code: str
    message: str
    section: str | None = None
    mapping_id: str | None = None


@dataclass
class CatalogValidationResult:
    valid: bool = True
    compatible: bool = True
    catalog_contract_version: str | None = None
    catalog_version: str | None = None
    erp_product: str | None = None
    erp_schema_version: str | None = None
    schema_fingerprint: str | None = None
    verified_mapping_count: int = 0
    candidate_mapping_count: int = 0
    unresolved_mapping_count: int = 0
    issues: list[CatalogIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [
            issue.message
            for issue in self.issues
            if issue.severity == "error"
        ]

    @property
    def warnings(self) -> list[str]:
        return [
            issue.message
            for issue in self.issues
            if issue.severity == "warning"
        ]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["errors"] = self.errors
        payload["warnings"] = self.warnings
        return payload


def catalog_path(schema_fingerprint: str) -> Path:
    return VERIFIED_CATALOG_DIR / f"{schema_fingerprint}.json"


def load_verified_semantic_catalog(
    schema_fingerprint: str,
    *,
    schema: dict[str, Any] | None = None,
    strict: bool = False,
) -> dict[str, Any] | None:
    if os.getenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG"):
        return None
    path = catalog_path(schema_fingerprint)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if strict:
            raise VerifiedCatalogError(
                f"Verified ERP catalog could not be loaded: {error}"
            ) from error
        return None
    definitions_file = payload.get("business_definitions_file")
    if definitions_file:
        definitions_path = VERIFIED_CATALOG_DIR / Path(definitions_file).name
        try:
            definitions = json.loads(
                definitions_path.read_text(encoding="utf-8")
            )
            if not isinstance(definitions, list):
                raise ValueError("business definitions must be a JSON list")
            payload["business_definitions"] = definitions
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if strict:
                raise VerifiedCatalogError(
                    f"Verified ERP business definitions could not be loaded: {error}"
                ) from error
            return None
    evidence_file = payload.get("relationship_evidence_file")
    if evidence_file:
        evidence_path = VERIFIED_CATALOG_DIR / Path(evidence_file).name
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(evidence, list):
                raise ValueError("relationship evidence must be a JSON list")
            payload["relationship_evidence"] = evidence
            _apply_relationship_evidence(payload)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if strict:
                raise VerifiedCatalogError(
                    f"Verified ERP relationship evidence could not be loaded: {error}"
                ) from error
            return None

    normalized = normalize_verified_semantic_catalog(payload)
    validation = validate_verified_semantic_catalog(
        normalized,
        expected_schema_fingerprint=schema_fingerprint,
        schema=schema,
    )
    if strict and not validation.valid:
        raise VerifiedCatalogError(
            "Verified ERP catalog validation failed: "
            + "; ".join(validation.errors)
        )
    normalized["validation"] = validation.to_dict()
    return normalized


def normalize_verified_semantic_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    normalized.setdefault(
        "catalog_contract_version",
        ERP_CATALOG_CONTRACT_VERSION,
    )
    normalized.setdefault("authority", "verified_erp_primary")
    normalized.setdefault("fallback_policy", "offline_discovery_only")

    for section in CATALOG_SECTIONS:
        items = normalized.get(section)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item.setdefault("mapping_id", _mapping_id(section, item, index))
            item["confidence_score"] = _confidence_score(item.get("confidence"))
            evidence = item.get("source_of_evidence")
            if isinstance(evidence, list):
                item["evidence_sources"] = [
                    str(value).strip()
                    for value in evidence
                    if str(value).strip()
                ]
            elif evidence:
                item["evidence_sources"] = [str(evidence).strip()]
            else:
                item["evidence_sources"] = []
    for index, item in enumerate(normalized.get("business_definitions", [])):
        if not isinstance(item, dict):
            continue
        item.setdefault(
            "mapping_id",
            _mapping_id("business_definitions", item, index),
        )
        item["confidence_score"] = _confidence_score(item.get("confidence"))
    return normalized


def validate_verified_semantic_catalog(
    payload: dict[str, Any],
    *,
    expected_schema_fingerprint: str | None = None,
    schema: dict[str, Any] | None = None,
) -> CatalogValidationResult:
    result = CatalogValidationResult(
        catalog_contract_version=payload.get("catalog_contract_version"),
        catalog_version=payload.get("catalog_version"),
        erp_product=payload.get("erp_product"),
        erp_schema_version=payload.get("erp_schema_version"),
        schema_fingerprint=payload.get("schema_fingerprint"),
    )

    _validate_header(payload, expected_schema_fingerprint, result)
    _validate_sections(payload, result)
    _validate_business_definitions(payload, result)
    _validate_relationship_evidence(payload, result)
    if schema is not None:
        _validate_schema_compatibility(payload, schema, result)

    result.valid = not result.errors
    result.compatible = not any(
        issue.severity == "error" and issue.code.startswith("schema_")
        for issue in result.issues
    )
    return result


def table_exists(schema: dict[str, Any], table: str | None) -> bool:
    return bool(table and table in schema)


def column_exists(schema: dict[str, Any], table: str | None, column: str | None) -> bool:
    if not table_exists(schema, table) or not column:
        return False
    return column in {
        item.get("column_name") or item.get("name") or item.get("COLUMN_NAME")
        for item in schema.get(table, {}).get("columns", [])
    }


def _validate_header(
    payload: dict[str, Any],
    expected_schema_fingerprint: str | None,
    result: CatalogValidationResult,
) -> None:
    required = (
        "catalog_name",
        "catalog_version",
        "catalog_contract_version",
        "erp_product",
        "erp_schema_version",
        "schema_fingerprint",
        "source_evidence",
    )
    for field_name in required:
        if payload.get(field_name) in (None, "", []):
            _issue(
                result,
                "error",
                "missing_header",
                f"Verified ERP catalog is missing required header field '{field_name}'.",
            )

    if (
        payload.get("catalog_contract_version")
        != ERP_CATALOG_CONTRACT_VERSION
    ):
        _issue(
            result,
            "error",
            "unsupported_contract",
            "Verified ERP catalog contract version is not supported.",
        )
    if (
        expected_schema_fingerprint
        and payload.get("schema_fingerprint") != expected_schema_fingerprint
    ):
        _issue(
            result,
            "error",
            "schema_fingerprint_mismatch",
            "Verified ERP catalog schema fingerprint does not match the connected schema.",
        )


def _validate_sections(
    payload: dict[str, Any],
    result: CatalogValidationResult,
) -> None:
    seen_ids = set()
    for section in CATALOG_SECTIONS:
        items = payload.get(section)
        if not isinstance(items, list):
            _issue(
                result,
                "error",
                "invalid_section",
                f"Catalog section '{section}' must be a list.",
                section=section,
            )
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                _issue(
                    result,
                    "error",
                    "invalid_mapping",
                    f"Catalog section '{section}' contains a non-object mapping.",
                    section=section,
                )
                continue
            mapping_id = item.get("mapping_id") or _mapping_id(section, item, index)
            if mapping_id in seen_ids:
                _issue(
                    result,
                    "error",
                    "duplicate_mapping_id",
                    f"Catalog mapping id '{mapping_id}' is duplicated.",
                    section=section,
                    mapping_id=mapping_id,
                )
            seen_ids.add(mapping_id)
            for field_name in COMMON_MAPPING_FIELDS:
                if field_name not in item:
                    _issue(
                        result,
                        "error",
                        "missing_mapping_metadata",
                        f"Catalog mapping '{mapping_id}' is missing '{field_name}'.",
                        section=section,
                        mapping_id=mapping_id,
                    )
            status = str(item.get("verification_status") or "").lower()
            if status not in SUPPORTED_VERIFICATION_STATUSES:
                _issue(
                    result,
                    "error",
                    "invalid_verification_status",
                    f"Catalog mapping '{mapping_id}' has unsupported verification status '{status}'.",
                    section=section,
                    mapping_id=mapping_id,
                )
            confidence = item.get("confidence")
            if not _valid_confidence(confidence):
                _issue(
                    result,
                    "error",
                    "invalid_confidence",
                    f"Catalog mapping '{mapping_id}' has invalid confidence.",
                    section=section,
                    mapping_id=mapping_id,
                )
            if status.startswith("verified"):
                result.verified_mapping_count += 1
            elif status == "candidate":
                result.candidate_mapping_count += 1
            elif status == "unresolved":
                result.unresolved_mapping_count += 1


def _validate_business_definitions(
    payload: dict[str, Any],
    result: CatalogValidationResult,
) -> None:
    definitions = payload.get("business_definitions")
    if not isinstance(definitions, list) or not definitions:
        if not payload.get("poc_scope"):
            return
        _issue(
            result,
            "error",
            "missing_business_definitions",
            "Verified ERP catalog must contain business definitions.",
            section="business_definitions",
        )
        return
    seen = set()
    for item in definitions:
        if not isinstance(item, dict):
            _issue(
                result,
                "error",
                "invalid_business_definition",
                "Business definitions must be objects.",
                section="business_definitions",
            )
            continue
        concept_id = item.get("concept_id")
        if not concept_id or concept_id in seen:
            _issue(
                result,
                "error",
                "invalid_business_definition_id",
                f"Business definition id '{concept_id}' is missing or duplicated.",
                section="business_definitions",
            )
        seen.add(concept_id)
        for field_name in BUSINESS_DEFINITION_REQUIRED_FIELDS:
            if field_name not in item:
                _issue(
                    result,
                    "error",
                    "incomplete_business_definition",
                    f"Business definition '{concept_id}' is missing '{field_name}'.",
                    section="business_definitions",
                    mapping_id=concept_id,
                )
        if item.get("verification_status") != "verified":
            _issue(
                result,
                "error",
                "unverified_business_definition",
                f"Business definition '{concept_id}' is not verified.",
                section="business_definitions",
                mapping_id=concept_id,
            )
        sql = str(item.get("reference_sql") or "").strip().lower()
        if not sql.startswith(("select ", "with ")):
            _issue(
                result,
                "error",
                "unsafe_reference_sql",
                f"Business definition '{concept_id}' lacks read-only reference SQL.",
                section="business_definitions",
                mapping_id=concept_id,
            )


def _relationship_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        str(item.get(field) or "")
        for field in (
            "left_table",
            "left_column",
            "right_table",
            "right_column",
        )
    )


def _apply_relationship_evidence(payload: dict[str, Any]) -> None:
    evidence_by_key = {
        _relationship_key(item): item
        for item in payload.get("relationship_evidence", [])
        if isinstance(item, dict)
    }
    for relationship in payload.get("join_paths", []):
        evidence = evidence_by_key.get(_relationship_key(relationship))
        approved = bool(evidence and evidence.get("runtime_approved"))
        relationship["runtime_approved"] = approved
        if not approved:
            relationship["verification_status"] = "candidate"
            continue
        for field in (
            "evidence_id",
            "observed_cardinality",
            "left_null_rate",
            "right_null_rate",
            "left_key_uniqueness",
            "right_key_uniqueness",
            "distinct_key_overlap",
            "orphan_left_rows",
            "join_amplification",
            "duplicate_amplification_risk",
            "pre_aggregation_requirements",
        ):
            relationship[field] = copy.deepcopy(evidence.get(field))


def _validate_relationship_evidence(
    payload: dict[str, Any],
    result: CatalogValidationResult,
) -> None:
    if not payload.get("poc_scope"):
        return
    evidence = payload.get("relationship_evidence")
    if not isinstance(evidence, list):
        _issue(
            result,
            "error",
            "missing_relationship_evidence",
            "Verified ERP catalog must contain relationship evidence.",
            section="relationship_evidence",
        )
        return
    evidence_by_key = {
        _relationship_key(item): item
        for item in evidence
        if isinstance(item, dict)
    }
    required = {
        "observed_cardinality",
        "left_null_rate",
        "right_null_rate",
        "left_key_uniqueness",
        "right_key_uniqueness",
        "distinct_key_overlap",
        "orphan_left_rows",
        "join_amplification",
        "duplicate_amplification_risk",
        "pre_aggregation_requirements",
        "runtime_approved",
    }
    for relationship in payload.get("join_paths", []):
        key = _relationship_key(relationship)
        item = evidence_by_key.get(key)
        label = f"{'.'.join(key[:2])} -> {'.'.join(key[2:])}"
        if not item:
            _issue(
                result,
                "error",
                "missing_relationship_evidence",
                f"Relationship {label} has no evidence.",
                section="relationship_evidence",
            )
            continue
        missing = sorted(required - item.keys())
        if missing:
            _issue(
                result,
                "error",
                "incomplete_relationship_evidence",
                f"Relationship evidence '{item.get('evidence_id')}' is missing: {', '.join(missing)}.",
                section="relationship_evidence",
            )
        if relationship.get("verification_status", "").startswith("verified"):
            if not item.get("runtime_approved"):
                _issue(
                    result,
                    "error",
                    "unapproved_runtime_relationship",
                    f"Verified relationship '{item.get('evidence_id')}' is not runtime approved.",
                    section="relationship_evidence",
                )
            if item.get("observed_cardinality") != "many_to_one":
                _issue(
                    result,
                    "error",
                    "cardinality_mismatch",
                    f"Runtime relationship '{item.get('evidence_id')}' is not observed as many-to-one.",
                    section="relationship_evidence",
                )
            if float(item.get("right_key_uniqueness") or 0) < 0.999:
                _issue(
                    result,
                    "error",
                    "non_unique_runtime_key",
                    f"Runtime relationship '{item.get('evidence_id')}' has a non-unique right key.",
                    section="relationship_evidence",
                )


def _validate_schema_compatibility(
    payload: dict[str, Any],
    schema: dict[str, Any],
    result: CatalogValidationResult,
) -> None:
    for item in payload.get("business_concepts", []):
        _require_table(result, schema, "business_concepts", item, item.get("preferred_table"))
        _require_column(
            result,
            schema,
            "business_concepts",
            item,
            item.get("preferred_table"),
            item.get("preferred_column"),
        )
        _require_column(
            result,
            schema,
            "business_concepts",
            item,
            item.get("preferred_table"),
            item.get("display_column"),
        )

    for item in payload.get("entity_types", []):
        for table in item.get("master_tables", []):
            _require_table(result, schema, "entity_types", item, table)
        for value in [
            *item.get("identifier_columns", []),
            *item.get("display_columns", []),
        ]:
            _require_qualified_column(result, schema, "entity_types", item, value)

    for item in payload.get("important_tables", []):
        table = item.get("table")
        _require_table(result, schema, "important_tables", item, table)
        for column in item.get("columns", []):
            _require_column(result, schema, "important_tables", item, table, column)

    for section in ("metrics", "dimensions", "date_fields", "status_fields"):
        for item in payload.get(section, []):
            table = item.get("table") or item.get("preferred_table")
            column = item.get("column") or item.get("preferred_column")
            _require_table(result, schema, section, item, table)
            _require_column(result, schema, section, item, table, column)

    for item in payload.get("join_paths", []):
        _require_column(
            result,
            schema,
            "join_paths",
            item,
            item.get("left_table"),
            item.get("left_column"),
        )
        _require_column(
            result,
            schema,
            "join_paths",
            item,
            item.get("right_table"),
            item.get("right_column"),
        )

    for item in payload.get("business_formulas", []):
        table = item.get("preferred_table")
        column = item.get("preferred_column")
        if table:
            _require_table(result, schema, "business_formulas", item, table)
        if table and column:
            _require_column(
                result,
                schema,
                "business_formulas",
                item,
                table,
                column,
            )
    for item in payload.get("business_definitions", []):
        for value in item.get("source_columns", []):
            _require_qualified_column(
                result,
                schema,
                "business_definitions",
                item,
                value,
            )
        for value in (item.get("date_field"), item.get("status_field")):
            if value:
                _require_qualified_column(
                    result,
                    schema,
                    "business_definitions",
                    item,
                    value,
                )


def _require_qualified_column(
    result: CatalogValidationResult,
    schema: dict[str, Any],
    section: str,
    item: dict[str, Any],
    value: str,
) -> None:
    if "." not in str(value):
        _issue(
            result,
            "error",
            "schema_invalid_qualified_column",
            f"Catalog reference '{value}' must be qualified as table.column.",
            section=section,
            mapping_id=item.get("mapping_id"),
        )
        return
    table, column = value.split(".", 1)
    _require_column(result, schema, section, item, table, column)


def _require_table(
    result: CatalogValidationResult,
    schema: dict[str, Any],
    section: str,
    item: dict[str, Any],
    table: str | None,
) -> None:
    if table and not table_exists(schema, table):
        _issue(
            result,
            "error",
            "schema_missing_table",
            f"Verified catalog table '{table}' does not exist in the connected ERP schema.",
            section=section,
            mapping_id=item.get("mapping_id"),
        )


def _require_column(
    result: CatalogValidationResult,
    schema: dict[str, Any],
    section: str,
    item: dict[str, Any],
    table: str | None,
    column: str | None,
) -> None:
    if table and column and not column_exists(schema, table, column):
        _issue(
            result,
            "error",
            "schema_missing_column",
            f"Verified catalog column '{table}.{column}' does not exist in the connected ERP schema.",
            section=section,
            mapping_id=item.get("mapping_id"),
        )


def _issue(
    result: CatalogValidationResult,
    severity: str,
    code: str,
    message: str,
    *,
    section: str | None = None,
    mapping_id: str | None = None,
) -> None:
    result.issues.append(
        CatalogIssue(
            severity=severity,
            code=code,
            message=message,
            section=section,
            mapping_id=mapping_id,
        )
    )


def _mapping_id(section: str, item: dict[str, Any], index: int) -> str:
    if section == "join_paths":
        identity = "-".join(
            str(item.get(field) or "")
            for field in (
                "left_table",
                "left_column",
                "right_table",
                "right_column",
            )
        )
    else:
        identity = next(
            (
                str(item.get(field))
                for field in (
                    "concept",
                    "entity_type",
                    "metric",
                    "dimension",
                    "formula",
                    "table",
                    "relationship",
                    "pattern",
                )
                if item.get(field)
            ),
            str(index),
        )
        if item.get("table") and item.get("column"):
            identity = f"{identity}-{item['table']}-{item['column']}"
    safe = "".join(
        character if character.isalnum() else "-"
        for character in identity.lower()
    )
    return f"{section}:{'-'.join(part for part in safe.split('-') if part)}"


def _confidence_score(value: Any) -> int:
    labels = {
        "high": 90,
        "medium": 70,
        "low": 45,
    }
    if isinstance(value, str):
        return labels.get(value.strip().lower(), 0)
    if not isinstance(value, (int, float)):
        return 0
    number = float(value)
    if 0 <= number <= 1:
        number *= 100
    return max(0, min(100, int(round(number))))


def _valid_confidence(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"high", "medium", "low"}
    return isinstance(value, (int, float)) and 0 <= float(value) <= 100
