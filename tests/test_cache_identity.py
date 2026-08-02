import cache_identity


def _identity_key():
    return cache_identity.build_cache_identity(
        question="Show top 5 projects by project value",
        database_fingerprint="db-fp",
        schema_fingerprint="schema-fp",
        dialect="mysql",
        source_identity="mysql:test",
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        session_scope="session-a",
        sql="SELECT 1",
        resolution={},
    )["key"]


def test_cache_identity_separates_llm_polished_verified_answers(monkeypatch):
    monkeypatch.setattr(cache_identity, "LLM_POLISH_VERIFIED_ANSWERS", False)
    deterministic_key = _identity_key()

    monkeypatch.setattr(cache_identity, "LLM_POLISH_VERIFIED_ANSWERS", True)
    polished_key = _identity_key()

    assert deterministic_key != polished_key
