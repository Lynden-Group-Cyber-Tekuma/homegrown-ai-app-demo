"""Upload size guard, file text extraction, and PII-finding context snippets."""
import io
import logging

from fastapi import HTTPException, UploadFile

from src.core import config

logger = logging.getLogger(__name__)

async def _read_upload_with_limit(file: UploadFile) -> bytes:
    data = await file.read(config.MAX_FILE_SIZE_BYTES + 1)
    if len(data) > config.MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large. Max {config.MAX_FILE_SIZE_MB} MB.")
    return data



# ── File text extraction for context snippets ────────────────────────────────
# Temporary store: job_id -> extracted text (cleared once consumed by the poll)
_job_file_texts: dict[str, str] = {}

def _extract_file_text(file_bytes: bytes, filename: str) -> str:  # noqa: C901
    """Best-effort plain-text extraction from uploaded files."""
    import html as _html
    name = (filename or "").lower()
    try:
        # ── PDF ──────────────────────────────────────────────────────────────
        if name.endswith(".pdf"):
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            return "\n".join(page.extract_text() or "" for page in reader.pages)

        # ── Plain text / markup (decode as UTF-8) ────────────────────────────
        if name.endswith((".txt", ".csv", ".tsv", ".md", ".rst", ".org", ".rtf",
                          ".xml", ".sdp", ".prn", ".eth", ".pbd", ".sxg")):
            return file_bytes.decode("utf-8", errors="replace")

        # ── HTML ─────────────────────────────────────────────────────────────
        if name.endswith((".html", ".htm")):
            from html.parser import HTMLParser
            class _Strip(HTMLParser):
                def __init__(self):
                    super().__init__(); self.parts = []
                def handle_data(self, d): self.parts.append(d)
            p = _Strip(); p.feed(file_bytes.decode("utf-8", errors="replace"))
            return " ".join(p.parts)

        # ── DOCX / DOTX / DOCM ───────────────────────────────────────────────
        if name.endswith((".docx", ".docm", ".dotx", ".dotm")):
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip(): parts.append(cell.text)
            return "\n".join(parts)

        # ── PPTX / POTX / PPTM ───────────────────────────────────────────────
        if name.endswith((".pptx", ".pptm", ".potx", ".pot")):
            from pptx import Presentation
            prs = Presentation(io.BytesIO(file_bytes))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            t = "".join(r.text for r in para.runs).strip()
                            if t: parts.append(t)
                    if shape.has_table:
                        for row in shape.table.rows:
                            for cell in row.cells:
                                if cell.text.strip(): parts.append(cell.text)
            return "\n".join(parts)

        # ── XLSX ──────────────────────────────────────────────────────────────
        if name.endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
            parts = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    vals = [str(c) for c in row if c is not None and str(c).strip()]
                    if vals: parts.append("\t".join(vals))
            wb.close()
            return "\n".join(parts)

        # ── XLS (legacy Excel) ────────────────────────────────────────────────
        if name.endswith(".xls"):
            import xlrd
            wb = xlrd.open_workbook(file_contents=file_bytes)
            parts = []
            for sheet in wb.sheets():
                for rx in range(sheet.nrows):
                    vals = [str(sheet.cell_value(rx, cx)) for cx in range(sheet.ncols)
                            if str(sheet.cell_value(rx, cx)).strip()]
                    if vals: parts.append("\t".join(vals))
            return "\n".join(parts)

        # ── ODT / ODS / ODP (OpenDocument ZIP-based XML) ─────────────────────
        if name.endswith((".odt", ".ods", ".odp", ".fods")):
            import zipfile, xml.etree.ElementTree as ET
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if "content.xml" in z.namelist():
                    root = ET.fromstring(z.read("content.xml"))
                    return " ".join(root.itertext())
            return ""

        # ── EPUB (ZIP containing HTML) ────────────────────────────────────────
        if name.endswith(".epub"):
            import zipfile
            from html.parser import HTMLParser
            class _Strip(HTMLParser):
                def __init__(self): super().__init__(); self.parts = []
                def handle_data(self, d): self.parts.append(d)
            parts = []
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                for n in z.namelist():
                    if n.endswith((".html", ".htm", ".xhtml")):
                        p = _Strip(); p.feed(z.read(n).decode("utf-8", errors="replace"))
                        parts.extend(p.parts)
            return " ".join(parts)

        # ── EML ───────────────────────────────────────────────────────────────
        if name.endswith(".eml"):
            import email as _email
            msg = _email.message_from_bytes(file_bytes)
            parts = []
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            return "\n".join(parts)

    except Exception as exc:
        logger.debug("Text extraction failed for %s: %s", filename, exc)
    return ""



def _build_entity_contexts(text: str, findings: dict, context_chars: int = 160) -> dict:
    """
    For each entity value in the findings, locate it in the extracted text and
    return a snippet with `context_chars` characters of surrounding context.
    Returns: {entity_value: {"before": str, "match": str, "after": str}}
    """
    contexts: dict = {}
    if not text:
        return contexts
    for items in findings.values():
        if not isinstance(items, list):
            continue
        for item in items:
            val = str(item.get("entity") or item.get("value") or "")
            if not val or val in contexts:
                continue
            idx = text.find(val)
            if idx == -1:
                # Case-insensitive fallback
                lower = text.lower()
                idx = lower.find(val.lower())
            if idx == -1:
                continue
            start = max(0, idx - context_chars)
            end   = min(len(text), idx + len(val) + context_chars)
            contexts[val] = {
                "before": text[start:idx].replace("\n", " ").strip(),
                "match":  text[idx:idx + len(val)],
                "after":  text[idx + len(val):end].replace("\n", " ").strip(),
            }
    return contexts
