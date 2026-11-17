import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from ..config import settings
from ..security import get_current_uid

router = APIRouter(prefix="/api/media", tags=["Media Storage"])

SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]")


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
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file uploaded")

    dest_path.write_bytes(data)

    relative_path = f"/media/{safe_category}/{safe_uid}/{filename}"
    base = str(request.base_url).rstrip("/")
    full_url = f"{base}{relative_path}"
    return {"url": full_url, "relativeUrl": relative_path}
