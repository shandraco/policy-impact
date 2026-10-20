"""Read policy files and split them into numbered clauses, references and defined terms."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# "## 4.2 Title", "4.2 Text", "4.2.1) Text", "§4.2 Text"
# Written so no two whitespace runs can trade characters: a long run of spaces must not make matching slow.
_NUMBERED_RE = re.compile(r"^[ \t]*(?:(?P<hash>#{1,6})[ \t]*)?(?:§[ \t]*)?(?P<num>\d+(?:\.\d+)*)[.)]?[ \t]+(?P<rest>\S.*)$")
MAX_HEADING_LINE = 1000  # longer lines are body text, never a section heading


class _Numbered:
    @staticmethod
    def match(line: str) -> re.Match | None:
        line = line.rstrip()
        return None if len(line) > MAX_HEADING_LINE else _NUMBERED_RE.match(line)


_NUMBERED = _Numbered()

# Limits on what an uploaded file may expand to, so a small file can't exhaust memory.
MAX_DOCX_UNZIPPED = 50 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000
MAX_PDF_PAGES = 1000
MAX_TEXT_CHARS = 5_000_000
_FRONT_MATTER = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*(?:\n|\Z)(?P<body>.*)\Z", re.S)
_DEFINITION = re.compile(
    r"""["“*]{1,2}(?P<term>[A-Za-z][\w /-]{1,58}?)["”*]{1,2}\s+(?:means|refers to|is defined as)\b""",
    re.I,
)
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".docx", ".pdf"}


@dataclass
class Clause:
    number: str
    heading: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.heading}\n{self.body}".strip() if self.heading else self.body


@dataclass
class PolicyDoc:
    id: str | None
    title: str
    owner: str | None = None
    parent: str | None = None
    subsets: list[str] = field(default_factory=list)
    version: int | None = None
    body: str = ""
    clauses: list[Clause] = field(default_factory=list)
    restricted: bool = False
    effective: str | None = None


def _is_title(text: str) -> bool:
    return len(text) <= 80 and not text.endswith((".", ";", ":", ","))


def _starts_clause(m: re.Match) -> bool:
    if m["hash"] or "." in m["num"]:
        return True
    # A bare "4 Isolation" line is a section title; "1. Do this." is a list item inside a clause.
    rest = m["rest"]
    return _is_title(rest) and rest[:1].isupper()


def split_clauses(text: str) -> list[Clause]:
    """Split policy text into clauses keyed by their section number. Text before the first number is clause "0"."""
    clauses: list[Clause] = []
    number, heading, lines = "0", "", []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body or heading:
            clauses.append(Clause(number, heading, body))

    for line in text.replace("\r\n", "\n").split("\n"):
        m = _NUMBERED.match(line)
        if m and _starts_clause(m):
            flush()
            number, lines = m["num"], []
            rest = m["rest"].strip().rstrip("#").strip()
            heading, lines = (rest, []) if _is_title(rest) else ("", [rest])
            continue
        stripped = line.strip()
        lines.append(stripped.lstrip("#").strip() if stripped.startswith("#") else line.rstrip())
    flush()

    seen: dict[str, int] = {}
    for c in clauses:
        seen[c.number] = seen.get(c.number, 0) + 1
        if seen[c.number] > 1:
            c.number = f"{c.number}~{seen[c.number]}"
    return clauses


def split_front_matter(text: str) -> tuple[dict, str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m["meta"]) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"The header block at the top of the file is not valid: {e}") from e
    return (meta if isinstance(meta, dict) else {}), m["body"]


def _too_long() -> ValueError:
    return ValueError("This file holds too much text to compare. Split it into separate policies.")


def _capped(text: str) -> str:
    if len(text) > MAX_TEXT_CHARS:
        raise _too_long()
    return text


def _check_docx_size(data: bytes) -> None:
    """A .docx is a zip; refuse one that would unpack to far more than any real policy."""
    try:
        entries = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile:
        raise ValueError("This .docx file is damaged or isn't really a Word document.") from None
    if len(entries) > MAX_DOCX_ENTRIES or sum(e.file_size for e in entries) > MAX_DOCX_UNZIPPED:
        raise ValueError("This Word file unpacks to an unusually large size, so it wasn't opened.")


def read_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".md", ".markdown", ".txt", ""):
        return _capped(data.decode("utf-8", errors="replace").replace("\r\n", "\n"))
    if suffix == ".docx":
        from docx import Document

        _check_docx_size(data)
        return _capped("\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs))
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"This PDF has more than {MAX_PDF_PAGES} pages, which is too long to compare.")
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise _too_long()
            parts.append(text)
        return "\n".join(parts)
    raise ValueError(f"Files of type {suffix} can't be read. Use .md, .txt, .docx or .pdf.")


def _first_heading(body: str) -> str | None:
    for line in body.split("\n"):
        if line.startswith("# ") and not _NUMBERED.match(line):
            return line[2:].strip()
    return None


def parse_policy(filename: str, data: bytes, id_pattern: str) -> PolicyDoc:
    meta, body = split_front_matter(read_text(filename, data))
    pid = meta.get("id")
    if not pid:
        m = re.search(id_pattern, Path(filename).stem) or re.search(id_pattern, body[:500])
        pid = m.group(0) if m else None
    subsets = meta.get("subsets") or []
    if isinstance(subsets, str):
        subsets = [s.strip() for s in subsets.split(",") if s.strip()]
    version = meta.get("version")
    return PolicyDoc(
        id=str(pid) if pid else None,
        title=str(meta.get("title") or _first_heading(body) or Path(filename).stem),
        owner=str(meta["owner"]) if meta.get("owner") else None,
        parent=str(meta["parent"]) if meta.get("parent") else None,
        subsets=[str(s) for s in subsets],
        version=int(version) if version is not None else None,
        body=body.strip(),
        clauses=split_clauses(body),
        restricted=str(meta.get("restricted", "")).lower() in ("true", "yes", "1"),
        effective=str(meta["effective"]) if meta.get("effective") else None,
    )


def find_refs(text: str, id_pattern: str, own_id: str | None = None) -> list[tuple[str, str | None]]:
    """Policy citations in a clause, as (policy id, section number or None for the whole policy)."""
    pid = f"(?P<pid>{id_pattern})"
    num = r"(?P<num>\d+(?:\.\d+)*)"
    found: set[tuple[str, str | None]] = set()
    for m in re.finditer(rf"§\s*{num}\s+of\s+{pid}", text):
        found.add((m["pid"], m["num"]))
    for m in re.finditer(rf"\b{pid}\b(?:\s*,?\s*(?:§|[Ss]ection|[Ss]ec\.|[Cc]lause)\s*{num})?", text):
        found.add((m["pid"], m["num"]))
    specific = {p for p, n in found if n}
    return sorted(
        ((p, n) for p, n in found if p != own_id and (n or p not in specific)),
        key=lambda r: (r[0], r[1] or ""),
    )


def terms_defined(text: str) -> list[str]:
    """Defined terms in a clause, e.g. '"Isolation point" means ...'."""
    return sorted({m["term"].strip() for m in _DEFINITION.finditer(text)})
