"""
Tests the _get_store() singleton behavior (review findings #10 and #35) —
this had been written and reasoned through but never actually run against
a simulated failure. Uses a fake VectorStore whose .build() raises on the
first call and succeeds on the second, to prove a transient failure
doesn't permanently poison the cache.

Finding #35: _get_store() used to be a hand-rolled module-global + "is
None" guard; it's now functools.lru_cache(maxsize=1), verified separately
(see test_bedrock_rate_limiting.py's neighbor or the direct lru_cache
exception-behavior check done live before this refactor) to not cache a
raised exception — so this test now asserts against lru_cache's own
introspection API (cache_info()) instead of a hand-rolled module global
that no longer exists.
"""

import src.agents.diagnostician as diagnostician_module


class _FakeStore:
    build_attempts = 0

    def build(self, chunks):
        _FakeStore.build_attempts += 1
        if _FakeStore.build_attempts == 1:
            raise RuntimeError("simulated transient embedding failure")
        self.built_ok = True


def test_transient_build_failure_does_not_poison_the_singleton(monkeypatch):
    _FakeStore.build_attempts = 0
    diagnostician_module._get_store.cache_clear()

    def fake_chunk_corpus(_dir):
        return ["fake chunk"]

    monkeypatch.setattr("src.rag.chunker.chunk_corpus", fake_chunk_corpus)
    monkeypatch.setattr("src.rag.vector_store.VectorStore", _FakeStore)

    # first call: build() raises. The old hand-rolled bug published an
    # empty, broken store to the module global BEFORE calling build() — so
    # even though this call correctly raises, the *next* call used to
    # silently return the poisoned store instead of retrying. lru_cache
    # must show the same not-poisoned behavior.
    try:
        diagnostician_module._get_store()
        assert False, "expected the first call to raise"
    except RuntimeError:
        pass

    assert diagnostician_module._get_store.cache_info().currsize == 0, (
        "a failed build() must not leave a broken store cached"
    )

    # second call: build() succeeds this time — must actually retry, not
    # return a cached broken instance.
    store = diagnostician_module._get_store()
    assert getattr(store, "built_ok", False) is True
    assert diagnostician_module._get_store.cache_info().currsize == 1

    # third call: must return the SAME cached instance, not rebuild again —
    # this is still meant to be a singleton, not "retry every time".
    assert diagnostician_module._get_store() is store
    assert _FakeStore.build_attempts == 2, "a successful build() must be cached, not repeated on every call"

    diagnostician_module._get_store.cache_clear()  # don't leak state into other tests
