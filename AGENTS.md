# AGENTS.md — briefing for AI coders working on this repo

This is **Lauren Davis Photography** — a small, elegant public website plus a
private admin area, built so the owner (a photographer, not a developer) can run
it herself. You are the AI that implements her feature requests. Read this fully
before editing.

## What the site is
- A **public landing page** (`index.html`): hero, photo gallery, about, contact.
  It is a single self-contained HTML file with inline CSS + vanilla JS. No
  frameworks, no build step. It renders its text and photos by `fetch()`ing the
  backend APIs — so the *words and images* are data (owner-editable in /admin),
  while the *layout and design* live in this file (that's what you edit).
- A private **admin** page (`admin.html`), same single-file style, reached at
  `/admin` (gated by Cloudflare Access). Tabs: Content (edit text), Photos
  (upload/caption/reorder/delete), and Requests (this feature board).
- A **FastAPI backend** (`webapp.py`, single file, plain SQL via the helpers
  `q()`, `execw()`, `insertw()`; idempotent schema in `ensure_schema()`).

## House style (match it exactly)
- No frameworks, no bundler, no new pip dependencies. Vanilla JS + `fetch`.
- Small helpers over abstractions. Keep changes minimal and local.
- The public site must stay fast and clean. It is a photographer's storefront —
  typography and whitespace matter; never make it look busy or "appy".
- **Theme follows the browser** (`prefers-color-scheme`) with a manual toggle.
  Preserve that. Don't hardcode colors that break dark mode — use the CSS
  variables already defined in `:root` / the theme blocks.
- Content the owner edits is stored under `content` keys and photos in the
  `photos` table. If you add a new editable text area, add it as a new content
  key with a sensible default in `DEFAULT_CONTENT` and surface it in the admin
  Content tab — never hardcode owner-facing copy in `index.html`.

## Hard rules
- You may ONLY edit these files: `webapp.py`, `index.html`, `admin.html`.
- Never touch: `feature_coder.py`, `.env`, anything under `data/`, secrets, or
  the deploy key. Never add dependencies. Never rename existing routes, DB
  columns, content keys, or element IDs that other code relies on.
- Admin-only endpoints live under the `/admin/` prefix and use the
  `require_admin` dependency. Keep new admin endpoints under `/admin/` so
  Cloudflare Access keeps protecting them. Public endpoints stay read-only.
- Do not run servers, commit, or push — the harness validates, boot-tests,
  commits, and (on the owner's approval) merges + pushes to GitHub for you.

## Where things are (webapp.py)
- `ensure_schema()` — tables: `content`, `photos`, `feature_request`,
  `feature_attempt`. Add columns idempotently here if you truly need them.
- `DEFAULT_CONTENT` — seed text keys.
- Public routes: `/`, `/api/content`, `/api/photos`, `/media/{file}`, `/healthz`.
- Admin routes: `/admin`, `/admin/api/...` (content, photos, features).
- The feature-request queue worker builds one request at a time (WIP-limit 1).

Make the smallest clean change that implements the request, in the file's
existing style. When in doubt, copy the nearest existing pattern.
