"""
Tests the _get_store() singleton fix (review finding #10) — this had been
written and reasoned through but never actually run against a simulated
failure. Uses a fake VectorStore whose .build() raises on the first call
and succeeds on the second, to prove a transient failure doesn't
permanently poison the cache.
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
    monkeypatch.setattr(diagnostician_module, "_store", None)

    def fake_chunk_corpus(_dir):
        return ["fake chunk"]

    monkeypatch.setattr("src.rag.chunker.chunk_corpus", fake_chunk_corpus)
    monkeypatch.setattr("src.rag.vector_store.VectorStore", _FakeStore)

    # first call: build() raises. The old bug published an empty, broken
    # store to the module global BEFORE calling build() — so even though
    # this call correctly raises, the *next* call used to silently return
    # the poisoned store instead of retrying.
    try:
        diagnostician_module._get_store()
        assert False, "expected the first call to raise"
    except RuntimeError:
        pass

    assert diagnostician_module._store is None, (
        "a failed build() must not leave a broken store cached in the module global"
    )

    # second call: build() succeeds this time — must actually retry, not
    # return a cached broken instance.
    store = diagnostician_module._get_store()
    assert getattr(store, "built_ok", False) is True
    assert diagnostician_module._store is store
