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

# Contact-form email delivery. Two transports, both stdlib (no extra dep, no
# image rebuild): SMTP (Gmail/iCloud/any — preferred when configured) and the
# Resend HTTPS API (fallback). If neither is configured the form still works:
# every enquiry is stored in the DB and readable in /admin; email just stays
# deferred (emailed=0) until credentials are set.
#
# SMTP (e.g. Google Workspace): SMTP_HOST=smtp.gmail.com SMTP_PORT=587
#   SMTP_USER=lauren.davis@dw-r.com SMTP_PASS=<16-char app password>
# iCloud is the same with SMTP_HOST=smtp.mail.me.com.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()

# From/To are shared across transports; the RESEND_* names are kept as fallbacks
# so nothing breaks if only the old vars are set.
MAIL_FROM = os.environ.get(
    "MAIL_FROM",
    os.environ.get("RESEND_FROM", "Lauren Davis Photography <lauren.davis@dw-r.com>"))
MAIL_TO = os.environ.get(
    "MAIL_TO", os.environ.get("RESEND_TO", "lauren.davis@dw-r.com"))


def mail_configured() -> bool:
    """True if any email transport is set up (SMTP creds or a Resend key)."""
    return bool((SMTP_HOST and SMTP_USER and SMTP_PASS) or RESEND_API_KEY)

MAX_UPLOAD = 25 * 1024 * 1024           # 25 MB per photo
MAX_EDGE = 2400                          # downscale longest side to this for web
ALLOWED_IMG = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_WLOCK = threading.Lock()                # serialize sqlite writes

# ---------------------------------------------------------------- default copy
DEFAULT_CONTENT = {
    "site_title":    "Lauren Davis Photography",
    "nav_brand":     "Lauren Davis",
    "hero_heading":  "Lauren Davis Photography",
    "hero_sub":      "Honest, timeless photographs of the people and moments you love.",
    "hero_cta":      "Send an enquiry",
    "intro_body":    ("Some moments are too good to let slip by. I make photographs "
                      "that hold onto them — the quiet in-between and the joy out "
                      "loud — so the feeling stays long after the day is done."),
    "about_heading": "Get to Know the Girl Behind the Camera",
    "about_body":    ("I'm Lauren, a photographer who loves natural light and "
                      "honest moments. Whether it's a wedding, a family session, "
                      "or a portrait, my goal is simple: photographs you'll "
                      "treasure for years."),
    "gallery_heading": "Portfolio",
    "gallery_sub":   "A little of the work I love making.",
    "services_heading": "Services",
    "services_sub":  "However you'd like to be remembered, there's a session for it.",
    "services_note": "I can be commissioned for other types of events or fine art! Just send an enquiry and we can make a plan together!",
    "svc1_name":     "Weddings",
    "svc1_desc":     "The whole day, told honestly — the vows, the tears, and the dance floor at midnight.",
    "svc2_name":     "Newborn & Baby",
    "svc2_desc":     "Gentle, unhurried sessions for your newest and tiniest arrival.",
    "svc3_name":     "Family",
    "svc3_desc":     "The everyday magic of your people, exactly as they are right now.",
    "svc4_name":     "Maternity",
    "svc4_desc":     "Celebrating the anticipation and the glow of this in-between season.",
    "svc5_name":     "Birth",
    "svc5_desc":     "A gentle, respectful presence for the arrival of your baby.",
    "svc6_name":     "Graduation",
    "svc6_desc":     "Celebrating the milestones and hard work — from kindergarten to college, captured with pride.",
    "svc2_price":    "$350",
    "svc3_price":    "$250",
    "svc4_price":    "$350",
    "svc5_price":    "$1,000",
    "svc6_price":    "$250",
    "wed_pkg1_name": "Engagement Only",
    "wed_pkg1_price": "$250",
    "wed_pkg2_name": "Ceremony Only",
    "wed_pkg2_price": "$500",
    "wed_pkg3_name": "Getting Ready + Ceremony + Reception",
    "wed_pkg3_price": "$1,000",
    "wed_pkg4_name": '"The Perfect Day" Package',
    "wed_pkg4_price": "$1,500",
    "wed_pkg4_note": "($200 off)",
    "wedding_packages_intro": "Choose a package that fits your day:",
    "bundle_name":   "The Bundle of Joy",
    "bundle_desc":   "Maternity shoot, birth session, and newborn shoot.",
    "bundle_price":  "$1,500",
    "bundle_note":   "$200 off rather than booking each.",
    "perfect_day_name": "The Perfect Day Package",
    "perfect_day_desc": "Engagement photos + your wedding day from beginning to end.",
    "contact_heading": "Let's work together!",
    "contact_body":  ("Tell me a little about what you have in mind — I read every "
                      "message myself and I'll be in touch soon."),
    "contact_form_note": "I'll never share your details, and there's no obligation to book.",
    "form_success":  "Thank you — your message is on its way to Lauren. She'll be in touch soon. ✨",
    "contact_email": "lauren.davis@dw-r.com",
    "instagram":     "",
    "footer":        "© Lauren Davis Photography",
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
    execw("""CREATE TABLE IF NOT EXISTS enquiry(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT DEFAULT '',
        service TEXT DEFAULT '', message TEXT DEFAULT '',
        ip TEXT DEFAULT '', emailed INTEGER DEFAULT 0, created_at TEXT)""")
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


# ---------------------------------------------------------------- contact form
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RATE = {}                                # ip -> [timestamps]; naive in-proc limiter
_RATE_LOCK = threading.Lock()
_RATE_MAX = 5                             # max submissions ...
_RATE_WINDOW = 600                        # ... per 10 minutes per IP


def _rate_ok(ip: str) -> bool:
    if not ip:
        return True
    cut = time.time() - _RATE_WINDOW
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if t > cut]
        if len(hits) >= _RATE_MAX:
            _RATE[ip] = hits
            return False
        hits.append(time.time())
        _RATE[ip] = hits
        return True


