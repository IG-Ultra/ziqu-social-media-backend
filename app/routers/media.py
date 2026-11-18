import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..security import get_current_uid
from ..utils import get_request_base_url

router = APIRouter(prefix="/api/media", tags=["Media Storage"])

SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB safety cap per upload
CHUNK_SIZE = 1024 * 1024  # 1 MB chunks


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_chunk(file_obj, chunk: bytes) -> None:
    file_obj.write(chunk)


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
    return {"url": full_url, "relativeUrl": relative_path}

