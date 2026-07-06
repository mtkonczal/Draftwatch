# Terminal Panel — Implementation Plan

Ship an embedded terminal as a third, closable panel so a CLI agent (Claude
Code, Codex, or any shell command) can run next to the editor and diff view.
Stays pure Python stdlib + vendored JS, consistent with the existing design.

**Explicit non-goals**
- No snapshots or baseline changes when commands run. Edits pile up against
  whatever baseline is selected; the existing watcher/diff loop is untouched.
- No Windows terminal in v1. Windows gets the current two-panel app unchanged
  (see "Platform gating" — one codebase, not a separate version).

---

## 1. Architecture

**Backend: `draftwatch/term.py`** (new module, imported lazily; `app.py` stays
the single source of routes and HTML).

- One PTY session max (single-user tool). `pty.openpty()` +
  `subprocess.Popen([shell], cwd=root, start_new_session=True)` where shell is
  `$SHELL` or `/bin/sh`. The user launches `claude`/`codex` themselves —
  agent-agnostic, and we never construct command strings server-side.
- A reader thread pumps the PTY master fd into a bounded scrollback buffer
  (~200 KB) and fan-out queues, mirroring the existing SSE subscribe pattern.
- Lifecycle: on close/shutdown, SIGHUP the process group, SIGKILL after a
  short grace. Registered on ctrl-c, `httpd.shutdown()`, and the pywebview
  window-close path. No orphans.

**Transport** (no WebSockets; consistent with current design):

| Route | Method | Purpose |
|---|---|---|
| `/api/term/open` | POST | start (or attach to) the session; returns `{supported, running}` |
| `/api/term/input` | POST | raw keystrokes `{data}` → PTY master (bytes piped, never parsed) |
| `/api/term/resize` | POST | `{cols, rows}` → `TIOCSWINSZ` ioctl |
| `/api/term/close` | POST | terminate the process group |
| `/term/events` | GET (SSE) | output stream; replays scrollback on (re)connect |

Output chunks are JSON-string-encoded (UTF-8 with replacement) — fine for
human-speed terminal use; keeps the zero-dependency property.

**Frontend**

- Vendor **xterm.js + fit addon** via the existing `scripts/build_vendor.mjs`
  esbuild pipeline; add `xterm.js` and `xterm.css` to `STATIC_ASSETS`; pin in
  `package.json` so `npm audit` covers it. No CDN, works offline.
- Layout: toolbar button "terminal" adds `body.term-open`; grid goes
  `1fr 1fr` → `1fr 1fr minmax(360px, 0.8fr)`. Panel header has:
  - **hide** (collapses the panel; shell keeps running — an agent mid-task
    survives), and
  - **end session** (kills the process group; confirm if a child is running).
  Reopening while running just re-attaches and replays scrollback.

## 2. Security (the gates, in order of importance)

1. **Loopback-only, by construction.** Remove the `--host` flag entirely
   (pre-1.0, no users to break): the server always binds `127.0.0.1`. The
   whole app — not just the terminal — is unreachable from the network, the
   "exposed on your network" warning path and its docs go away, and
   `allowed_hosts` reduces to the two loopback forms. A shell is never
   network-exposed because *nothing* is.
2. **Existing `_guard` on every terminal route**: exact Host allowlist (DNS
   rebinding), Origin check (cross-site XHR), per-session token with
   `secrets.compare_digest`.
3. **Header-only token for input.** `/api/term/input|resize|open|close` accept
   the token only via `X-Draftwatch-Token`, not the query string (POSTs can
   set headers; only EventSource needs the `?t=` fallback). Keystrokes never
   ride in URLs.
4. **No server-side interpretation.** Input bytes go straight to the PTY fd.
   No `shell=True` string building, no command API, nothing to inject into.
5. **Kill switch:** `--no-terminal` flag disables the feature server-side
   (routes 404, button hidden). Default: enabled on supported platforms,
   panel closed until opened.
6. **Hostile-output safety** is xterm.js's job (it is built for untrusted
   escape sequences); we keep it pinned + vendored and in the audit pass.
7. Optional hardening (separate commit): add a `Content-Security-Policy`
   header to `/` (`default-src 'self'; …`) — benefits the whole app, not just
   the terminal.

## 3. Platform gating (answers the Windows question)

No separate "backup version" — one codebase that degrades gracefully:

```python
try:
    import pty, termios, fcntl
    TERM_SUPPORTED = True
except ImportError:          # Windows
    TERM_SUPPORTED = False
```

When unsupported: terminal routes report `{supported: false}`, the toolbar
button never renders, and the app is byte-for-byte today's behavior. Windows
users lose nothing they have now; there is no second release to maintain. If
Windows terminal demand materializes later, ConPTY (via `pywinpty` as an
optional extra, like `[app]`) slots in behind the same interface.

## 4. Work sequence

0. **Remove `--host`** — always bind loopback. Delete the argparse option,
   the non-loopback `allowed_hosts` branch, the network-exposure warning, and
   the README mention; update the acceptance test that exercises it. Ship as
   its own commit before the terminal work so the security baseline is clean.
1. **Vendor xterm.js** — `package.json`, `build_vendor.mjs`, `STATIC_ASSETS`,
   rebuild, audit. (Small, isolated, unblocks everything.)
2. **`term.py`** — session class: spawn, reader thread, buffer, resize,
   terminate; unit-testable without HTTP.
3. **Routes + guards** — wire into `Handler`, loopback-only check,
   header-only token, `--no-terminal`, shutdown hooks (incl. pywebview path).
4. **Frontend panel** — xterm bootstrap, fit-on-resize, SSE reconnect with
   scrollback replay, hide vs. end-session UX, `no-repo` mode coexistence
   (terminal works in write-only mode too — useful for "agent drafts a new
   doc from scratch").
5. **Tests**
   - Acceptance: open → `echo hi` → output in SSE; 403 without token; 403
     with query-string token on input; routes absent under `--no-terminal`;
     `--host` is no longer accepted (argparse rejects it); close kills the
     process group (probe with `os.kill(pid, 0)`); `TERM_SUPPORTED=False`
     forces the Windows path.
   - Frontend smoke: button hidden when unsupported; panel open/hide/close
     class toggles; no-repo + terminal together.
6. **Docs + release** — README section (with a security paragraph), CHANGELOG
   under `0.2.0` (move the current Unreleased notes under `0.1.1` while
   there — they were left behind at the version bump), `RELEASING.md` steps.

Estimated size: ~250 lines Python, ~200 lines JS/CSS, plus vendored assets.

## 5. Decisions locked in

- No auto-snapshots on terminal activity (explicitly rejected).
- Panel is closable; **hide** preserves the running shell, **end session**
  kills it.
- Spawn the user's shell, not a specific LLM binary.
- `--host` removed; the app binds `127.0.0.1` unconditionally (pre-1.0, no
  users to break).
- Single session, loopback-only, POSIX-only in v1.
- Windows = current app via feature gating, not a fork.
