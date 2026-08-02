import time

from datetime import date
from decimal import Decimal

import response_cache as response_cache_module


class InMemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        item = self.values.get(key)
        if not item:
            return None

        value, expires_at = item
        if expires_at <= time.time():
            self.values.pop(key, None)
            return None

        return value

    def setex(self, key, ttl, value):
        self.values[key] = (value, time.time() + ttl)


def test_cache_key_uses_normalized_question_and_database_fingerprint(tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = InMemoryRedis()
    database = tmp_path / "sample.db"
    database.write_bytes(b"same database")
    response = {"success": True, "answer": "Alice", "sql": "SELECT 1"}

    cache.set("  Who   is Alice? ", database, response, "gemini", "model-a")
    cached = cache.get("who is alice?", database, "gemini", "model-a")

    assert cached == response


def test_different_database_with_same_question_misses_cache(tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = InMemoryRedis()
    database_a = tmp_path / "a.db"
    database_b = tmp_path / "b.db"
    database_a.write_bytes(b"database a")
    database_b.write_bytes(b"database b")

    cache.set("same question", database_a, {"answer": "A"}, "gemini", "model-a")

    assert cache.get("same question", database_b, "gemini", "model-a") is None


def test_different_provider_or_model_misses_cache(tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = InMemoryRedis()
    database = tmp_path / "sample.db"
    database.write_bytes(b"database")

    cache.set("same question", database, {"answer": "A"}, "gemini", "model-a")

    assert cache.get("same question", database, "anthropic", "model-a") is None
    assert cache.get("same question", database, "gemini", "model-b") is None


def test_redis_unavailable_still_returns_none_and_does_not_raise(tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = None
    database = tmp_path / "sample.db"
    database.write_bytes(b"database")

    cache.set("question", database, {"answer": "A"}, "gemini", "model-a")

    assert cache.get("question", database, "gemini", "model-a") is None


def test_cache_expires_using_ttl(monkeypatch, tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = InMemoryRedis()
    monkeypatch.setattr(response_cache_module, "CACHE_TTL_SECONDS", 1)
    database = tmp_path / "sample.db"
    database.write_bytes(b"database")

    cache.set("question", database, {"answer": "A"}, "gemini", "model-a")
    assert cache.get("question", database, "gemini", "model-a") == {"answer": "A"}

    stored_key = next(iter(cache.client.values))
    value, _expires_at = cache.client.values[stored_key]
    cache.client.values[stored_key] = (value, time.time() - 1)

    assert cache.get("question", database, "gemini", "model-a") is None


def test_corrupt_or_failing_redis_never_breaks_request(tmp_path):
    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis down")

        def setex(self, key, ttl, value):
            raise RuntimeError("redis down")

    cache = response_cache_module.ResponseCache()
    cache.client = BrokenRedis()
    database = tmp_path / "sample.db"
    database.write_bytes(b"database")

    cache.set("question", database, {"answer": "A"}, "gemini", "model-a")

    assert cache.get("question", database, "gemini", "model-a") is None


def test_cache_serializes_decimal_and_date_values(tmp_path):
    cache = response_cache_module.ResponseCache()
    cache.client = InMemoryRedis()
    database = tmp_path / "sample.db"
    database.write_bytes(b"database")
    response = {
        "success": True,
        "answer": "ok",
        "result": {
            "records": [
                {"amount": Decimal("123.45"), "bill_date": date(2026, 7, 21)},
                {"amount": Decimal("10"), "bill_date": date(2026, 7, 20)},
            ],
        },
    }

    cache.set("question", database, response, "openrouter", "deepseek")
    cached = cache.get("question", database, "openrouter", "deepseek")

    assert cached["result"]["records"] == [
        {"amount": 123.45, "bill_date": "2026-07-21"},
        {"amount": 10, "bill_date": "2026-07-20"},
    ]
