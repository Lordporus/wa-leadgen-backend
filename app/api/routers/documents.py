from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.database import SessionLocal
from app.core.models import Client

router = APIRouter()

@router.post("/api/documents/upload", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute", key_func=get_client_key)
def upload_document(request: Request, response: Response, file: UploadFile, client: Client = Depends(require_api_key)):
    """Upload a PDF or TXT file to the tenant's knowledge base."""
    from app.services.usage import check_limit
    plan = client.plan_tier or "base"
    allowed, reason = check_limit(client.id, "document_upload", plan=plan)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")

    fname = file.filename or "upload"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    if ext not in ("pdf", "txt"):
        raise HTTPException(status_code=400, detail="Only PDF and TXT files are supported.")

    raw = file.file.read(MAX_FILE_SIZE + 1)
    if len(raw) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")

    if ext == "pdf":
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(raw))
        text_content = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text_content = raw.decode("utf-8", errors="replace")

    if not text_content.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from file.")

    from app.services.ingestion import ingest_document
    stored = ingest_document(client.id, fname, text_content)
    return {"success": True, "filename": fname, "chunks_stored": stored}


@router.get("/api/documents", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def list_documents(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """List distinct documents uploaded by this tenant."""
    from app.core.models import Document as DocModel
    from sqlalchemy import func
    with SessionLocal() as s:
        rows = (
            s.query(DocModel.filename, func.count(DocModel.id), func.min(DocModel.created_at))
            .filter(DocModel.client_id == client.id)
            .group_by(DocModel.filename)
            .order_by(func.min(DocModel.created_at).desc())
            .all()
        )
        return [
            {"filename": r[0], "chunks": r[1], "uploaded_at": r[2].isoformat() if r[2] else None}
            for r in rows
        ]


# ── Billing endpoints ──────────────────────────────────────────────────────
