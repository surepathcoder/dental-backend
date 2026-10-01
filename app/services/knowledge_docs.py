"""Extract text from uploaded files and turn it into retrievable knowledge chunks."""

from __future__ import annotations

import io
import re
from pathlib import Path

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 80_000
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120
ALLOWED_SUFFIXES = {".txt", ".md", ".csv", ".pdf"}
_STOP = set(
    "a an the and or but if of to in on for with at by from is are was were be been do does did can could would should will "
    "you your yours we our us i me my it its this that these those what which who whom how when where why there here have has "
    "had any some about please tell know want like get".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP and len(w) > 1]


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(p.strip() for p in pages if p.strip())


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename or "note.txt").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("Upload a PDF, .txt, .md or .csv file.")
    if suffix == ".pdf":
        try:
            text = extract_pdf(data)
        except Exception as exc:  # noqa: BLE001 — surface a clean admin error
            raise ValueError("Couldn't read that PDF. Try exporting it as text.") from exc
    else:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("No readable text was found in that file.")
    return text[:MAX_TEXT_CHARS]


def keywords_for(title: str, text: str, extra: list[str] | None = None) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for token in _tokens(f"{title} {text[:2000]}"):
        if token in _STOP or token in seen or len(token) < 3:
            continue
        seen.add(token)
        words.append(token)
        if len(words) >= 40:
            break
    for item in extra or []:
        cleaned = re.sub(r"\s+", " ", item).strip().lower()
        if cleaned and cleaned not in seen:
            words.append(cleaned)
    if title.strip() and title.strip().lower() not in seen:
        words.insert(0, title.strip().lower())
    return words[:48]


def chunk_text(text: str) -> list[str]:
    compact = text.strip()
    if len(compact) <= CHUNK_SIZE:
        return [compact]
    chunks: list[str] = []
    start = 0
    while start < len(compact):
        end = min(len(compact), start + CHUNK_SIZE)
        if end < len(compact):
            cut = compact.rfind("\n", start + CHUNK_SIZE // 2, end)
            if cut == -1:
                cut = compact.rfind(" ", start + CHUNK_SIZE // 2, end)
            if cut > start:
                end = cut
        piece = compact[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(compact):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks
