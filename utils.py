"""
utils.py
--------
Small, dependency-free helper functions shared across routes.
"""

import re
import uuid
import unicodedata

from werkzeug.utils import secure_filename


def slugify(text):
    """Turn 'Renewable Energies Technology' into 'renewable-energies-technology'."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "item"


def human_filesize(num_bytes):
    """Return a friendly file size string, e.g. '1.4 MB'."""
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def allowed_file(filename, allowed_extensions):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in allowed_extensions


def build_stored_filename(original_filename):
    """Generate a collision-proof filename for disk storage while keeping
    the original name (sanitized) recoverable for display/download."""
    safe_name = secure_filename(original_filename) or "file"
    unique_prefix = uuid.uuid4().hex[:12]
    return f"{unique_prefix}_{safe_name}"


# Magic-byte signatures used as a lightweight defense-in-depth check that the
# uploaded bytes actually look like the claimed file type. This is not a
# substitute for real antivirus scanning in a production deployment, but it
# catches the common case of a renamed/disguised file extension.
_SIGNATURES = {
    "pdf": [b"%PDF-"],
    "docx": [b"PK\x03\x04"],
    "pptx": [b"PK\x03\x04"],
}


# Never trust the MIME type supplied by a browser upload. The extension has
# already passed the allow-list check; this mapping ensures files are stored
# and later served with the matching, safe media type.
RESOURCE_MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "mp4": "video/mp4",
    "webm": "video/webm",
}


def looks_like_claimed_type(file_storage, ext):
    """Peek at the first bytes of an uploaded file to sanity-check its type.
    Returns True for extensions we don't have a signature for (txt/md), since
    plain text has no reliable magic number.
    """
    signatures = _SIGNATURES.get(ext)
    if not signatures:
        return True
    try:
        pos = file_storage.stream.tell()
        header = file_storage.stream.read(8)
        file_storage.stream.seek(pos)
    except Exception:
        return True  # fail open on unexpected stream issues; extension + size still checked
    return any(header.startswith(sig) for sig in signatures)


def safe_resource_mime_type(filename):
    """Return the application-controlled media type for an allowed resource."""
    ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    return RESOURCE_MIME_TYPES.get(ext, "application/octet-stream")


def excerpt(text, length=160):
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"
