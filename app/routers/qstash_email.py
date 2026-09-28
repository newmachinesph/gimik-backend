"""Signed QStash callback for transactional email delivery."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, ValidationError
from qstash import Receiver
from qstash.errors import SignatureError

from app.config import get_settings
from app.services.email import deliver_email

router = APIRouter(tags=["ops"])


class EmailJob(BaseModel):
    to: EmailStr
    subject: str
    text: str


@router.post("/internal/qstash/email", include_in_schema=False)
async def deliver_queued_email(request: Request) -> dict[str, bool]:
    settings = get_settings()
    if not settings.qstash_enabled or not settings.mail_enabled:
        raise HTTPException(status_code=503, detail="Email delivery is unavailable")

    signature = request.headers.get("Upstash-Signature")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing QStash signature")

    raw_body = await request.body()
    if len(raw_body) > 100_000:
        raise HTTPException(status_code=413, detail="Email job is too large")
    try:
        body = raw_body.decode("utf-8")
        Receiver(
            current_signing_key=settings.qstash_current_signing_key,
            next_signing_key=settings.qstash_next_signing_key,
        ).verify(signature=signature, body=body, url=settings.qstash_email_callback_url)
    except (UnicodeDecodeError, SignatureError) as error:
        raise HTTPException(status_code=401, detail="Invalid QStash signature") from error

    try:
        job = EmailJob.model_validate_json(raw_body)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Invalid email job") from error

    await run_in_threadpool(deliver_email, str(job.to), job.subject, job.text)
    return {"ok": True}
