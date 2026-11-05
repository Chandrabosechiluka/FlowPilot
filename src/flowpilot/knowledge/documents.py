"""Loading the knowledge files: markdown with a small header (front matter) between `---` lines."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

VALID_TYPES = {"runbook", "ticket"}


class DocumentError(ValueError):
    """A knowledge file is malformed. The message names the file so it can be fixed."""


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    title: str
    type: str
    body: str
    source: str  # path relative to the knowledge folder, e.g. runbooks/maintenance-jobs.md
    tags: tuple[str, ...] = ()
    date: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Changes when anything that is indexed changes, so unchanged documents can be skipped."""
        material = "\n".join([self.id, self.title, self.type, ",".join(self.tags), self.date or "", self.body])
        return hashlib.sha256(material.encode()).hexdigest()[:16]


def parse_front_matter(text: str, *, source: str) -> tuple[dict[str, object], str]:
    """Split `---\\nkey: value\\n---\\nbody`. Values may be lists: `tags: [a, b]`. Comments start with `#`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DocumentError(f"{source}: must start with a '---' header block")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise DocumentError(f"{source}: header block is not closed with '---'") from exc

    meta: dict[str, object] = {}
    for n, line in enumerate(lines[1:end], start=2):
        line = line.split(" #", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise DocumentError(f"{source}:{n}: expected 'key: value', got {line!r}")
        key, value = (part.strip() for part in line.split(":", 1))
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, "\n".join(lines[end + 1 :]).strip()


def parse_document(text: str, *, source: str) -> Document:
    meta, body = parse_front_matter(text, source=source)
    missing = [k for k in ("id", "title", "type") if not meta.get(k)]
    if missing:
        raise DocumentError(f"{source}: header is missing {', '.join(missing)}")
    doc_type = str(meta["type"])
    if doc_type not in VALID_TYPES:
        raise DocumentError(f"{source}: type must be one of {sorted(VALID_TYPES)}, got {doc_type!r}")
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        raise DocumentError(f"{source}: tags must be a list like [a, b]")
    known = {"id", "title", "type", "tags", "date"}
    return Document(
        id=str(meta["id"]),
        title=str(meta["title"]),
        type=doc_type,
        body=body,
        source=source,
        tags=tuple(tags),
        date=str(meta["date"]) if meta.get("date") else None,
        extra={k: str(v) for k, v in meta.items() if k not in known},
    )


def load_documents(folder: Path) -> list[Document]:
    """Every `*.md` under `folder` except README files. Duplicate ids are an error, not a silent overwrite."""
    if not folder.is_dir():
        raise DocumentError(f"knowledge folder {folder} does not exist")
    docs: list[Document] = []
    seen: dict[str, str] = {}
    for path in sorted(folder.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        source = path.relative_to(folder).as_posix()
        doc = parse_document(path.read_text(encoding="utf-8"), source=source)
        if doc.id in seen:
            raise DocumentError(f"{source}: id {doc.id!r} is already used by {seen[doc.id]}")
        seen[doc.id] = source
        docs.append(doc)
    return docs
