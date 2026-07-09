"""AI feature-request coder for the Lauren Davis Photography site.

A feature request comes in from the /admin board; this module lets a local LLM
attempt to build it. Every attempt runs in an isolated git worktree on its own
branch, is validated (compile checks + a boot smoke-test against a COPY of the
db), committed, and surfaced to the owner as a diff. Nothing reaches the live
code until she approves the merge — the AI never deploys on its own.

Adapted from the FMI app's feature_coder. Backends (FEATURE_CODERS env,
comma-separated, first is default):
  qwen  — a local llama.cpp OpenAI endpoint (default: AI3 qwen3.6-35b). We drive
          it: a PLAN call picks file regions from outlines, a PATCH call returns
          SEARCH/REPLACE blocks, one repair round if a block doesn't apply.
  claude/codex — the CLI edits the worktree itself (only if installed + logged in).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ---------- configuration ----------

def _repo_dir() -> Path:
    env = os.environ.get("SITE_REPO")
    if env and (Path(env) / ".git").exists():
        return Path(env)
    if env:
        return Path(env)
    return Path(__file__).resolve().parent

REPO = _repo_dir()
WORK = REPO / "feature_work"                     # gitignored worktree scratch
BACKENDS = [b.strip() for b in
            (os.environ.get("FEATURE_CODERS") or "qwen").split(",") if b.strip()]
DEFAULT_BACKEND = BACKENDS[0] if BACKENDS else "qwen"
LLM_URL = (os.environ.get("FEATURE_CODER_URL")
           or "http://10.100.0.13:8080/v1/chat/completions")
LLM_MODEL = os.environ.get("FEATURE_CODER_MODEL") or "qwen3.6-35b"
VET_URL = os.environ.get("FEATURE_VET_URL") or ""
VET_MODEL = os.environ.get("FEATURE_VET_MODEL") or ""
GIT_ID = ["-c", "user.name=LDP Feature Coder", "-c", "user.email=coder@laurendavisphoto"]

# the AI may only touch app source; never the harness, secrets, or data
EDITABLE = ["webapp.py", "index.html", "admin.html"]

APP_CONTEXT = """The app is a FastAPI + SQLite website for a photographer,
Lauren Davis. Backend: webapp.py (single file, plain SQL via q()/execw()/
insertw(), idempotent schema in ensure_schema()). Frontends are single-file
vanilla-JS pages served by webapp.py: index.html (public landing page — hero,
gallery, about, contact; renders text from /api/content and photos from
/api/photos) and admin.html (owner tools under /admin: content editor, photo
manager, feature board). House style: no frameworks, no build step, no new pip
dependencies, small helpers, theme follows the browser (prefers-color-scheme).
Owner-editable copy lives in the content table, not hardcoded in index.html."""

_LOCK = threading.Lock()          # one build at a time (shares the LLM GPU)


def _repo_guide(wt: Path) -> str:
    try:
        return (wt / "AGENTS.md").read_text(errors="replace")[:7000]
    except OSError:
        return APP_CONTEXT


# ---------- small utils ----------

def _git(args, cwd=REPO, check=True, timeout=120):
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _backend_status(backend: str) -> dict:
    out = {"id": backend, "ready": False, "detail": "",
           "label": {"qwen": f"Local AI ({LLM_MODEL})",
                     "claude": "Claude Code",
                     "codex": "Codex"}.get(backend, backend)}
    if backend == "qwen":
        try:
            req = urllib.request.Request(LLM_URL.replace("/chat/completions", "/models"))
            urllib.request.urlopen(req, timeout=4).read()
            out["ready"] = True
        except Exception as e:
            out["detail"] = f"LLM endpoint unreachable: {e}"
    elif backend in ("claude", "codex"):
        if not shutil.which(backend):
            out["detail"] = f"'{backend}' CLI not found on PATH"
        elif backend == "codex":
            try:
                r = subprocess.run(["codex", "login", "status"],
                                   capture_output=True, text=True, timeout=10)
                out["ready"] = r.returncode == 0
                if not out["ready"]:
                    out["detail"] = "codex is installed but not logged in"
            except Exception as e:
                out["detail"] = f"codex check failed: {e}"
        else:
            out["ready"] = True
    else:
        out["detail"] = f"unknown backend '{backend}'"
    return out


def coder_status() -> dict:
    if not (REPO / ".git").exists():
        return {"backends": [], "default": "", "detail": f"no git repo at {REPO}"}
    return {"backends": [_backend_status(b) for b in BACKENDS],
            "default": DEFAULT_BACKEND, "detail": ""}


def busy() -> bool:
    if _LOCK.acquire(blocking=False):
        _LOCK.release()
        return False
    return True


# ---------- LLM plumbing (qwen backend) ----------

def _chat(messages, max_tokens=10000, temperature=0.3, timeout=900,
          url="", model="") -> str:
    body = json.dumps({"model": model or LLM_MODEL, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(url or LLM_URL, data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    if not content:
        raise RuntimeError("LLM returned no content (reasoning ate the token budget?)")
    return content


def vet_request(req: dict) -> str:
    ask = (f"Title: {req['title']}\nDetails: {req.get('description') or '(none)'}\n"
           f"Target area: {req.get('page') or 'unspecified'}")
    msgs = [
        {"role": "system", "content":
         f"{APP_CONTEXT}\nThe photographer filed a request for her website. "
         "Restate what you will build in plain, non-technical words she can "
         "verify — 1 to 3 sentences, starting with \"I'm going to\". Name the "
         "page or section concretely. If the request is ambiguous or bundles "
         "unrelated changes, add ONE short sentence starting with \"Note:\" "
         "saying what is unclear. Output only that statement, nothing else."},
        {"role": "user", "content": ask}]
    if VET_URL or VET_MODEL:
        try:
            return _chat(msgs, max_tokens=2500, temperature=0.2, timeout=180,
                         url=VET_URL, model=VET_MODEL)
        except Exception:
            pass
    return _chat(msgs, max_tokens=2500, temperature=0.2)


def _outline(path: Path) -> str:
    pats = (re.compile(r"^(def |class |@app\.|# -+|ensure_schema|[A-Z_]{3,} = )")
            if path.suffix == ".py" else
            re.compile(r"(<dialog |<section |<div id=|<template|function [A-Za-z_]|"
                       r"/\* -+|^const [A-Za-z_]+\s*=|addEventListener\('submit'|id=\")"))
    lines = path.read_text(errors="replace").splitlines()
    picked = [f"{i}: {ln.strip()[:150]}" for i, ln in enumerate(lines, 1) if pats.search(ln)]
    return f"### {path.name} — {len(lines)} lines\n" + "\n".join(picked)


def _numbered(path: Path, start: int, end: int) -> str:
    lines = path.read_text(errors="replace").splitlines()
    start = max(1, start); end = min(len(lines), end)
    body = "\n".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))
    return f"### {path.name} lines {start}-{end}\n{body}"


_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<file>\S+)\s*\n<{5,}\s*SEARCH\s*\n(?P<search>.*?)\n={5,}\s*\n"
    r"(?P<replace>.*?)\n>{5,}\s*REPLACE", re.S)


def _apply_blocks(text: str, wt: Path, log):
    blocks = list(_BLOCK_RE.finditer(text))
    if not blocks:
        raise RuntimeError("model produced no SEARCH/REPLACE blocks")
    for b in blocks:
        name = Path(b["file"]).name
        if name not in EDITABLE:
            raise RuntimeError(f"model tried to edit non-editable file: {name}")
        f = wt / name
        content = f.read_text(errors="replace")
        search, replace = b["search"], b["replace"]
        if content.count(search) == 0:
            norm = "\n".join(ln.rstrip() for ln in search.splitlines())
            cnorm = "\n".join(ln.rstrip() for ln in content.splitlines())
            if cnorm.count(norm) == 1:
                i = cnorm.index(norm)
                pre_lines = cnorm[:i].count("\n")
                lines = content.splitlines(keepends=True)
                n = len(norm.splitlines())
                orig = "".join(lines[pre_lines:pre_lines + n]).rstrip("\n")
                content = content.replace(orig, replace, 1)
                f.write_text(content)
                log(f"  applied (whitespace-normalized) block in {name}")
                continue
            raise RuntimeError(f"SEARCH block not found in {name}:\n{search[:400]}")
        if content.count(search) > 1:
            raise RuntimeError(f"SEARCH block ambiguous ({content.count(search)} matches) in {name}")
        f.write_text(content.replace(search, replace, 1))
        log(f"  applied block in {name}")


def _qwen_build(req: dict, wt: Path, log) -> None:
    ask = f"Feature request: {req['title']}\n\nDetails: {req.get('description') or '(none)'}"
    if req.get("page"):
        ask += f"\nTarget area: {req['page']}"

    files = [wt / n for n in EDITABLE if (wt / n).exists()]
    guide = _repo_guide(wt)
    log("planning: sending file outlines to the model…")
    plan_raw = _chat([
        {"role": "system", "content":
         f"You are an expert software engineer.\n{guide}\n"
         "You will be shown numbered OUTLINES of the source files. Decide which "
         "line regions you need to read to implement the request. Respond with "
         "ONLY a JSON object: {\"regions\": [{\"file\": \"name\", \"start\": N, "
         "\"end\": N}, ...]} — at most 8 regions, at most 700 total lines. "
         "Pick regions that cover both where similar features live (to copy "
         "style) and where your edits will go."},
        {"role": "user", "content": ask + "\n\n" + "\n\n".join(_outline(f) for f in files)},
    ])
    m = re.search(r"\{.*\}", plan_raw, re.S)
    if not m:
        raise RuntimeError(f"plan was not JSON: {plan_raw[:300]}")
    regions = json.loads(m.group(0)).get("regions", [])[:8]
    if not regions:
        raise RuntimeError("model picked no regions to read")
    log("model wants to read: " + ", ".join(
        f"{r['file']}:{r['start']}-{r['end']}" for r in regions))

    total = 0
    excerpts = []
    for r in regions:
        name = Path(r["file"]).name
        if name not in EDITABLE:
            continue
        start, end = int(r["start"]) - 10, int(r["end"]) + 10
        if total + (end - start) > 900:
            log(f"  skipping {name}:{start}-{end} (region budget reached)")
            continue
        total += end - start
        excerpts.append(_numbered(wt / name, start, end))

    patch_sys = (
        f"You are an expert software engineer making a real change.\n{guide}\n"
        "Implement the request by editing the files shown. Respond with ONLY "
        "edit blocks in this exact format (repeat per edit; nothing else):\n"
        "FILE: <filename>\n<<<<<<< SEARCH\n<exact existing lines, no line "
        "numbers>\n=======\n<replacement lines>\n>>>>>>> REPLACE\n"
        "Rules: SEARCH must copy the current file text EXACTLY (the line-number "
        "prefixes in the excerpts are not part of the file). Keep each SEARCH "
        "under ~25 lines and unique within its file. Make the smallest change "
        "that cleanly implements the feature, matching the file's existing "
        "style. Do not rename existing ids/functions/columns, do not add "
        "dependencies, do not touch files other than: " + ", ".join(EDITABLE))
    log("patching: asking the model for edits…")
    patch = _chat([{"role": "system", "content": patch_sys},
                   {"role": "user", "content": ask + "\n\n" + "\n\n".join(excerpts)}],
                  max_tokens=12000)
    try:
        _apply_blocks(patch, wt, log)
    except RuntimeError as e:
        log(f"apply failed ({e}); asking the model to repair…")
        patch = _chat([{"role": "system", "content": patch_sys},
                       {"role": "user", "content":
                        ask + "\n\n" + "\n\n".join(excerpts) +
                        "\n\nYour previous edits failed to apply:\n" + str(e)[:800] +
                        "\n\nPrevious response:\n" + patch[:4000] +
                        "\n\nResend ALL edit blocks, corrected."}],
                      max_tokens=12000)
        _apply_blocks(patch, wt, log)


def _cli_build(backend: str, req: dict, wt: Path, log) -> None:
    prompt = ("Read AGENTS.md first — it explains this codebase, the patterns "
              "to copy, and the hard rules.\n\n"
              "Implement this feature request from the site owner:\n"
              f"TITLE: {req['title']}\nDETAILS: {req.get('description') or '(none)'}\n"
              f"Target area: {req.get('page') or 'wherever it fits'}\n"
              f"Only edit these files: {', '.join(EDITABLE)}. Match existing "
              "style, no new dependencies, smallest clean change. Do not run "
              "servers, commit, or push; just edit the files.")
    cmd = (["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
           if backend == "claude" else
           ["codex", "exec", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", prompt])
    log(f"running {backend} CLI in the worktree…")
    r = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=2400)
    log((r.stdout or "")[-3000:])
    if r.returncode != 0:
        raise RuntimeError(f"{backend} exited {r.returncode}: {(r.stderr or '')[-800:]}")


# ---------- validation ----------

def _validate(wt: Path, changed: list, log):
    bad = [f for f in changed if Path(f).name not in EDITABLE]
    if bad:
        raise RuntimeError(f"disallowed files changed: {', '.join(bad)}")
    for f in changed:
        p = wt / f
        if p.suffix == ".py":
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"python syntax error in {f}:\n{r.stderr[-1200:]}")
            log(f"  ✓ py_compile {f}")
        elif p.suffix == ".html":
            node = shutil.which("node")
            if not node:
                log(f"  ~ node not installed; skipping JS check for {f}")
                continue
            for i, m in enumerate(re.finditer(r"<script>(.*?)</script>",
                                              p.read_text(errors="replace"), re.S)):
                tmp = wt / f".jscheck{i}.js"
                tmp.write_text(m.group(1))
                r = subprocess.run([node, "--check", str(tmp)],
                                   capture_output=True, text=True)
                tmp.unlink()
                if r.returncode != 0:
                    raise RuntimeError(f"JS syntax error in {f}:\n{r.stderr[-1200:]}")
            log(f"  ✓ node --check {f}")


def _smoke_test(wt: Path, log):
    """Boot the patched app against a copy of the db; hit the main pages."""
    data_src = Path(os.environ.get("SITE_DATA", str(REPO / "data")))
    sd = wt / ".smoke_data"
    (sd / "media").mkdir(parents=True, exist_ok=True)
    if (data_src / "site.db").exists():
        shutil.copy2(data_src / "site.db", sd / "site.db")
    # SITE_QUEUE_WORKER=0: the patched app must NOT start its own build queue
    # (it would see the copied db's queued rows and build inside the smoke test).
    # ADMIN_EMAILS empty: so /admin is reachable (require_admin allows when no
    # allow-list and no Access header).
    env = {**os.environ, "SITE_DATA": str(sd), "SITE_QUEUE_WORKER": "0",
           "ADMIN_EMAILS": ""}
    port = "8907"
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "webapp:app",
                             "--host", "127.0.0.1", "--port", port],
                            cwd=str(wt), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 40
        last = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read()[-1500:] if proc.stdout else ""
                raise RuntimeError(f"patched app crashed on boot:\n{out}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                       timeout=2).read()
                break
            except Exception as e:
                last = str(e)
                time.sleep(1)
        else:
            raise RuntimeError(f"patched app never came up: {last}")
        for path in ("/", "/admin", "/api/content", "/api/photos"):
            code = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                          timeout=10).status
            if code != 200:
                raise RuntimeError(f"GET {path} returned {code} on the patched app")
            log(f"  ✓ GET {path} → 200")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(sd, ignore_errors=True)


# ---------- the attempt runner ----------

def run_attempt(req: dict, attempt_id: int, update, backend: str = ""):
    backend = backend or DEFAULT_BACKEND
    if not _LOCK.acquire(blocking=False):
        update({"status": "failed", "error": "another build is already running"})
        return
    loglines = []

    def log(msg):
        loglines.append(msg)
        update({"log": "\n".join(loglines)})

    branch = f"feature/req{req['id']}-a{attempt_id}"
    wt = WORK / f"req{req['id']}-a{attempt_id}"
    try:
        WORK.mkdir(exist_ok=True)
        _git(["worktree", "prune"])
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        _git(["branch", "-D", branch], check=False)
        _git(["worktree", "add", "-b", branch, str(wt), "main"])
        log(f"worktree ready on branch {branch}")
        update({"branch": branch})

        if backend not in BACKENDS:
            raise RuntimeError(f"backend '{backend}' is not enabled (FEATURE_CODERS={','.join(BACKENDS)})")
        log(f"coder backend: {backend}")
        if backend == "qwen":
            _qwen_build(req, wt, log)
        elif backend in ("claude", "codex"):
            _cli_build(backend, req, wt, log)
        else:
            raise RuntimeError(f"unknown coder backend '{backend}'")

        changed = [ln[3:].strip() for ln in
                   _git(["status", "--porcelain"], cwd=wt).splitlines() if ln.strip()]
        if not changed:
            raise RuntimeError("the model made no changes")
        log("changed: " + ", ".join(changed))
        _validate(wt, changed, log)
        log("smoke test: booting the patched app on a copy of the db…")
        _smoke_test(wt, log)

        _git(["add", "-A"], cwd=wt)
        _git(GIT_ID + ["commit", "-m",
                       f"feature request #{req['id']}: {req['title']} [{backend}]"], cwd=wt)
        diff = _git(["diff", "--no-color", "main...HEAD"], cwd=wt)
        log("committed; awaiting your review")
        update({"status": "needs_review", "diff": diff[:300_000],
                "files": json.dumps(changed)})
    except Exception as e:
        log(f"FAILED: {e}")
        update({"status": "failed", "error": str(e)[:2000]})
        _git(["branch", "-D", branch], check=False)
    finally:
        if wt.exists():
            _git(["worktree", "remove", "--force", str(wt)], check=False)
            shutil.rmtree(wt, ignore_errors=True)
        _git(["worktree", "prune"], check=False)
        _LOCK.release()


# ---------- review actions (called from webapp) ----------

def approve(branch: str) -> str:
    if not branch or not branch.startswith("feature/"):
        raise RuntimeError("bad branch")
    dirty = _git(["status", "--porcelain"]).strip()
    changed_by_branch = _git(["diff", "--name-only", f"main...{branch}"]).split()
    conflict = [f for f in changed_by_branch
                if any(ln[3:].strip() == f for ln in dirty.splitlines())]
    if conflict:
        raise RuntimeError("uncommitted local edits in " + ", ".join(conflict) +
                           " — commit or stash them first")
    out = _git(GIT_ID + ["merge", "--no-ff", branch, "-m", f"approve {branch}"])
    _git(["branch", "-d", branch], check=False)
    return out.strip()


def reject(branch: str):
    if branch and branch.startswith("feature/"):
        _git(["branch", "-D", branch], check=False)


def push_github() -> str:
    """Best-effort mirror push after a merge. Returns '' on success or a short
    error — never raises; the merge is done and must not roll back."""
    try:
        if not _git(["remote"], check=False).split().count("origin"):
            return "no 'origin' remote configured yet"
        r = subprocess.run(["git", "push", "origin", "main"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=90)
        return "" if r.returncode == 0 else (r.stderr.strip() or "push failed")[-300:]
    except Exception as e:
        return str(e)[-300:]
