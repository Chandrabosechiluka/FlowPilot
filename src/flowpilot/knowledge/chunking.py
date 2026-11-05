"""Splitting documents into chunks: the units that are embedded, searched and cited.

Why chunk at all? A search should return the *part* of a runbook that answers the question ("Fix"),
not the whole file, and an embedding of a 3-page document blurs every topic in it together.

The rules here:
1. **Split on markdown headings**, because authors already grouped related sentences under them.
2. **Keep the heading path** (`Maintenance jobs > Diagnose`) with each chunk, and put the document
   title and section in front of the text that is embedded ("contextual chunk headers"). A chunk
   that only says "Restart the API" is useless on its own; with its header it is findable.
3. **Cap the size** (about 1,200 characters). A long section is split on paragraph boundaries, and
   the last paragraph of one piece is repeated at the start of the next (overlap) so an idea that
   spans the cut is not lost.
4. **Tickets are one chunk** when they fit, because symptoms, cause and fix belong together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from flowpilot.knowledge.documents import Document

MAX_CHARS = 1200
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str  # "<doc id>#<n>", stable as long as the document's structure does not change
    doc_id: str
    doc_type: str
    title: str
    section: str  # heading path, e.g. "Maintenance jobs failing or not running > Fix"
    text: str  # the chunk's own text, shown to people and models
    source: str
    tags: tuple[str, ...]
    doc_hash: str

    @property
    def embedding_text(self) -> str:
        """What gets embedded and keyword-indexed: title and section first, then the text."""
        tags = f"\nTags: {', '.join(self.tags)}" if self.tags else ""
        return f"{self.title}\nSection: {self.section}{tags}\n\n{self.text}"

    @property
    def citation(self) -> str:
        return f"{self.source} ({self.section})"


def _sections(body: str) -> list[tuple[list[str], str]]:
    """[(heading path, text)] in document order. Text before the first heading has an empty path."""
    sections: list[tuple[list[str], str]] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []
    in_code = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            # Level-1 headings are the document's own title, which every chunk already carries.
            sections.append(([h for lvl, h in path if lvl > 1], text))
        buf.clear()

    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING.match(line)
        if match:
            flush()
            level, heading = len(match.group(1)), match.group(2).strip()
            path = [(lvl, h) for lvl, h in path if lvl < level] + [(level, heading)]
        else:
            buf.append(line)
    flush()
    return sections


def _split_long(text: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    current: list[str] = []
    for para in paragraphs:
        if current and len("\n\n".join([*current, para])) > max_chars:
            pieces.append("\n\n".join(current))
            current = [current[-1]] if len(current[-1]) < max_chars // 2 else []  # overlap
        current.append(para)
    if current:
        pieces.append("\n\n".join(current))
    # A single paragraph longer than the cap is cut on sentence boundaries as a last resort.
    final: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars * 1.5:
            cut = piece.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            final.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        final.append(piece)
    return final


def chunk_document(doc: Document, *, max_chars: int = MAX_CHARS) -> list[Chunk]:
    if doc.type == "ticket" and len(doc.body) <= max_chars * 2:
        body = "\n".join(line for line in doc.body.splitlines() if not line.startswith("# ")).strip()
        parts = [(["Ticket"], body)]
    else:
        parts = []
        for path, text in _sections(doc.body):
            for piece in _split_long(text, max_chars):
                parts.append((path, piece))

    chunks = []
    for n, (path, text) in enumerate(parts):
        section_path = [h for h in path if h.lower() != doc.title.lower()] or ["Overview"]
        chunks.append(
            Chunk(
                id=f"{doc.id}#{n}",
                doc_id=doc.id,
                doc_type=doc.type,
                title=doc.title,
                section=" > ".join(section_path),
                text=text,
                source=doc.source,
                tags=doc.tags,
                doc_hash=doc.content_hash,
            )
        )
    return chunks
