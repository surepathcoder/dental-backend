"""Admin knowledge base: FAQs and uploaded documents used by RAG retrieval."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.runner import load_clinic
from ..database import get_db
from ..models import FAQ, KnowledgeDocument
from ..schemas import FAQOut, FAQWrite, KnowledgeDocOut, KnowledgePasteIn
from ..security import audit, client_ip, require_admin
from ..services.knowledge_docs import MAX_UPLOAD_BYTES, extract_text, keywords_for

router = APIRouter(prefix="/api/knowledge", tags=["Knowledge"], dependencies=[Depends(require_admin)])


def _doc_out(row: KnowledgeDocument) -> KnowledgeDocOut:
    preview = re.sub(r"\s+", " ", (row.text or ""))[:240]
    return KnowledgeDocOut(
        id=row.id,
        title=row.title,
        filename=row.filename,
        source=row.source,
        char_count=row.char_count,
        is_active=row.is_active,
        created_at=row.created_at,
        preview=preview,
    )


def _save_document(
    db: Session,
    clinic_id: int,
    *,
    title: str,
    text: str,
    source: str,
    filename: str | None,
    mime_type: str | None,
    extra_keywords: list[str] | None,
    request: Request,
) -> KnowledgeDocument:
    keywords = keywords_for(title, text, extra_keywords)
    row = KnowledgeDocument(
        clinic_id=clinic_id,
        title=title.strip(),
        filename=filename,
        mime_type=mime_type,
        source=source,
        text=text,
        keywords=keywords,
        is_active=True,
        char_count=len(text),
    )
    db.add(row)
    db.flush()
    audit(db, "admin", "knowledge.uploaded", "knowledge_document", row.id, ip=client_ip(request),
          detail={"title": row.title, "source": source, "chars": row.char_count})
    db.commit()
    db.refresh(row)
    return row


@router.get("/documents", response_model=list[KnowledgeDocOut], summary="List uploaded knowledge documents")
def list_documents(db: Session = Depends(get_db)) -> list[KnowledgeDocOut]:
    clinic = load_clinic(db)
    rows = db.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.clinic_id == clinic.id).order_by(KnowledgeDocument.id.desc())
    ).all()
    return [_doc_out(r) for r in rows]


@router.post("/documents", response_model=KnowledgeDocOut, summary="Paste text into the knowledge base")
def paste_document(body: KnowledgePasteIn, request: Request, db: Session = Depends(get_db)) -> KnowledgeDocOut:
    clinic = load_clinic(db)
    row = _save_document(
        db, clinic.id, title=body.title, text=body.text.strip(), source="paste",
        filename=None, mime_type="text/plain", extra_keywords=body.keywords, request=request,
    )
    return _doc_out(row)


@router.post("/documents/upload", response_model=KnowledgeDocOut, summary="Upload a PDF or text file")
async def upload_document(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    title: str = Form(""),
) -> KnowledgeDocOut:
    data = await file.read()
    if not data:
        raise HTTPException(422, "The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 2 MB")
    try:
        text = extract_text(file.filename or "document.txt", data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    clinic = load_clinic(db)
    name = (title or "").strip() or (file.filename or "Uploaded document")
    row = _save_document(
        db, clinic.id, title=name[:200], text=text, source="upload",
        filename=(file.filename or "")[:200] or None, mime_type=file.content_type, extra_keywords=None, request=request,
    )
    return _doc_out(row)


@router.delete("/documents/{doc_id}", summary="Remove a knowledge document")
def delete_document(doc_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    clinic = load_clinic(db)
    row = db.get(KnowledgeDocument, doc_id)
    if row is None or row.clinic_id != clinic.id:
        raise HTTPException(404, "Document not found")
    audit(db, "admin", "knowledge.deleted", "knowledge_document", row.id, ip=client_ip(request),
          detail={"title": row.title})
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/faqs", response_model=list[FAQOut], summary="List FAQs used by the receptionist")
def list_faqs(db: Session = Depends(get_db)) -> list[FAQ]:
    clinic = load_clinic(db)
    return list(clinic.faqs)


@router.post("/faqs", response_model=FAQOut, summary="Add an FAQ")
def create_faq(body: FAQWrite, request: Request, db: Session = Depends(get_db)) -> FAQ:
    clinic = load_clinic(db)
    row = FAQ(
        clinic_id=clinic.id,
        question=body.question.strip(),
        answer=body.answer.strip(),
        category=body.category.strip() or "general",
        keywords=[k.strip().lower() for k in body.keywords if k.strip()][:24],
        sort_order=len(clinic.faqs),
    )
    db.add(row)
    db.flush()
    audit(db, "admin", "faq.created", "faq", row.id, ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return row


@router.patch("/faqs/{faq_id}", response_model=FAQOut, summary="Update an FAQ")
def update_faq(faq_id: int, body: FAQWrite, request: Request, db: Session = Depends(get_db)) -> FAQ:
    clinic = load_clinic(db)
    row = db.get(FAQ, faq_id)
    if row is None or row.clinic_id != clinic.id:
        raise HTTPException(404, "FAQ not found")
    row.question = body.question.strip()
    row.answer = body.answer.strip()
    row.category = body.category.strip() or "general"
    row.keywords = [k.strip().lower() for k in body.keywords if k.strip()][:24]
    audit(db, "admin", "faq.updated", "faq", row.id, ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return row


@router.delete("/faqs/{faq_id}", summary="Delete an FAQ")
def delete_faq(faq_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    clinic = load_clinic(db)
    row = db.get(FAQ, faq_id)
    if row is None or row.clinic_id != clinic.id:
        raise HTTPException(404, "FAQ not found")
    audit(db, "admin", "faq.deleted", "faq", row.id, ip=client_ip(request))
    db.delete(row)
    db.commit()
    return {"ok": True}
