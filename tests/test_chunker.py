from pathlib import Path

from src.rag.chunker import chunk_corpus, chunk_file


def test_gdpr_art32_chunks_correctly():
    path = Path(__file__).resolve().parents[1] / "rag_corpus" / "gdpr" / "article_32.txt"
    chunks = chunk_file(path)
    assert len(chunks) >= 4  # 4 numbered paragraphs, sub-points may merge into para 1
    assert all(c.article.startswith("GDPR Article 32") for c in chunks)
    assert all("Source:" not in c.text for c in chunks)


def test_corpus_loads_both_documents():
    corpus_dir = Path(__file__).resolve().parents[1] / "rag_corpus"
    chunks = chunk_corpus(corpus_dir)
    articles = {c.article for c in chunks}
    assert any("GDPR" in a for a in articles)
    assert any("EU AI Act" in a for a in articles)
