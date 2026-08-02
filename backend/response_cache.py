import hashlib
import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from config import CACHE_TTL_SECONDS, LLM_MODEL, LLM_PROVIDER, REDIS_URL
from cache_identity import build_cache_identity
from cache_relevance import cache_relevance


logger = logging.getLogger(__name__)


def _json_safe(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class ResponseCache:
    def __init__(self):
        self.client = None
        if REDIS_URL:
            try:
                import redis

                self.client = redis.Redis.from_url(
                    REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
                self.client.ping()
                logger.info("Redis connected")
            except Exception as error:
                logger.warning(
                    "Redis unavailable (caching disabled): %s",
                    error,
                )
                self.client = None
        else:
            logger.info("Redis unavailable (caching disabled)")

    def normalize_question(self, question):
        return re.sub(r"\s+", " ", question.strip().lower())

    def database_fingerprint(self, database_path):
        path = Path(database_path)
        digest = hashlib.sha256()
        with path.open("rb") as database_file:
            for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _key_for_identity(self, question, database_identity, provider=None, model=None):
        normalized = self.normalize_question(question)
        identity = "|".join(
            [
                database_identity,
                normalized,
                provider or LLM_PROVIDER,
                model or LLM_MODEL,
            ]
        )
        return "sql-assistant:answer:" + hashlib.sha256(identity.encode()).hexdigest()

    def _legacy_metadata(self, question, database_identity, provider=None, model=None):
        return {
            "database_fingerprint": database_identity,
            "schema_fingerprint": database_identity,
            "catalog_version": "legacy",
            "relationship_graph_version": "legacy",
            "mapping_version": "legacy",
            "question_hash": hashlib.sha256(
                self.normalize_question(question).encode("utf-8")
            ).hexdigest(),
            "normalized_intent": "legacy",
            "resolution_hash": "legacy",
            "provider": provider or LLM_PROVIDER,
            "model": model or LLM_MODEL,
            "prompt_template_version": "legacy",
            "result_shaping_version": "legacy",
            "session_scope": None,
        }

    def identity_for(
        self,
        question,
        *,
        database_fingerprint,
        schema_fingerprint,
        dialect,
        source_identity,
        provider=None,
        model=None,
        session_scope=None,
        sql=None,
        resolution=None,
        data_version=None,
    ):
        return build_cache_identity(
            question=question,
            database_fingerprint=database_fingerprint,
            schema_fingerprint=schema_fingerprint,
            dialect=dialect,
            source_identity=source_identity,
            provider=provider or LLM_PROVIDER,
            model=model or LLM_MODEL,
            session_scope=session_scope,
            sql=sql,
            resolution=resolution,
            data_version=data_version,
        )

    def _key(self, question, database_path, provider=None, model=None):
        return self._key_for_identity(
            question,
            self.database_fingerprint(database_path),
            provider,
            model,
        )

    def get(self, question, database_path, provider=None, model=None):
        return self.get_by_identity(
            question,
            self.database_fingerprint(database_path),
            provider,
            model,
        )

    def get_by_identity(
        self,
        question,
        database_identity,
        provider=None,
        model=None,
        cache_identity=None,
        max_age_seconds=None,
    ):
        if not self.client:
            return None
        try:
            if cache_identity:
                key = cache_identity["key"]
                expected_metadata = cache_identity["metadata"]
            else:
                key = self._key_for_identity(question, database_identity, provider, model)
                expected_metadata = self._legacy_metadata(question, database_identity, provider, model)
            value = self.client.get(key)
            if not value:
                logger.info("Cache MISS")
                return None
            decoded = json.loads(value)
            relevance = cache_relevance.validate(
                decoded,
                expected_metadata,
                max_age_seconds=max_age_seconds,
            )
            if not relevance["valid"]:
                logger.info("Cache MISS (%s)", relevance["reason"])
                return None
            logger.info("Cache HIT")
            return decoded["response"]
        except Exception as error:
            logger.warning("Redis cache read failed: %s", error)
            return None

    def set(self, question, database_path, response, provider=None, model=None):
        self.set_by_identity(
            question,
            self.database_fingerprint(database_path),
            response,
            provider,
            model,
        )

    def set_by_identity(
        self,
        question,
        database_identity,
        response,
        provider=None,
        model=None,
        ttl_seconds=CACHE_TTL_SECONDS,
        cache_identity=None,
        selected_tables=None,
        selected_columns=None,
        join_path=None,
        result_signature=None,
    ):
        if not self.client:
            return
        try:
            if cache_identity:
                key = cache_identity["key"]
                metadata = dict(cache_identity["metadata"])
            else:
                key = self._key_for_identity(question, database_identity, provider, model)
                metadata = self._legacy_metadata(question, database_identity, provider, model)
            metadata.update(
                {
                    "sql": response.get("sql"),
                    "selected_tables": selected_tables or [],
                    "selected_columns": selected_columns or [],
                    "join_path": join_path or [],
                    "confidence_score": (response.get("confidence") or {}).get("confidence_score"),
                    "result_signature": result_signature,
                    "cache_policy": "ttl",
                }
            )
            envelope = {
                "metadata": _json_safe(metadata),
                "response": _json_safe(response),
                "created_at": time.time(),
            }
            self.client.setex(
                key,
                ttl_seconds,
                json.dumps(envelope),
            )
            logger.info("Cache STORE")
        except Exception as error:
            logger.warning("Redis cache write failed: %s", error)


response_cache = ResponseCache()
