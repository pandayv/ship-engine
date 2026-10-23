"""
Tests the _get_store() singleton behavior. Uses a fake VectorStore whose .build() raises on the
first call and succeeds on the second, to prove a transient failure doesn't permanently poison
the cache.

_get_store() is functools.lru_cache(maxsize=1), which does not cache a raised exception, so
these tests assert against lru_cache's own introspection API (cache_info()).
"""

import src.agents.detector as detector_module


class _FakeStore:
    build_attempts = 0

    def build(self, chunks, cache_path=None):
        _FakeStore.build_attempts += 1
        if _FakeStore.build_attempts == 1:
            raise RuntimeError("simulated transient embedding failure")
        self.built_ok = True


def test_transient_build_failure_does_not_poison_the_singleton(monkeypatch):
    _FakeStore.build_attempts = 0
    detector_module._get_store.cache_clear()

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
        detector_module._get_store()
        assert False, "expected the first call to raise"
    except RuntimeError:
        pass

    assert detector_module._get_store.cache_info().currsize == 0, (
        "a failed build() must not leave a broken store cached"
    )

    # second call: build() succeeds this time — must actually retry, not
    # return a cached broken instance.
    store = detector_module._get_store()
    assert getattr(store, "built_ok", False) is True
    assert detector_module._get_store.cache_info().currsize == 1

    # third call: must return the SAME cached instance, not rebuild again —
    # this is still meant to be a singleton, not "retry every time".
    assert detector_module._get_store() is store
    assert _FakeStore.build_attempts == 2, "a successful build() must be cached, not repeated on every call"

    detector_module._get_store.cache_clear()  # don't leak state into other tests
