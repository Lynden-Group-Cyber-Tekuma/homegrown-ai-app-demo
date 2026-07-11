"""File upload routes: image/text attach and text-preview extraction."""
import base64
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from auth import get_current_user
from models import User
from src.core.config import ALLOWED_IMAGE_TYPES, ALLOWED_PDF_TYPE, ALLOWED_TEXT_TYPES
from src.services.file_extract import _extract_file_text, _read_upload_with_limit

router = APIRouter()

# ── File upload ───────────────────────────────────────────────────────────────
@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    data = await _read_upload_with_limit(file)

    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or "upload"

    if mime in ALLOWED_IMAGE_TYPES:
        b64 = base64.b64encode(data).decode()
        return {"type": "image", "content": f"data:{mime};base64,{b64}", "filename": filename, "size_bytes": len(data)}

    if mime == ALLOWED_PDF_TYPE or filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
            if not text:
                raise HTTPException(status_code=422, detail="Could not extract text from PDF.")
        except ImportError:
            raise HTTPException(status_code=501, detail="PDF support not installed.")
        return {"type": "text", "content": text, "filename": filename, "size_bytes": len(data)}

    if mime in ALLOWED_TEXT_TYPES or filename.lower().endswith((".txt", ".md", ".csv", ".json")):
        text = data.decode("utf-8", errors="replace")
        return {"type": "text", "content": text, "filename": filename, "size_bytes": len(data)}

    raise HTTPException(status_code=415, detail=f"Unsupported file type '{mime}'.")



@router.post("/upload/preview-text")
async def upload_preview_text(file: UploadFile = File(...)):
    """Extract and return plain text from an uploaded file for in-browser preview."""
    file_bytes = await _read_upload_with_limit(file)
    text = _extract_file_text(file_bytes, file.filename or "")
    if not text:
        return JSONResponse({"text": None, "error": "Could not extract text from this file type."})
    return JSONResponse({"text": text, "filename": file.filename})
