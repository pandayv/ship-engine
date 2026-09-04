"""
Splits a regulation text file (rag_corpus/**/*.txt) into paragraph-level
chunks with source metadata, ready for embedding. Pure text logic, no
AWS dependency — fully testable without credentials.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Chunk:
    text: str
    source_file: str
    article: str
    paragraph_index: int

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source_file": self.source_file,
            "article": self.article,
            "paragraph_index": self.paragraph_index,
        }


def chunk_file(path: Path) -> list[Chunk]:
    """
    Splits on blank-line-separated paragraphs. The first non-empty line of
    the file is treated as the article title (e.g. "GDPR Article 32 —
    Security of processing") and carried as metadata on every chunk from
    that file, so a citation can always name its source article even if
    the retrieved chunk is just paragraph 3.
    """
    raw = path.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if not paragraphs:
        return []

    title = paragraphs[0].splitlines()[0].strip()
    chunks = []
    idx = 0
    for para in paragraphs[1:]:
        # skip the "Source:" / "Retrieved verbatim from..." provenance line
        # and standalone "Note:" appendices we added ourselves, not part of
        # the actual statute text
        if para.startswith("Source:") or para.startswith("Note:"):
            continue
        chunks.append(Chunk(text=para, source_file=str(path), article=title, paragraph_index=idx))
        idx += 1
    return chunks


def chunk_corpus(corpus_dir: Path) -> list[Chunk]:
    all_chunks = []
    for path in sorted(corpus_dir.rglob("*.txt")):
        all_chunks.extend(chunk_file(path))
    return all_chunks


if __name__ == "__main__":
    corpus_dir = Path(__file__).resolve().parents[2] / "rag_corpus"
    chunks = chunk_corpus(corpus_dir)
    print(f"{len(chunks)} chunks from {corpus_dir}")
    for c in chunks:
        print(f"[{c.article} / para {c.paragraph_index}] {c.text[:80]}...")