def send_enquiry_email(e: dict) -> bool:
    """Best-effort delivery. Prefers SMTP (Gmail/iCloud/any, stdlib smtplib);
    falls back to the Resend HTTPS API. Returns True on success, False otherwise
    (including when no transport is configured)."""
    lines = [
        "New enquiry from your website",
        "",
        f"Name:    {e['name']}",
        f"Email:   {e['email']}",
        f"Phone:   {e.get('phone') or '—'}",
        f"Service: {e.get('service') or '—'}",
        "",
        "Message:",
        f"{e.get('message') or ''}",
        "",
        f"— Reply straight to this email to reach {e['name']}.",
    ]
    subject = f"New enquiry — {e.get('service') or 'general'} — {e['name']}"
    text = "\n".join(lines)

    # Preferred: SMTP (Google Workspace / iCloud / any). stdlib only.
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = MAIL_TO
        msg["Reply-To"] = e["email"]
        msg.set_content(text)
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            return True
        except Exception:
            return False

    # Fallback: Resend HTTPS API.
    if RESEND_API_KEY:
        import urllib.request
        import urllib.error
        payload = {
            "from": MAIL_FROM,
            "to": [MAIL_TO],
            "reply_to": e["email"],
            "subject": subject,
            "text": text,
        }
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    return False


@app.post("/api/contact")
async def contact(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "bad request")
    # Honeypot: bots fill hidden fields. Pretend success, store nothing.
    if (body.get("website") or body.get("company") or "").strip():
        return {"ok": True}
    name = (body.get("name") or "").strip()[:120]
    email = (body.get("email") or "").strip()[:200]
    phone = (body.get("phone") or "").strip()[:60]
    service = (body.get("service") or "").strip()[:80]
    message = (body.get("message") or "").strip()[:5000]
    if not name or not email or not message:
        raise HTTPException(400, "Please fill in your name, email and a message.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "That email address doesn't look right.")
    ip = (request.headers.get("cf-connecting-ip")
          or (request.client.host if request.client else "") or "")
    if not _rate_ok(ip):
        raise HTTPException(429, "You've sent a few messages already — please try again shortly.")
    eid = insertw(
        "INSERT INTO enquiry(name,email,phone,service,message,ip,emailed,created_at)"
        " VALUES(?,?,?,?,?,?,0,?)",
        (name, email, phone, service, message, ip, now()))
    sent = send_enquiry_email(
        {"name": name, "email": email, "phone": phone,
         "service": service, "message": message})
    if sent:
        execw("UPDATE enquiry SET emailed=1 WHERE id=?", (eid,))
    return {"ok": True, "id": eid}


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


# ---------------------------------------------------------------- admin: enquiries
@app.get("/admin/api/enquiries")
def admin_list_enquiries(request: Request):
    require_admin(request)
    rows = q("SELECT * FROM enquiry ORDER BY id DESC")
    return {"enquiries": rows, "mail_configured": mail_configured()}


@app.post("/admin/api/enquiries/{eid}/resend")
def admin_resend_enquiry(eid: int, request: Request):
    require_admin(request)
    rows = q("SELECT * FROM enquiry WHERE id=?", (eid,))
    if not rows:
        raise HTTPException(404, "no such enquiry")
    if not mail_configured():
        raise HTTPException(400, "Email isn't configured yet (no SMTP or Resend credentials).")
    sent = send_enquiry_email(rows[0])
    if sent:
        execw("UPDATE enquiry SET emailed=1 WHERE id=?", (eid,))
    return {"ok": sent}


@app.delete("/admin/api/enquiries/{eid}")
def admin_delete_enquiry(eid: int, request: Request):
    require_admin(request)
    if not q("SELECT 1 FROM enquiry WHERE id=?", (eid,)):
        raise HTTPException(404, "no such enquiry")
    execw("DELETE FROM enquiry WHERE id=?", (eid,))
    return {"ok": True}


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
