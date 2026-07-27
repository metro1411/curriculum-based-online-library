"""
file_processing.py
-------------------
Extracts plain text from uploaded academic resources (PDF, DOCX, PPTX,
TXT, MD) and splits it into overlapping chunks that the lightweight RAG
engine (ai_engine.py) can search over.

Extraction is best-effort: if a file is encrypted, corrupted, or in an
unexpected format, we log the problem and store the resource anyway
without chunks. A resource is always usable for viewing/downloading even
if AI-assisted search over its contents is unavailable.
"""

import logging

logger = logging.getLogger("smart_dit_archive.file_processing")

MAX_EXTRACT_CHARS = 60_000   # cap extraction so a huge PDF can't stall a request
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120


def extract_text(filepath, ext):
    """Return extracted plain text for a resource file, or '' if not possible."""
    ext = (ext or "").lower()
    try:
        if ext == "pdf":
            return _extract_pdf(filepath)
        if ext == "docx":
            return _extract_docx(filepath)
        if ext == "pptx":
            return _extract_pptx(filepath)
        if ext in ("txt", "md"):
            return _extract_plain(filepath)
    except Exception:
        logger.exception("Text extraction failed for %s (%s)", filepath, ext)
        return ""
    return ""


def _extract_pdf(filepath):
    from pypdf import PdfReader

    text_parts = []
    reader = PdfReader(filepath)
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            text_parts.append(page_text)
        if sum(len(t) for t in text_parts) > MAX_EXTRACT_CHARS:
            break
    return "\n\n".join(text_parts)[:MAX_EXTRACT_CHARS]


def _extract_docx(filepath):
    import docx

    document = docx.Document(filepath)
    parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text and cell.text.strip():
                    parts.append(cell.text.strip())
    return "\n".join(parts)[:MAX_EXTRACT_CHARS]


def _extract_pptx(filepath):
    from pptx import Presentation

    prs = Presentation(filepath)
    parts = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_lines = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_lines.append(shape.text_frame.text.strip())
            elif shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            slide_lines.append(cell.text.strip())
        if slide_lines:
            parts.append(f"[Slide {i}] " + " | ".join(slide_lines))
    return "\n".join(parts)[:MAX_EXTRACT_CHARS]


def _extract_plain(filepath):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()[:MAX_EXTRACT_CHARS]


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks, breaking on paragraph/sentence
    boundaries where possible so chunks stay reasonably self-contained."""
    text = (text or "").strip()
    if not text:
        return []

    # Prefer splitting on paragraph breaks first.
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks = []
    buffer = ""

    for para in paragraphs:
        candidate = (buffer + "\n" + para).strip() if buffer else para
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue

        if buffer:
            chunks.append(buffer)
        if len(para) <= chunk_size:
            buffer = para
        else:
            # paragraph itself is too long; hard-split it with overlap
            start = 0
            while start < len(para):
                end = start + chunk_size
                chunks.append(para[start:end])
                start = end - overlap if end - overlap > start else end
            buffer = ""

    if buffer:
        chunks.append(buffer)

    # Safety net: if paragraph splitting produced nothing usable (e.g. no
    # newlines at all), fall back to a plain sliding window.
    if not chunks:
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start = end - overlap if end - overlap > start else end

    return chunks


def process_resource_text(filepath, ext):
    """High-level helper: extract + chunk. Returns (chunks, status) where
    status is 'success', 'failed', or 'not_applicable'."""
    if ext not in ("pdf", "docx", "pptx", "txt", "md"):
        return [], "not_applicable"

    text = extract_text(filepath, ext)
    if not text or not text.strip():
        return [], "failed"

    chunks = chunk_text(text)
    if not chunks:
        return [], "failed"

    return chunks, "success"
