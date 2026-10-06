import logging
import os
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import analytics
import auth
import converter
import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trs")

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
ALLOWED_SUFFIXES = {".xlsx", ".xls"}

SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    log.warning("SESSION_SECRET is not set; using a random key (sessions reset on restart).")
    SESSION_SECRET = secrets.token_urlsafe(32)

# Render serves over HTTPS, so mark cookies Secure there unless overridden.
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true" if os.environ.get("RENDER") else "false") == "true"

app = FastAPI(title="TRS Converter")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="trs_session",
    max_age=12 * 60 * 60,
    same_site="lax",
    https_only=COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

analytics.init_db()


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    # Browsers navigating to a protected page get sent to the login screen;
    # API calls get JSON so the front end can show the message.
    if not request.url.path.startswith("/api/"):
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code == 403:
            return RedirectResponse("/", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------- Authentication ----------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "username": ""})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    username = username.strip()
    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{username.lower()}|{client_ip}"

    def fail(message: str, status: int):
        return templates.TemplateResponse(
            request, "login.html", {"error": message, "username": username}, status_code=status
        )

    if auth.throttle.is_locked(throttle_key):
        analytics.track("login_locked", username or None)
        return fail("Too many failed attempts. Please wait 15 minutes and try again.", 429)

    user = auth.authenticate(username, password)
    if user is None:
        auth.throttle.record_failure(throttle_key)
        analytics.track("login_failed", username or None)
        return fail("Incorrect username or password.", 401)

    auth.throttle.reset(throttle_key)
    request.session.clear()
    request.session["user"] = user
    analytics.track("login_succeeded", user["username"])
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    user = auth.current_user(request)
    if user:
        analytics.track("logout", user["username"])
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- Converter ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(auth.require_user)):
    return templates.TemplateResponse(
        request, "index.html",
        {"user": user, "max_upload_mb": MAX_UPLOAD_MB},
    )


def safe_stem(filename: str) -> str:
    """Strip any path and unsafe characters from an uploaded filename."""
    stem = Path(filename.replace("\\", "/")).stem
    stem = re.sub(r"[^A-Za-z0-9 ._-]", "_", stem).strip(" .")
    return stem[:100] or "TRS"


@app.post("/api/convert")
async def convert(file: UploadFile = File(None), user: dict = Depends(auth.require_user)):
    def reject(message: str, status: int = 400):
        analytics.track("upload_rejected", user["username"], file=file.filename if file else None, reason=message)
        raise HTTPException(status_code=status, detail=message)

    if file is None or not file.filename:
        reject("Please choose an Excel file to upload.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        reject("Only Excel files (.xlsx or .xls) are supported.")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data:
        reject("The selected file is empty.")
    if len(data) > max_bytes:
        reject(f"The file is larger than {MAX_UPLOAD_MB} MB.", 413)

    try:
        converter.check_signature(data[:8], suffix)
    except converter.ConversionError as e:
        reject(str(e))

    job = jobs.submit(user["username"], file.filename, safe_stem(file.filename), data, suffix)
    analytics.track("upload_received", user["username"], file=file.filename, bytes=len(data))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, user: dict = Depends(auth.require_user)):
    job = jobs.get(job_id, user["username"])
    if job is None:
        raise HTTPException(status_code=404, detail="This conversion has expired. Please upload the file again.")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/jpeg")
async def job_jpeg(job_id: str, download: bool = False, user: dict = Depends(auth.require_user)):
    job = jobs.get(job_id, user["username"])
    if job is None or job.status != "done" or not job.jpeg_path or not job.jpeg_path.exists():
        raise HTTPException(status_code=404, detail="This JPEG is no longer available. Please convert the file again.")
    if download:
        analytics.track("jpeg_downloaded", user["username"], file=job.filename)
    return FileResponse(
        job.jpeg_path,
        media_type="image/jpeg",
        filename=job.jpeg_path.name,
        content_disposition_type="attachment" if download else "inline",
    )


class ClientEvent(BaseModel):
    event: str = Field(max_length=40)
    details: dict = Field(default_factory=dict)


@app.post("/api/events", status_code=204)
async def client_event(payload: ClientEvent, user: dict = Depends(auth.require_user)):
    if payload.event not in analytics.CLIENT_EVENTS:
        raise HTTPException(status_code=400, detail="Unknown event.")
    # Keep only short scalar values so the browser can't stuff the database.
    details = {
        str(k)[:40]: (v[:200] if isinstance(v, str) else v)
        for k, v in list(payload.details.items())[:10]
        if isinstance(v, (str, int, float, bool))
    }
    analytics.track(payload.event, user["username"], **details)


# ---------- Admin ----------

@app.get("/admin/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, days: int = 7, user: dict = Depends(auth.require_admin)):
    days = max(1, min(days, 365))
    return templates.TemplateResponse(
        request, "analytics.html", {"user": user, "stats": analytics.summary(days=days)}
    )
