"""Lauren Davis Photography — public site + owner admin + AI feature board.

Single-file FastAPI app, plain SQL over SQLite. The public landing page renders
owner-editable text (the `content` table) and photos (the `photos` table); the
owner manages both from /admin, which also hosts a Kanban feature-request board
that hands work to a local LLM one item at a time (see feature_coder.py).

Editable-by-AI source files: webapp.py, index.html, admin.html.
"""

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

APP_DIR = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SITE_DATA", str(APP_DIR / "data")))
MEDIA = DATA / "media"
DB = DATA / "site.db"
DATA.mkdir(parents=True, exist_ok=True)
MEDIA.mkdir(parents=True, exist_ok=True)

ADMIN_EMAILS = {e.strip().lower()
                for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
RUN_QUEUE_WORKER = os.environ.get("SITE_QUEUE_WORKER", "1") != "0"

MAX_UPLOAD = 25 * 1024 * 1024           # 25 MB per photo
MAX_EDGE = 2400                          # downscale longest side to this for web
ALLOWED_IMG = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_WLOCK = threading.Lock()                # serialize sqlite writes

# ---------------------------------------------------------------- default copy
DEFAULT_CONTENT = {
    "site_title":    "Lauren Davis Photography",
    "nav_brand":     "Lauren Davis",
    "hero_heading":  "Lauren Davis Photography",
    "hero_sub":      "Portraits, weddings & everyday moments — made to last.",
    "hero_cta":      "Get in touch",
    "about_heading": "About",
    "about_body":    ("I'm Lauren, a photographer who loves natural light and "
                      "honest moments. Whether it's a wedding, a family session, "
                      "or a portrait, my goal is simple: photographs you'll "
                      "treasure for years."),
    "gallery_heading": "Work",
    "gallery_sub":   "A few recent favorites.",
    "contact_heading": "Let's work together",
    "contact_body":  "Tell me a little about what you have in mind.",
    "contact_email": "hello@laurendavisphoto.com",
    "contact_phone": "",
    "instagram":     "",
    "footer":        "© Lauren Davis Photography",
    "construction_msg": ("🚧 Website Under Construction! 🚧\n\n"
                         "As my business grows, so must my professionalism! "
                         "I'm working hard on bringing you the absolute best product and experience. "
                         "Thank you SO much for your patience — I can't wait to show you what's coming! ✨\n\n"
                         "In the meantime, feel free to reach out:\n"
                         "📞 269-270-1433\n"
                         "📧 lauren.davis@dw-r.com"),
}

# ---------------------------------------------------------------- db helpers
def _conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def q(sql, params=()):
    c = _conn()
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def execw(sql, params=()):
    with _WLOCK:
        c = _conn()
        try:
            c.execute(sql, params)
            c.commit()
        finally:
            c.close()


def insertw(sql, params=()):
    with _WLOCK:
        c = _conn()
        try:
            cur = c.execute(sql, params)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def ensure_schema():
    execw("""CREATE TABLE IF NOT EXISTS content(
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
    execw("""CREATE TABLE IF NOT EXISTS photos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL, original_name TEXT, caption TEXT DEFAULT '',
        section TEXT DEFAULT 'gallery', sort INTEGER DEFAULT 0,
        w INTEGER, h INTEGER, created_at TEXT)""")
    execw("""CREATE TABLE IF NOT EXISTS feature_request(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, description TEXT DEFAULT '', page TEXT DEFAULT '',
        status TEXT DEFAULT 'new', backend TEXT DEFAULT '',
        restatement TEXT DEFAULT '', queued_at TEXT,
        created_at TEXT, updated_at TEXT)""")
    execw("""CREATE TABLE IF NOT EXISTS feature_attempt(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER, status TEXT DEFAULT 'running', backend TEXT DEFAULT '',
        branch TEXT DEFAULT '', log TEXT DEFAULT '', diff TEXT DEFAULT '',
        files TEXT DEFAULT '', error TEXT DEFAULT '',
        created_at TEXT, updated_at TEXT)""")
    for k, v in DEFAULT_CONTENT.items():
        if not q("SELECT 1 FROM content WHERE key=?", (k,)):
            execw("INSERT INTO content(key,value,updated_at) VALUES(?,?,?)",
                  (k, v, now()))


ensure_schema()

app = FastAPI(title="Lauren Davis Photography")

# ---------------------------------------------------------------- auth
def viewer_email(request: Request) -> str:
    """Cloudflare Access injects the verified identity on the tunnel path."""
    return (request.headers.get("Cf-Access-Authenticated-User-Email") or "").lower()


def require_admin(request: Request) -> str:
    """Cloudflare Access is the real gate at the edge (on /admin). This is
    defense-in-depth: if an allow-list is configured AND Access identified the
    user, enforce membership. On the trusted LAN (no Access header) we allow, so
    the owner can reach admin directly by IP if the tunnel is down."""
    email = viewer_email(request)
    if ADMIN_EMAILS and email and email not in ADMIN_EMAILS:
        raise HTTPException(403, f"{email} is not on the admin allow-list")
    return email or "local"


# ---------------------------------------------------------------- pages
def _page(name: str) -> HTMLResponse:
    p = APP_DIR / name
    if not p.exists():
        raise HTTPException(404, f"{name} missing")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def landing():
    return _page("index.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    require_admin(request)
    return _page("admin.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- public API
@app.get("/api/content")
def get_content():
    return {r["key"]: r["value"] for r in q("SELECT key,value FROM content")}


@app.get("/api/photos")
def get_photos(section: str = ""):
    if section:
        rows = q("SELECT * FROM photos WHERE section=? ORDER BY sort,id", (section,))
    else:
        rows = q("SELECT * FROM photos ORDER BY sort,id")
    for r in rows:
        r["url"] = f"/media/{r['filename']}"
    return rows


@app.get("/media/{filename}")
def media(filename: str):
    # prevent path traversal; filenames we generate are safe slugs
    if "/" in filename or ".." in filename:
        raise HTTPException(404)
    p = MEDIA / filename
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------------------------------------------------------- admin: identity
@app.get("/admin/api/me")
def admin_me(request: Request):
    email = require_admin(request)
    return {"email": email, "allow_list": sorted(ADMIN_EMAILS)}


# ---------------------------------------------------------------- admin: content
@app.get("/admin/api/content")
def admin_get_content(request: Request):
    require_admin(request)
    return {r["key"]: r["value"] for r in q("SELECT key,value FROM content")}


@app.put("/admin/api/content")
async def admin_put_content(request: Request):
    require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected an object of {key: value}")
    for k, v in body.items():
        k = str(k)[:64]
        v = "" if v is None else str(v)
        if q("SELECT 1 FROM content WHERE key=?", (k,)):
            execw("UPDATE content SET value=?, updated_at=? WHERE key=?", (v, now(), k))
        else:
            execw("INSERT INTO content(key,value,updated_at) VALUES(?,?,?)", (k, v, now()))
    return {"saved": len(body)}


# ---------------------------------------------------------------- admin: photos
def _slug(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.") or "photo"
    return base[:60]


def _store_image(raw: bytes, original_name: str) -> dict:
    """Validate, optionally downscale/strip EXIF, save under MEDIA. Returns meta."""
    from io import BytesIO
    from PIL import Image, ImageOps
    try:
        im = Image.open(BytesIO(raw))
        im.verify()                       # integrity check
        im = Image.open(BytesIO(raw))     # re-open after verify
        im = ImageOps.exif_transpose(im)  # honor orientation, then drop EXIF
    except Exception:
        raise HTTPException(400, "not a readable image")
    fmt = (im.format or "JPEG").upper()
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt, "jpg")
    if max(im.size) > MAX_EDGE and fmt != "GIF":
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    stem = _slug(Path(original_name).stem)
    fname = f"{int(time.time()*1000)}-{stem}.{ext}"
    out = MEDIA / fname
    save_kw = {"quality": 88, "optimize": True} if fmt in ("JPEG", "WEBP") else {}
    if fmt == "GIF":
        Image.open(BytesIO(raw)).save(out)          # keep animation intact
    else:
        if im.mode in ("RGBA", "P") and fmt == "JPEG":
            im = im.convert("RGB")
        im.save(out, **save_kw)
    return {"filename": fname, "w": im.size[0], "h": im.size[1]}


@app.post("/admin/api/photos")
async def admin_upload(request: Request, section: str = Form("gallery"),
                       files: list[UploadFile] = File(...)):
    require_admin(request)
    section = (section or "gallery").strip()[:32]
    base = q("SELECT COALESCE(MAX(sort),0) AS m FROM photos WHERE section=?",
             (section,))[0]["m"]
    saved = []
    for i, f in enumerate(files, 1):
        raw = await f.read()
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, f"{f.filename} exceeds 25 MB")
        if f.content_type and f.content_type not in ALLOWED_IMG:
            raise HTTPException(400, f"{f.filename}: unsupported type {f.content_type}")
        meta = _store_image(raw, f.filename or "photo")
        pid = insertw(
            "INSERT INTO photos(filename,original_name,caption,section,sort,w,h,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (meta["filename"], f.filename, "", section, base + i,
             meta["w"], meta["h"], now()))
        saved.append({"id": pid, "url": f"/media/{meta['filename']}", "section": section})
    return {"uploaded": saved}


@app.patch("/admin/api/photos/{pid}")
async def admin_edit_photo(pid: int, request: Request):
    require_admin(request)
    body = await request.json()
    if not q("SELECT 1 FROM photos WHERE id=?", (pid,)):
        raise HTTPException(404, "no such photo")
    for col in ("caption", "section"):
        if col in body:
            execw(f"UPDATE photos SET {col}=? WHERE id=?", (str(body[col])[:500], pid))
    if "sort" in body:
        execw("UPDATE photos SET sort=? WHERE id=?", (int(body["sort"]), pid))
    return {"ok": True}


@app.post("/admin/api/photos/reorder")
async def admin_reorder(request: Request):
    require_admin(request)
    body = await request.json()
    order = body.get("order") or []
    for i, pid in enumerate(order):
        execw("UPDATE photos SET sort=? WHERE id=?", (i, int(pid)))
    return {"ok": True, "count": len(order)}


@app.delete("/admin/api/photos/{pid}")
def admin_delete_photo(pid: int, request: Request):
    require_admin(request)
    rows = q("SELECT filename FROM photos WHERE id=?", (pid,))
    if not rows:
        raise HTTPException(404, "no such photo")
    execw("DELETE FROM photos WHERE id=?", (pid,))
    try:
        (MEDIA / rows[0]["filename"]).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


# ---------------------------------------------------------------- admin: feature board
import feature_coder  # noqa: E402  (local module)

FEATURE_STATUSES = {"new", "queued", "building", "needs_review", "approved",
                    "rejected", "failed"}


def _feature(fid: int) -> dict:
    rows = q("SELECT * FROM feature_request WHERE id=?", (fid,))
    if not rows:
        raise HTTPException(404, "no such request")
    return rows[0]


def _latest_attempt(fid: int) -> dict:
    rows = q("SELECT * FROM feature_attempt WHERE request_id=? ORDER BY id DESC LIMIT 1",
             (fid,))
    return rows[0] if rows else {}


@app.get("/admin/api/features/meta")
def features_meta(request: Request):
    require_admin(request)
    return {"coder": feature_coder.coder_status()}


@app.get("/admin/api/features")
def list_features(request: Request):
    require_admin(request)
    out = []
    for r in q("SELECT * FROM feature_request ORDER BY id DESC"):
        a = _latest_attempt(r["id"])
        r["attempt_status"] = a.get("status", "")
        r["has_diff"] = bool(a.get("diff"))
        out.append(r)
    qn = q("SELECT COUNT(*) AS n FROM feature_request WHERE status='queued'")[0]["n"]
    return {"features": out, "building": feature_coder.busy(), "queued": qn}


@app.get("/admin/api/features/{fid}")
def get_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    r["attempt"] = _latest_attempt(fid)
    return r


def _restate_async(fid: int):
    """Ask the model to restate the request so the owner can confirm intent."""
    try:
        r = _feature(fid)
        text = feature_coder.vet_request(r)
    except Exception as e:
        text = f"(couldn't reach the AI to restate this: {str(e)[:120]})"
    execw("UPDATE feature_request SET restatement=?, updated_at=? WHERE id=?",
          (text, now(), fid))


@app.post("/admin/api/features")
async def create_feature(request: Request):
    require_admin(request)
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "a title is required")
    fid = insertw(
        "INSERT INTO feature_request(title,description,page,status,created_at,updated_at)"
        " VALUES(?,?,?,'new',?,?)",
        (title[:200], (body.get("description") or "").strip(),
         (body.get("page") or "").strip()[:60], now(), now()))
    threading.Thread(target=_restate_async, args=(fid,), daemon=True).start()
    return {"id": fid}


@app.post("/admin/api/features/{fid}/vet")
def vet_feature(fid: int, request: Request):
    require_admin(request)
    _feature(fid)
    threading.Thread(target=_restate_async, args=(fid,), daemon=True).start()
    return {"ok": True}


@app.patch("/admin/api/features/{fid}")
async def edit_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    if r["status"] in ("queued", "building"):
        raise HTTPException(409, "can't edit while queued or building — pull it back first")
    body = await request.json()
    execw("UPDATE feature_request SET title=?, description=?, page=?, "
          "status=CASE WHEN status IN ('failed','rejected') THEN 'new' ELSE status END, "
          "updated_at=? WHERE id=?",
          ((body.get("title") or r["title"])[:200],
           body.get("description", r["description"]),
           (body.get("page") or r["page"])[:60], now(), fid))
    threading.Thread(target=_restate_async, args=(fid,), daemon=True).start()
    return {"ok": True}


@app.post("/admin/api/features/{fid}/build")
async def build_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    if r["status"] in ("queued", "building"):
        raise HTTPException(409, "already queued/building")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    backend = (body.get("backend") or feature_coder.DEFAULT_BACKEND)
    execw("UPDATE feature_request SET status='queued', backend=?, queued_at=?, "
          "updated_at=? WHERE id=?", (backend, now(), now(), fid))
    pos = q("SELECT COUNT(*) AS n FROM feature_request WHERE status='queued'")[0]["n"]
    return {"queued": True, "position": pos, "backend": backend}


@app.post("/admin/api/features/{fid}/unqueue")
def unqueue_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    if r["status"] != "queued":
        raise HTTPException(400, "only queued requests can be pulled back")
    execw("UPDATE feature_request SET status='new', queued_at=NULL, updated_at=? WHERE id=?",
          (now(), fid))
    return {"ok": True}


@app.post("/admin/api/features/{fid}/approve")
def approve_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    a = _latest_attempt(fid)
    if r["status"] != "needs_review" or not a.get("branch"):
        raise HTTPException(400, "nothing to approve")
    summary = feature_coder.approve(a["branch"])
    push = feature_coder.push_github()
    execw("UPDATE feature_request SET status='approved', updated_at=? WHERE id=?",
          (now(), fid))
    execw("UPDATE feature_attempt SET status='approved', updated_at=? WHERE id=?",
          (now(), a["id"]))
    _schedule_restart()               # bring the app up on the merged code
    return {"approved": True, "merge": summary, "push_error": push,
            "note": "The site will restart in a few seconds to load your change."}


@app.post("/admin/api/features/{fid}/reject")
def reject_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    a = _latest_attempt(fid)
    if a.get("branch"):
        feature_coder.reject(a["branch"])
    execw("UPDATE feature_request SET status='rejected', updated_at=? WHERE id=?",
          (now(), fid))
    if a:
        execw("UPDATE feature_attempt SET status='rejected', updated_at=? WHERE id=?",
              (now(), a["id"]))
    return {"ok": True}


@app.delete("/admin/api/features/{fid}")
def delete_feature(fid: int, request: Request):
    require_admin(request)
    r = _feature(fid)
    if r["status"] in ("queued", "building"):
        raise HTTPException(409, "can't delete while queued or building")
    a = _latest_attempt(fid)
    if a.get("branch"):
        feature_coder.reject(a["branch"])
    execw("DELETE FROM feature_attempt WHERE request_id=?", (fid,))
    execw("DELETE FROM feature_request WHERE id=?", (fid,))
    return {"ok": True}


# ---------------------------------------------------------------- build queue worker
def _schedule_restart(delay=1.5):
    """Exit so Docker's restart policy relaunches with the merged code."""
    threading.Timer(delay, lambda: os._exit(3)).start()


def _run_build(req: dict):
    aid = insertw(
        "INSERT INTO feature_attempt(request_id,status,backend,created_at,updated_at)"
        " VALUES(?,?,?,?,?)",
        (req["id"], "running", req.get("backend") or "", now(), now()))

    def update(fields: dict):
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now())
        vals.append(aid)
        execw(f"UPDATE feature_attempt SET {','.join(sets)} WHERE id=?", vals)
        if "status" in fields:
            execw("UPDATE feature_request SET status=?, updated_at=? WHERE id=?",
                  (fields["status"], now(), req["id"]))

    execw("UPDATE feature_request SET status='building', updated_at=? WHERE id=?",
          (now(), req["id"]))
    feature_coder.run_attempt(req, aid, update, req.get("backend") or "")


def _queue_worker():
    """Single worker: builds queued requests oldest-first, one at a time. The
    Kanban 'Building' column therefore has WIP limit 1 by construction — the
    local LLM (shared GPU) never gets more than one build at once."""
    # recover orphans from a mid-build restart
    execw("UPDATE feature_request SET status='queued', "
          "queued_at=COALESCE(queued_at,updated_at) WHERE status='building'")
    execw("UPDATE feature_attempt SET status='failed', "
          "error='interrupted by restart' WHERE status='running'")
    while True:
        try:
            rows = q("SELECT * FROM feature_request WHERE status='queued' "
                     "ORDER BY COALESCE(queued_at,''), id LIMIT 1")
            if rows:
                _run_build(rows[0])
            else:
                time.sleep(3)
        except Exception:
            time.sleep(5)


if RUN_QUEUE_WORKER:
    threading.Thread(target=_queue_worker, daemon=True).start()
