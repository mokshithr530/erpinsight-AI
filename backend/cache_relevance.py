import time


REQUIRED_METADATA_FIELDS = (
    "database_fingerprint",
    "schema_fingerprint",
    "catalog_version",
    "relationship_graph_version",
    "schema_retriever_version",
    "mapping_version",
    "question_hash",
    "normalized_intent",
    "resolution_hash",
    "provider",
    "model",
    "prompt_template_version",
    "result_shaping_version",
)


class CacheRelevance:
    def validate(self, envelope: dict, expected_metadata: dict, *, max_age_seconds: int | None = None) -> dict:
        if not isinstance(envelope, dict) or "response" not in envelope:
            return {"valid": False, "reason": "legacy_or_malformed_cache_entry"}
        metadata = envelope.get("metadata") or {}
        for field in REQUIRED_METADATA_FIELDS:
            if metadata.get(field) != expected_metadata.get(field):
                return {"valid": False, "reason": f"{field}_mismatch"}
        if metadata.get("session_scope") != expected_metadata.get("session_scope"):
            return {"valid": False, "reason": "session_scope_mismatch"}
        if expected_metadata.get("data_version") and metadata.get("data_version") != expected_metadata.get("data_version"):
            return {"valid": False, "reason": "data_version_mismatch"}
        if max_age_seconds is not None:
            created_at = envelope.get("created_at")
            if not created_at or time.time() - float(created_at) > max_age_seconds:
                return {"valid": False, "reason": "cache_entry_expired_by_policy"}
        response = envelope.get("response") or {}
        confidence = response.get("confidence") or {}
        if confidence.get("confidence_level") == "low":
            return {"valid": False, "reason": "low_confidence_legacy_answer"}
        return {"valid": True, "reason": "metadata_match"}


cache_relevance = CacheRelevance()
