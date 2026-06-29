from __future__ import annotations

from pathlib import Path


def approximate_token_count(text: str) -> int:
    if not text:
        return 0
    return int(len(text.split()) * 1.3)


def extract_text_from_uploaded_file(file_obj, file_type: str) -> str:
    """Extract UTF-8 text from txt/pdf files.

    file_obj should be an opened Django File object.
    """

    normalized = (file_type or "").lower()
    if normalized == "txt":
        raw = file_obj.read()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace").strip()
        return str(raw).strip()

    if normalized == "pdf":
        try:
            from PyPDF2 import PdfReader
        except Exception as exc:  # pragma: no cover - dependency-driven
            raise RuntimeError("PyPDF2 is required to parse PDF syllabus files.") from exc

        reader = PdfReader(file_obj)
        pages: list[str] = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n".join(pages).strip()

    extension = Path(getattr(file_obj, "name", "")).suffix.lower()
    raise RuntimeError(f"Unsupported syllabus file type '{normalized or extension}'. Use .pdf or .txt")
