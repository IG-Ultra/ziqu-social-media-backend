import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..security import get_current_uid
from ..utils import get_request_base_url

logger = logging.getLogger("ziqu.media")
router = APIRouter(prefix="/api/media", tags=["Media Storage"])

SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB safety cap per upload
CHUNK_SIZE = 1024 * 1024  # 1 MB chunks
_VIDEO_EXTS = {"mp4", "mov", "webm"}


def is_cloudinary_enabled() -> bool:
    return bool(
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    )


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_chunk(file_obj, chunk: bytes) -> None:
    file_obj.write(chunk)


async def _upload_to_cloudinary(
    data: bytes,
    safe_category: str,
    safe_uid: str,
    chosen_ext: str,
    content_type: Optional[str],
) -> str:
    resource_type = "video" if (chosen_ext in _VIDEO_EXTS or "video" in (content_type or "")) else "image"
    folder = f"ziqu/{safe_category}/{safe_uid}"
    public_id = str(uuid.uuid4())
    timestamp = int(time.time())

    # Cloudinary SHA-1 signature: alphabetically sorted params + api_secret
    params_to_sign = f"folder={folder}&public_id={public_id}&timestamp={timestamp}{settings.CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(params_to_sign.encode("utf-8")).hexdigest()

    upload_url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/{resource_type}/upload"
    form_data = {
        "api_key": settings.CLOUDINARY_API_KEY,
        "timestamp": str(timestamp),
        "folder": folder,
        "public_id": public_id,
        "signature": signature,
    }
    mime = content_type or ("video/mp4" if resource_type == "video" else "image/jpeg")
    files = {"file": (f"{public_id}.{chosen_ext}", data, mime)}

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(upload_url, data=form_data, files=files)
        if resp.status_code >= 400:
            logger.error("Cloudinary upload failed (%s): %s", resp.status_code, resp.text[:300])
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Cloud media upload failed. Please try again.",
            )
        payload = resp.json()
        secure_url = (payload.get("secure_url") or payload.get("url") or "").strip()
        if not secure_url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Cloud media provider did not return a valid URL.",
            )
        return secure_url


@router.post("/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    category: str = Form(default="posts"),
    extension: str = Form(default=""),
    uid: str = Depends(get_current_uid),
):
    safe_category = SAFE_SEGMENT.sub("", category) or "posts"
    safe_uid = SAFE_SEGMENT.sub("", uid) or "shared"

    orig_ext = Path(file.filename or "").suffix.lstrip(".").lower()
    chosen_ext = re.sub(r"[^a-z0-9]", "", (extension or orig_ext or "jpg").lower()) or "jpg"
    if chosen_ext not in {"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "webm"}:
        chosen_ext = "mp4" if "video" in (file.content_type or "") else "jpg"

    if is_cloudinary_enabled():
        buf = bytearray()
        try:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                if len(buf) + len(chunk) > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="File exceeds 100 MB maximum upload limit",
                    )
                buf.extend(chunk)
        finally:
            await file.close()

        if not buf:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file uploaded")

        cloud_url = await _upload_to_cloudinary(
            bytes(buf),
            safe_category,
            safe_uid,
            chosen_ext,
            file.content_type,
        )
        return {"url": cloud_url, "relativeUrl": cloud_url, "storage": "cloudinary"}

    # Fallback: local disk storage (persistent only when attached to Render Persistent Disk / VPS)
    filename = f"{uuid.uuid4()}.{chosen_ext}"
    dest_dir = settings.UPLOAD_DIR / safe_category / safe_uid
    await run_in_threadpool(_ensure_dir, dest_dir)
    dest_path = dest_dir / filename

    total_bytes = 0
    out_file = await run_in_threadpool(dest_path.open, "wb")
    try:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File exceeds 100 MB maximum upload limit",
                )
            await run_in_threadpool(_write_chunk, out_file, chunk)
    except Exception:
        await run_in_threadpool(out_file.close)
        await run_in_threadpool(dest_path.unlink, True)
        raise
    else:
        await run_in_threadpool(out_file.close)
    finally:
        await file.close()

    if total_bytes == 0:
        await run_in_threadpool(dest_path.unlink, True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file uploaded")

    relative_path = f"/media/{safe_category}/{safe_uid}/{filename}"
    base = get_request_base_url(request)
    full_url = f"{base}{relative_path}" if base else relative_path
    return {"url": full_url, "relativeUrl": relative_path, "storage": "local"}
