#!/usr/bin/env python3
"""draftwatch — watch a single .md file and review an agent's edits as a real git diff.

A read-and-review tool for writers whose .md files are being edited by an
autonomous AI agent. It shows the exact, git-backed diff of what changed and lets
the writer keep or revert the agent's changes hunk by hunk, or write into the file directly.

The diff is always real `git diff --word-diff`, never a JS/Python approximation.
"""

import argparse
import hashlib
import html
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Single source of truth for the version and the date of the latest release.
# __init__.py re-exports __version__; the About panel shows both (injected into
# the served page from these constants).
__version__ = "0.1.0"
RELEASE_DATE = "2026-07-02"

# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #

def run_git(args, cwd):
    """Run a git command with an argument list (never shell=True). Returns (rc, out, err)."""
    p = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


def git_toplevel(cwd):
    rc, out, _ = run_git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        return None
    return os.path.realpath(out.strip())


def is_inside_work_tree(cwd):
    rc, out, _ = run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0 and out.strip() == "true"


def is_tracked(root, relpath):
    rc, _, _ = run_git(["ls-files", "--error-unmatch", "--", relpath], root)
    return rc == 0


def head_label(root):
    """Return (short_sha, relative_date) for HEAD, or (None, None) if no commits."""
    rc, out, _ = run_git(["log", "-1", "--format=%h%x09%cr"], root)
    if rc != 0 or not out.strip():
        return None, None
    parts = out.strip().split("\t")
    if len(parts) == 2:
        return parts[0], parts[1]
    return out.strip(), ""


def commit_list(root, relpath):
    """Recent commits touching the file: list of {ref, label}."""
    rc, out, _ = run_git(
        ["log", "-n", "30", "--format=%H%x09%h%x09%cr%x09%s", "--", relpath], root
    )
    commits = []
    if rc != 0:
        return commits
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        full, short, reldate, subject = parts[0], parts[1], parts[2], parts[3]
        subject = subject.strip()
        if len(subject) > 48:
            subject = subject[:47] + "…"
        label = "{} · {} · {}".format(short, reldate, subject)
        commits.append({"ref": full, "label": label})
    return commits


def push_ref(root):
    """Resolve the 'last pushed' baseline: the remote-tracking ref the current branch
    pushes to. Returns (refname, short_sha, reldate), e.g. ("origin/main", "1a2b3c4",
    "3 hours ago"), or (None, None, None) when there is no push/upstream target (no
    remote, detached HEAD, or the branch was never pushed/fetched).

    `@{push}` is the push destination, which is the right notion for triangular
    workflows; git falls back to `@{upstream}` automatically when no separate push
    target is configured. We also try `@{upstream}` explicitly as a second chance.
    The remote-tracking ref this resolves to (e.g. origin/main) advances on both push
    and fetch, so it reflects what the remote currently holds — git keeps no record of
    push events themselves, so this ref is the standard proxy for "last pushed."
    """
    for spec in ("@{push}", "@{upstream}"):
        rc, name, _ = run_git(["rev-parse", "--abbrev-ref", spec], root)
        name = name.strip()
        if rc != 0 or not name:
            continue
        # confirm the remote-tracking ref actually exists locally (it may not if the
        # branch has an upstream configured but was never pushed/fetched)
        rc, out, _ = run_git(["log", "-1", "--format=%h%x09%cr", name], root)
        if rc != 0 or not out.strip():
            continue
        parts = out.strip().split("\t")
        short = parts[0]
        reldate = parts[1] if len(parts) > 1 else ""
        return name, short, reldate
    return None, None, None


def git_diff_text(root, baseline, relpath, abspath):
    """Run the appropriate word-diff command. Returns diff text ('' if no diff).

    Uses `--word-diff=porcelain`, the machine-readable variant: every token sits on
    its own line prefixed with `+`/`-`/` ` (space), and the one-character line `~`
    marks a newline. Unlike plain `--word-diff`, source text can never be mistaken
    for markup — a source line always carries a prefix, so literal `[-`/`{+`
    sequences in unchanged text parse correctly. `--no-color` guards against a
    user's color.ui=always config leaking ANSI codes into the output we parse.

    Baselines are always git versions:
      "head"   -> diff against HEAD (last local commit)
      "push"   -> diff against the remote-tracking ref the branch pushes to
                  (e.g. origin/main); shows everything changed since the last push
      "commit" -> diff against a specific commit ref
      "empty"  -> file is not committed yet; diff against git's empty tree so the
                  whole file reads as new. Implemented with `--no-index` against
                  os.devnull because an untracked path never appears in
                  `git diff <tree> -- path`.

    git diff returns exit code 1 when differences exist; 0 and 1 are both success,
    any other code is an error.
    """
    kind = baseline["kind"]
    if kind == "head":
        args = ["diff", "-U1000000", "--no-color", "--word-diff=porcelain", "HEAD", "--", relpath]
        rc, out, err = run_git(args, root)
    elif kind in ("commit", "push"):
        # both diff against a named ref (a commit sha, or a remote-tracking ref)
        args = ["diff", "-U1000000", "--no-color", "--word-diff=porcelain", baseline["ref"], "--", relpath]
        rc, out, err = run_git(args, root)
    elif kind == "empty":
        args = ["diff", "-U1000000", "--no-index", "--no-color", "--word-diff=porcelain", os.devnull, abspath]
        rc, out, err = run_git(args, root)
    else:
        raise ValueError("unknown baseline kind: %r" % kind)

    if rc not in (0, 1):
        raise RuntimeError("git diff failed (rc=%d): %s" % (rc, err.strip()))
    return out


def resolve_target(root, path):
    """Resolve a target path to (abspath, relpath) inside the repo, or raise ValueError.

    Relative paths resolve against the repo root (the file picker sends repo-relative
    paths); absolute paths are used as-is. Symlinks are resolved and the result must
    live inside the repo root (no escape).
    """
    if not path or not path.strip():
        raise ValueError("no path given")
    p = path.strip()
    cand = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    if not os.path.exists(cand):
        raise ValueError("file does not exist: %s" % path)
    if os.path.isdir(cand):
        raise ValueError("path is a directory, not a file: %s" % path)
    root_prefix = root.rstrip(os.sep) + os.sep
    if not (cand == root or cand.startswith(root_prefix)):
        raise ValueError("refusing a path outside the repository")
    return cand, os.path.relpath(cand, root)


def list_repo_files(root, limit=5000):
    """Repo files for the picker: tracked + non-ignored untracked, sorted, deduped."""
    files = []
    rc, out, _ = run_git(["ls-files"], root)
    if rc == 0:
        files.extend(out.splitlines())
    rc, out, _ = run_git(["ls-files", "--others", "--exclude-standard"], root)
    if rc == 0:
        files.extend(out.splitlines())
    seen = set()
    result = []
    for f in sorted(files):
        if not f or f in seen:
            continue
        seen.add(f)
        result.append(f)
        if len(result) >= limit:
            break
    return result


# --------------------------------------------------------------------------- #
# word-diff parser  (`--word-diff=porcelain`: one token per line, `~` = newline)
# --------------------------------------------------------------------------- #

def parse_word_diff(text):
    """Parse `git diff --word-diff=porcelain` output into typed segments + hunk ids.

    Segment shapes (unchanged from the original plain-word-diff parser; the
    reconstruction engine and the whole frontend consume this schema as-is):
      {"type": "context", "text": s}
      {"type": "add",     "text": s, "hunk": id}
      {"type": "del",     "text": s, "hunk": id}
      {"type": "newline", "text": "\\n", "side": "common"|"add"|"del", "hunk": id|None}

    Porcelain format: after the `@@` hunk header, every body line is one of
      ` text`  (space prefix)  an unchanged token
      `-text`                  a removed token
      `+text`                  an added token
      `~`                      end of a display line (a newline)
    Because every source token carries a prefix and the newline marker is exactly
    the one-character line `~`, source text can never be mistaken for markup —
    literal `[-`/`{+` sequences in unchanged prose parse correctly (the defining
    fix over plain `--word-diff`).

    A hunk is a maximal run of consecutive add/del tokens not interrupted by a
    context token or a `~` (= end of display line). Each add/del token is tagged
    with its hunk id.

    NEWLINE SIDING (composition rule, identical to the plain-format parser and
    empirically re-confirmed against porcelain output):
    a display line's trailing newline depends on the line's token composition:
      - the line has any context token                -> "common" (line in both)
      - purely added (+ tokens only)                  -> "add"  (emit on accept)
      - purely deleted (- tokens only)                -> "del"  (emit on reject)
      - full replacement (add+del, no context)        -> "common" (both end in \\n)
      - a bare `~` (blank line; ambiguous)            -> "common" (documented limit;
        porcelain emits identical bodies for a pure blank-line insert and delete,
        distinguishable only by the @@ header counts, so the ambiguity survives)
    A side-add/del newline carries the hunk id of the add/del run on its line so it
    follows that hunk's accept/reject decision during reconstruction.
    """
    segments = []
    lines = text.split("\n")

    # Find the body: everything after the first "@@" hunk header. If there is no
    # "@@" line at all (empty diff), there are no segments. Everything before it
    # (diff --git / index / --- / +++ headers) is skipped, so the `+++ b/...`
    # header can't be mistaken for an added token.
    start = None
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            start = i + 1
            break
    if start is None:
        return segments, []

    body = lines[start:]
    # git terminates the output with a trailing newline -> split() yields a final
    # "" artifact. Drop exactly that one. (Interior "" lines do not occur in
    # porcelain: even an empty token would carry its prefix character.)
    if body and body[-1] == "":
        body = body[:-1]

    next_hunk = 0
    cur_hunk = None                  # hunk id of the current add/del run
    has_context = has_add = has_del = False
    addel_hunk = None

    for raw in body:
        if raw == "~":
            # end of display line: emit the newline segment, sided by composition
            if has_context:
                side, hunk = "common", None
            elif has_add and not has_del:
                side, hunk = "add", addel_hunk
            elif has_del and not has_add:
                side, hunk = "del", addel_hunk
            else:
                # full-line replacement (add+del, no context) or blank line -> common
                side, hunk = "common", None
            segments.append({"type": "newline", "text": "\n", "side": side, "hunk": hunk})
            cur_hunk = None
            has_context = has_add = has_del = False
            addel_hunk = None
            continue
        if raw.startswith("@@"):            # extra hunk header (rare with -U1000000)
            continue
        if raw.startswith("\\"):            # "\ No newline at end of file"
            continue
        if not raw:                          # defensive: stray empty line
            continue

        prefix, tok = raw[0], raw[1:]
        if prefix == " ":
            segments.append({"type": "context", "text": tok})
            cur_hunk = None
            has_context = True
        elif prefix == "+":
            if cur_hunk is None:
                cur_hunk = next_hunk
                next_hunk += 1
            segments.append({"type": "add", "text": tok, "hunk": cur_hunk})
            has_add = True
            addel_hunk = cur_hunk
        elif prefix == "-":
            if cur_hunk is None:
                cur_hunk = next_hunk
                next_hunk += 1
            segments.append({"type": "del", "text": tok, "hunk": cur_hunk})
            has_del = True
            addel_hunk = cur_hunk
        # any other prefix is outside the porcelain vocabulary; ignore it

    return _assign_content_ids(segments)


def _assign_content_ids(segments):
    """Replace positional hunk ids with content-addressed string ids.

    id = short SHA-1 of (deleted text, added text, surrounding working-text
    context window, occurrence index for identical changes). Content addressing
    keeps a hunk's id stable when *other* parts of the document change, so the
    client's review decisions survive an agent save mid-review. (Positional ids
    forced the client to reset all decisions on every SSE payload — an agent
    save silently wiped every revert mark.)

    Returns (segments, hunks) with hunks in document order (the client relies
    on this order for next/prev navigation and the change map).
    """
    WINDOW = 32   # chars of surrounding working text folded into the hash

    # per-hunk token texts + first/last segment index, in document order
    order = []
    info = {}
    for idx, s in enumerate(segments):
        if s["type"] not in ("add", "del"):
            continue
        h = s["hunk"]
        if h not in info:
            info[h] = {"del": [], "add": [], "first": idx, "last": idx}
            order.append(h)
        info[h]["last"] = idx
        info[h][s["type"]].append(s["text"])

    def working_char(s):
        """The text a segment contributes to the working (accept) side."""
        t = s["type"]
        if t == "context":
            return s["text"]
        if t == "add":
            return s["text"]
        if t == "newline" and s["side"] in ("common", "add"):
            return "\n"
        return ""

    mapping = {}
    seen = {}
    for h in order:
        rec = info[h]
        dtext = "".join(rec["del"])
        atext = "".join(rec["add"])
        # context window: working-side text immediately before and after the hunk
        before = []
        need = WINDOW
        for i in range(rec["first"] - 1, -1, -1):
            if need <= 0:
                break
            if segments[i].get("hunk") == h:
                continue
            piece = working_char(segments[i])
            if piece:
                before.append(piece[-need:])
                need -= len(piece[-need:])
        before = "".join(reversed(before))
        after = []
        need = WINDOW
        for i in range(rec["last"] + 1, len(segments)):
            if need <= 0:
                break
            if segments[i].get("hunk") == h:
                continue
            piece = working_char(segments[i])
            if piece:
                after.append(piece[:need])
                need -= len(piece[:need])
        after = "".join(after)

        key = (dtext, atext, before, after)
        occ = seen.get(key, 0)
        seen[key] = occ + 1
        raw = "\x00".join((dtext, atext, before, after, str(occ)))
        mapping[h] = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    for s in segments:
        if s.get("hunk") is not None:
            s["hunk"] = mapping.get(s["hunk"], s["hunk"])

    return segments, [mapping[h] for h in order]


# --------------------------------------------------------------------------- #
# reconstruction  (see spec section 7)
# --------------------------------------------------------------------------- #

def _decide(hunk_id, decisions, pending_is_accept):
    d = decisions.get(str(hunk_id))
    if d in ("accept", "reject"):
        return d
    return "accept" if pending_is_accept else "reject"


def reconstruct(segments, decisions, pending_is_accept=True):
    """Walk segments and produce file contents for the given accept/reject decisions.

    decisions: {str(hunk_id): "accept"|"reject"}. Hunks not present are treated as
    pending; pending defaults to "accept" (keep what the agent did) unless
    pending_is_accept=False.

    Invariants (verified by the test harness):
      accept-all  reconstructs the working file
      reject-all  reconstructs the baseline file
    """
    out = []
    for s in segments:
        t = s["type"]
        if t == "context":
            out.append(s["text"])
        elif t == "newline":
            side = s["side"]
            if side == "common":
                out.append("\n")
            elif side == "add":
                if _decide(s["hunk"], decisions, pending_is_accept) == "accept":
                    out.append("\n")
            elif side == "del":
                if _decide(s["hunk"], decisions, pending_is_accept) == "reject":
                    out.append("\n")
        elif t == "add":
            if _decide(s["hunk"], decisions, pending_is_accept) == "accept":
                out.append(s["text"])
        elif t == "del":
            if _decide(s["hunk"], decisions, pending_is_accept) == "reject":
                out.append(s["text"])
    return "".join(out)


# --------------------------------------------------------------------------- #
# file io
# --------------------------------------------------------------------------- #

def read_text(path):
    """Read file as text, preserving newlines. Retries once on failure (mid-save)."""
    for attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                return f.read()
        except OSError:
            if attempt == 0:
                time.sleep(0.05)
                continue
            raise
    return ""


def write_file(abspath, new_text):
    """Write new_text to the file verbatim. No backup is kept; recovery of
    overwritten content is via git. Writes are always explicit (a button) and
    never auto-commit."""
    with open(abspath, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- #
# shared state
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, root, relpath=None, abspath=None):
        self.root = root
        self.relpath = relpath           # None until a file is opened
        self.abspath = abspath           # None until a file is opened
        self.lock = threading.RLock()
        # baseline: {"kind": "head"|"commit"|"empty", "ref": ..., "label": ...}
        self.baseline = {"kind": "head", "ref": "HEAD", "label": "HEAD"}
        self.untracked = False           # file not committed; forces the empty-tree baseline
        self.last_mtime = file_mtime(abspath) if abspath else 0.0
        self.client_dirty = False
        # SSE subscribers
        self.subscribers = []            # list of queue.Queue

    def has_file(self):
        return self.abspath is not None

    # -- subscriber management --
    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event):
        data = json.dumps(event)
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    # -- open / switch the watched file --
    def open_file(self, path):
        """Switch to watching `path` (resolved inside the repo). Raises ValueError on a
        bad path. The baseline is always a git version: HEAD when the file is
        committed, otherwise the empty tree (whole file reads as new). Resets the
        baseline and mtime."""
        abspath, relpath = resolve_target(self.root, path)   # may raise ValueError
        tracked = is_tracked(self.root, relpath)
        short, reldate = head_label(self.root)
        no_head = (not tracked) or (short is None)
        with self.lock:
            self.relpath = relpath
            self.abspath = abspath
            self.untracked = no_head
            self.last_mtime = file_mtime(abspath)
            if no_head:
                self.baseline = {"kind": "empty", "ref": "empty",
                                 "label": "empty tree · file not yet committed"}
            else:
                # Default to "last push" (the remote-tracking ref) so the diff shows
                # everything not yet pushed. Fall back to HEAD when the branch has no
                # push/upstream target (no remote, detached HEAD, never pushed).
                pname, pshort, preldate = push_ref(self.root)
                if pname:
                    self.baseline = {"kind": "push", "ref": pname,
                                     "label": "last push · {} · {}".format(pname, preldate or "")}
                else:
                    self.baseline = {"kind": "head", "ref": "HEAD",
                                     "label": "HEAD · {}".format(reldate or "")}
        return relpath


# --------------------------------------------------------------------------- #
# diff engine
# --------------------------------------------------------------------------- #

def compute_diff_epoch(root, baseline, raw_text):
    """Fingerprint of the exact inputs a rendered diff was computed from: the
    working file content plus the baseline (kind, ref, and the commit the ref
    currently resolves to — "HEAD" moves after a commit). The client echoes
    this on /api/apply so the server can refuse to apply decisions that were
    made against a diff that no longer reflects reality (e.g. the agent wrote
    to disk during the SSE debounce window)."""
    if baseline["kind"] == "empty":
        resolved = "empty"
    else:
        rc, out, _ = run_git(["rev-parse", baseline["ref"]], root)
        resolved = out.strip() if rc == 0 else baseline["ref"]
    raw = "\x00".join((raw_text, baseline["kind"], baseline.get("ref", ""), resolved))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def compute_payload(state, event_type):
    """Read the file, run git diff, parse, and build an SSE payload dict."""
    with state.lock:
        baseline = dict(state.baseline)
        relpath = state.relpath
        abspath = state.abspath
    if abspath is None:
        return {
            "type": event_type,
            "file": None,
            "raw_text": "",
            "baseline": None,
            "segments": [],
            "hunks": [],
            "file_mtime": 0.0,
            "diff_epoch": None,
        }
    raw_text = read_text(abspath)
    diff_text = git_diff_text(state.root, baseline, relpath, abspath)
    segments, hunks = parse_word_diff(diff_text)
    pub_baseline = {k: baseline[k] for k in ("kind", "ref", "label") if k in baseline}
    return {
        "type": event_type,
        "file": relpath,
        "raw_text": raw_text,
        "baseline": pub_baseline,
        "segments": segments,
        "hunks": hunks,
        "file_mtime": file_mtime(abspath),
        "diff_epoch": compute_diff_epoch(state.root, baseline, raw_text),
    }


def broadcast_diff(state, event_type):
    payload = compute_payload(state, event_type)
    state.broadcast(payload)
    return payload


# --------------------------------------------------------------------------- #
# watch loop (mtime poll + debounce)
# --------------------------------------------------------------------------- #

def watch_loop(state, stop_event):
    POLL = 0.30
    DEBOUNCE = 0.18
    while not stop_event.is_set():
        time.sleep(POLL)
        with state.lock:
            abspath = state.abspath
            last = state.last_mtime
        if abspath is None:
            continue
        try:
            mt = file_mtime(abspath)
        except Exception:
            continue
        if mt == last:
            continue
        # change detected; debounce so a burst of saves coalesces
        time.sleep(DEBOUNCE)
        with state.lock:
            if state.abspath != abspath:
                continue                 # file was switched mid-tick; let next tick handle it
            mt2 = file_mtime(abspath)
            state.last_mtime = mt2
            dirty = state.client_dirty
        # With a dirty buffer, notify rather than overwrite (section 8).
        event_type = "disk_changed" if dirty else "update"
        try:
            broadcast_diff(state, event_type)
        except Exception as e:
            sys.stderr.write("draftwatch: diff error: %s\n" % e)


# --------------------------------------------------------------------------- #
# vendored static assets (served from the package; no CDN, works offline)
# --------------------------------------------------------------------------- #

# Fixed allowlist: name -> MIME type. The URL path is never interpreted as a
# filesystem path; anything not literally in this dict is a 404.
STATIC_ASSETS = {
    "codemirror.js": "application/javascript; charset=utf-8",
    "marked.js": "application/javascript; charset=utf-8",
    "purify.js": "application/javascript; charset=utf-8",
    "turndown.js": "application/javascript; charset=utf-8",
}


def load_asset(name):
    """Read a vendored asset from the installed package (or the source tree)."""
    try:
        from importlib import resources
        return (resources.files("draftwatch") / "assets" / name).read_bytes()
    except Exception:
        pass
    try:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
        with open(here, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    state = None            # set on the class before serving
    token = None            # per-session secret; required on /api/* and /events
    allowed_hosts = set()   # exact Host header values we will serve

    # quiet default logging
    def log_message(self, fmt, *args):
        pass

    # ---- access control ----
    # Any local process, or any web page you happen to have open (drive-by CSRF;
    # DNS rebinding defeats the browser's same-origin protection), could
    # otherwise POST to /api/save and overwrite the file. Three checks:
    #   1. Host must be exactly our loopback host:port (kills DNS rebinding).
    #   2. Origin, when the browser sends one, must match (kills cross-site XHR).
    #   3. /api/* and /events require the per-session token, sent as the
    #      X-Draftwatch-Token header or the `t` query parameter (EventSource
    #      cannot set headers). The token is minted in main() and only ever
    #      printed to the local terminal / embedded in the opened URL.
    def _query(self):
        parts = self.path.split("?", 1)
        return parse_qs(parts[1]) if len(parts) == 2 else {}

    def _guard(self, path):
        """Return True if the request may proceed; otherwise send 403."""
        host = (self.headers.get("Host") or "").strip()
        if host not in self.allowed_hosts:
            self._send(403, json.dumps({"error": "forbidden: bad Host"}))
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            if origin not in {"http://" + h for h in self.allowed_hosts}:
                self._send(403, json.dumps({"error": "forbidden: bad Origin"}))
                return False
        if path.startswith("/api/") or path == "/events":
            tok = self.headers.get("X-Draftwatch-Token")
            if not tok:
                vals = self._query().get("t")
                tok = vals[0] if vals else ""
            if not (self.token and secrets.compare_digest(tok, self.token)):
                self._send(403, json.dumps({"error": "forbidden: missing or bad session token"}))
                return False
        return True

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._guard(path):
            return
        if path == "/":
            # inject version/date (literal tokens, so no clash with CSS/JS braces)
            page = (INDEX_HTML
                    .replace("{{VERSION}}", __version__)
                    .replace("{{RELEASE_DATE}}", RELEASE_DATE))
            self._send(200, page, "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/events":
            self._serve_events()
        elif path == "/api/baselines":
            self._api_baselines()
        elif path == "/api/files":
            self._api_files()
        else:
            self._send(404, "not found", "text/plain")

    def _serve_static(self, name):
        """Vendored library code only — token-exempt (no state, no secrets).
        `name` must literally match the allowlist; it is never interpreted as
        a filesystem path, so traversal is structurally impossible."""
        ctype = STATIC_ASSETS.get(name)
        if ctype is None:
            return self._send(404, "not found", "text/plain")
        data = load_asset(name)
        if data is None:
            return self._send(404, "not found", "text/plain")
        self._send(200, data, ctype)

    def _serve_events(self):
        st = self.state
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = st.subscribe()
        # send current state immediately so the page populates
        try:
            payload = compute_payload(st, "update")
            self._sse_write(json.dumps(payload))
        except Exception as e:
            sys.stderr.write("draftwatch: initial diff error: %s\n" % e)
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    self._sse_write(data)
                except queue.Empty:
                    # heartbeat to keep the connection alive
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            st.unsubscribe(q)

    def _sse_write(self, data):
        self.wfile.write(b"data: ")
        self.wfile.write(data.encode("utf-8"))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def _api_baselines(self):
        st = self.state
        with st.lock:
            baseline = {k: st.baseline[k] for k in ("kind", "ref", "label")
                        if k in st.baseline}
            untracked = st.untracked
            relpath = st.relpath
        if relpath is None:
            return self._json({"current": None, "head": None, "push": None,
                               "commits": [], "untracked": False})
        short, reldate = head_label(st.root)
        head = None
        if short is not None:
            head = {"ref": "HEAD", "short": short,
                    "label": "HEAD · {}".format(reldate)}
        pname, pshort, preldate = push_ref(st.root)
        push = None
        if pname is not None:
            push = {"ref": pname, "short": pshort,
                    "label": "last push · {} · {}".format(pname, preldate)}
        commits = commit_list(st.root, relpath)
        self._json({
            "current": baseline,
            "head": head,
            "push": push,
            "commits": commits,
            "untracked": untracked,
        })

    def _api_files(self):
        st = self.state
        with st.lock:
            current = st.relpath
        self._json({"files": list_repo_files(st.root), "current": current})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._guard(path):
            return
        if path == "/api/baseline":
            self._api_set_baseline()
        elif path == "/api/apply":
            self._api_apply()
        elif path == "/api/save":
            self._api_save()
        elif path == "/api/commit":
            self._api_commit()
        elif path == "/api/client_state":
            self._api_client_state()
        elif path == "/api/open":
            self._api_open()
        else:
            self._send(404, "not found", "text/plain")

    def _api_open(self):
        st = self.state
        body = self._read_json()
        path = body.get("path", "")
        try:
            relpath = st.open_file(path)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        with st.lock:
            st.client_dirty = False          # the old buffer belonged to the old file
        payload = broadcast_diff(st, "update")
        self._json({"ok": True, "file": relpath, "baseline": payload["baseline"]})

    def _api_set_baseline(self):
        st = self.state
        if not st.has_file():
            return self._json({"error": "no file open"}, 400)
        body = self._read_json()
        kind = body.get("kind")
        try:
            if kind == "head":
                if st.untracked:
                    return self._json({"error": "file is not committed; HEAD baseline unavailable"}, 400)
                with st.lock:
                    short, reldate = head_label(st.root)
                    st.baseline = {"kind": "head", "ref": "HEAD",
                                   "label": "HEAD · {}".format(reldate or "")}
            elif kind == "push":
                if st.untracked:
                    return self._json({"error": "file is not committed; push baseline unavailable"}, 400)
                # re-resolve the push target server-side rather than trust the client
                pname, pshort, preldate = push_ref(st.root)
                if not pname:
                    return self._json({"error": "no push/upstream target configured for this branch"}, 400)
                with st.lock:
                    st.baseline = {"kind": "push", "ref": pname,
                                   "label": "last push · {} · {}".format(pname, preldate or "")}
            elif kind == "commit":
                ref = body.get("ref")
                if not ref:
                    return self._json({"error": "missing ref"}, 400)
                label = body.get("label", ref)
                with st.lock:
                    st.baseline = {"kind": "commit", "ref": ref, "label": label}
            elif kind == "empty":
                if not st.untracked:
                    return self._json({"error": "file is committed; use HEAD or a commit baseline"}, 400)
                with st.lock:
                    st.baseline = {"kind": "empty", "ref": "empty",
                                   "label": "empty tree · file not yet committed"}
            else:
                return self._json({"error": "unknown baseline kind"}, 400)
            payload = broadcast_diff(st, "baseline_changed")
            self._json({"ok": True, "baseline": payload["baseline"]})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _api_apply(self):
        st = self.state
        if not st.has_file():
            return self._json({"error": "no file open"}, 400)
        body = self._read_json()
        decisions = body.get("decisions", {}) or {}
        # normalise keys to strings
        decisions = {str(k): v for k, v in decisions.items()}
        client_epoch = body.get("diff_epoch")
        try:
            with st.lock:
                baseline = dict(st.baseline)
            # staleness guard: the decisions were made against a rendered diff;
            # refuse to apply them if the file or baseline has changed since that
            # render (e.g. an agent save during the SSE debounce window would
            # otherwise revert the wrong spans). The client echoes the epoch it
            # rendered; we recompute from current reality and compare.
            raw_now = read_text(st.abspath)
            epoch_now = compute_diff_epoch(st.root, baseline, raw_now)
            if client_epoch != epoch_now:
                broadcast_diff(st, "update")   # re-render clients (decisions survive)
                return self._json({
                    "error": "stale diff: the file or baseline changed since this "
                             "view was rendered; the view has been refreshed — "
                             "check your marks and apply again",
                    "stale": True,
                    "diff_epoch": epoch_now,
                }, 409)
            diff_text = git_diff_text(st.root, baseline, st.relpath, st.abspath)
            segments, _ = parse_word_diff(diff_text)
            if not segments:
                # Empty diff: nothing to apply. Never write (would truncate the file).
                return self._json({"ok": True, "no_changes": True})
            new_text = reconstruct(segments, decisions, pending_is_accept=True)
            write_file(st.abspath, new_text)
            with st.lock:
                st.last_mtime = file_mtime(st.abspath)
                st.client_dirty = False
            broadcast_diff(st, "update")
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _api_save(self):
        st = self.state
        if not st.has_file():
            return self._json({"error": "no file open"}, 400)
        body = self._read_json()
        if "text" not in body:
            return self._json({"error": "missing text"}, 400)
        text = body["text"]
        try:
            write_file(st.abspath, text)
            with st.lock:
                st.last_mtime = file_mtime(st.abspath)
                st.client_dirty = False
            broadcast_diff(st, "update")
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _api_commit(self):
        """Commit the current state of the watched file. This is the final step
        of the review loop — on success the baseline auto-advances to HEAD, so
        the diff clears to zero (review -> revert -> commit -> clean). Earlier
        commits and the push baseline remain available in the dropdown."""
        st = self.state
        if not st.has_file():
            return self._json({"error": "no file open"}, 400)
        body = self._read_json()
        msg = (body.get("message") or "").strip() or "Update {}".format(st.relpath)
        try:
            rc, _, err = run_git(["add", "--", st.relpath], st.root)
            if rc != 0:
                return self._json({"error": "git add failed: " + err.strip()}, 500)
            rc, out, err = run_git(["commit", "-m", msg, "--", st.relpath], st.root)
            if rc != 0:
                combined = (err.strip() or out.strip())
                if "nothing to commit" in combined or "nothing added to commit" in combined \
                        or "no changes added to commit" in combined:
                    return self._json({"error": "nothing to commit — no changes since the last commit"}, 400)
                return self._json({"error": "git commit failed: " + combined}, 500)
            # the file is now committed; advance the baseline to the new HEAD so
            # the review loop closes with an empty diff.
            short, reldate = head_label(st.root)
            with st.lock:
                st.untracked = False
                st.baseline = {"kind": "head", "ref": "HEAD",
                               "label": "HEAD · {}".format(reldate or "")}
            broadcast_diff(st, "baseline_changed")
            self._json({"ok": True, "short": short})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _api_client_state(self):
        st = self.state
        body = self._read_json()
        with st.lock:
            if "dirty" in body:
                st.client_dirty = bool(body["dirty"])
        self._json({"ok": True})


# --------------------------------------------------------------------------- #
# frontend (embedded; no CDN, no framework, no build)
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Draftwatch</title>
<script src="/static/codemirror.js"></script>
<script src="/static/marked.js"></script>
<script src="/static/purify.js"></script>
<script src="/static/turndown.js"></script>
<script>
  // Resolve the saved theme before first paint so there is no light/dark flash.
  // "auto" (or nothing saved) leaves it to prefers-color-scheme via CSS.
  try {
    var _t = localStorage.getItem("draftwatch-theme");
    if (_t === "light" || _t === "dark") {
      document.documentElement.setAttribute("data-theme", _t);
    }
  } catch (e) {}
</script>
<style>
  /* Draftwatch identity: a cool, light-blue editorial palette — blue-tinted
     paper and slate ink, with a single steel-blue accent (no SaaS indigo). The
     accent is kept deep enough to hold white button text (>=4.5:1); the green/red
     add/del pair stays strictly semantic, and the disk-changed warning stays
     amber so it pops against the cool UI. Theme resolves in this order: manual
     choice (data-theme on <html>) wins; otherwise follow the OS
     (prefers-color-scheme); light is the base. */
  :root {
    --bg: #f3f7fc;
    --panel: #e7eef8;
    --border: #d0dcec;
    --text: #1e2733;
    --muted: #5f6b7a;
    --add-bg: #d7ecd6;
    --add-fg: #1f5a2c;
    --del-bg: #f6d8d3;
    --del-fg: #8a2f22;
    --accent: #2f6fb0;       /* steel blue: buttons, links, focus */
    --accent-fg: #f5f9fe;    /* text on a filled --accent button */
    --warn-bg: #fdf0cf;
    --warn-border: #d8b24e;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }
  /* dark values, declared once and reused by both the auto (OS) path and the
     manual override below */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme]) {
      --bg: #121824;
      --panel: #1a2231;
      --border: #2e3a4c;
      --text: #e6ecf4;
      --muted: #94a1b4;
      --add-bg: #1f3f26;
      --add-fg: #b7e6b3;
      --del-bg: #4f2620;
      --del-fg: #f0c2b8;
      --accent: #6aa9e0;     /* lighter sky blue reads well on the dark ground */
      --accent-fg: #0e1826;  /* dark text on the lighter accent button */
      --warn-bg: #3c3620;
      --warn-border: #8a7320;
    }
  }
  :root[data-theme="dark"] {
    --bg: #121824;
    --panel: #1a2231;
    --border: #2e3a4c;
    --text: #e6ecf4;
    --muted: #94a1b4;
    --add-bg: #1f3f26;
    --add-fg: #b7e6b3;
    --del-bg: #4f2620;
    --del-fg: #f0c2b8;
    --accent: #6aa9e0;
    --accent-fg: #0e1826;
    --warn-bg: #3c3620;
    --warn-border: #8a7320;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; overscroll-behavior: none; }

  /* app chrome (native window via pywebview, ?app=1): the OS title bar
     already says "draftwatch · file", so the in-page wordmark is redundant —
     drop it, tighten the toolbar, and make the chrome feel native (no text
     selection on controls, no web-page tells). */
  body.app-mode header .title { display: none; }
  body.app-mode header { padding: 5px 10px; }
  body.app-mode header,
  body.app-mode footer {
    -webkit-user-select: none;
    user-select: none;
  }
  body.app-mode footer input[type="text"] {
    -webkit-user-select: auto;
    user-select: auto;         /* the commit message field stays typable/selectable */
  }
  body {
    font-family: var(--mono);
    background: var(--bg);
    color: var(--text);
    font-size: 13px;
    display: flex;
    flex-direction: column;
  }
  header {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  /* Two fixed rows that never wrap into each other, so the layout stays put
     regardless of title length or window width:
       row 1 = file context + mode (title, file picker, review/edit)
       row 2 = state + diff target (status line, baseline selector)
     The variable-length status line lives on its own row and ellipsizes
     instead of pushing the controls around. */
  header .bar {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  header .title {
    font-weight: 700;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    letter-spacing: .02em;
  }
  /* the +/− mark: the product in two characters (keep the add, strike the del) */
  .mark {
    display: inline-flex;
    align-items: center;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 0 5px;
    font-weight: 700;
    line-height: 1.45;
  }
  .mark .ma { color: var(--add-fg); }
  .mark .md { color: var(--del-fg); text-decoration: line-through; }
  header .status { color: var(--muted); }
  header #status {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  header .spacer { flex: 1; }
  #edit-toolbar .fmt { display: inline-flex; gap: 4px; }
  #edit-toolbar .fmt button { min-width: 30px; }
  select, button {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 4px 8px;
    cursor: pointer;
  }
  button:hover, select:hover { border-color: var(--accent); }

  #banner {
    display: none;
    align-items: center;
    gap: 10px;
    padding: 7px 12px;
    background: var(--warn-bg);
    border-bottom: 1px solid var(--warn-border);
  }
  #banner.show { display: flex; }
  main {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr 1fr;
    min-height: 0;
  }
  .panel { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
  .panel.left { border-right: 1px solid var(--border); }
  .panel h2 {
    margin: 0;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  .scroll { flex: 1; overflow: auto; padding: 12px; }
  pre {
    margin: 0;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
    background: transparent;
  }
  /* CodeMirror host: the editor owns scrolling inside the left panel */
  #editor-host { flex: 1; min-height: 0; overflow: hidden; }
  #editor-host .cm-editor { height: 100%; background: transparent; }
  #editor-host .cm-editor.cm-focused { outline: none; }
  #editor-host .cm-scroller {
    overflow: auto;
    padding: 12px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
  }
  #editor-host .cm-content { caret-color: var(--text); }
  #editor-host .cm-cursor { border-left-color: var(--text); }
  #editor-host .cm-selectionBackground,
  #editor-host .cm-editor.cm-focused .cm-selectionBackground {
    background: rgba(127, 127, 127, .28);
  }
  #editor-host .cm-placeholder { color: var(--muted); font-style: italic; }
  /* CM search panel, themed with the app variables (light/dark for free) */
  .cm-panels {
    background: var(--panel) !important;
    color: var(--text) !important;
    border-color: var(--border) !important;
  }
  .cm-panels input, .cm-panels button, .cm-panels label {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
  }
  .cm-panels input {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
  }
  .cm-panels button {
    background: var(--bg);
    border: 1px solid var(--border) !important;
    border-radius: 4px;
    cursor: pointer;
  }
  .cm-searchMatch { background: var(--warn-bg); outline: 1px solid var(--warn-border); }
  .cm-searchMatch-selected { background: var(--warn-border); }

  /* rendered markdown preview: reading typography, sanitized content only */
  #preview {
    font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
    font-size: 16px;
    line-height: 1.65;
    padding: 18px 24px;
  }
  #preview > * { max-width: 42em; }
  #preview h1, #preview h2, #preview h3, #preview h4 {
    line-height: 1.25;
    margin: 1.2em 0 .5em;
  }
  #preview h1:first-child, #preview h2:first-child { margin-top: 0; }
  #preview p { margin: .8em 0; }
  #preview a { color: var(--accent); }
  #preview code {
    font-family: var(--mono);
    font-size: .85em;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0 4px;
  }
  #preview pre {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 10px 12px;
    overflow: auto;
  }
  #preview pre code { border: none; background: none; padding: 0; }
  #preview blockquote {
    margin: .8em 0;
    padding-left: 14px;
    border-left: 3px solid var(--border);
    color: var(--muted);
  }
  #preview img { max-width: 100%; }
  #preview hr { border: none; border-top: 1px solid var(--border); }
  #preview table { border-collapse: collapse; }
  #preview th, #preview td { border: 1px solid var(--border); padding: 4px 9px; }
  #diff { white-space: pre-wrap; word-break: break-word; line-height: 1.6; }
  #diff .add { background: var(--add-bg); color: var(--add-fg); border-radius: 2px; }
  #diff .del { background: var(--del-bg); color: var(--del-fg); text-decoration: line-through; border-radius: 2px; }
  .hunk { border-radius: 3px; padding: 0 1px; }
  /* keep (default): show the agent's edit normally (del struck, add green).
     revert: the add won't be written (dim + strike), the baseline del is what stays. */
  .hunk-revert .add { opacity: .3; }
  .hunk-revert .del { text-decoration: none; opacity: 1; }
  .ctrl {
    display: inline-flex;
    gap: 1px;
    margin: 0 2px;
    vertical-align: baseline;
    white-space: nowrap;
  }
  .ctrl button {
    padding: 0 5px;
    font-size: 11px;
    line-height: 1.5;
    border-radius: 3px;
  }
  .ctrl button.rev { color: var(--del-fg); }
  .ctrl button.rev.on { background: var(--del-bg); color: var(--del-fg); border-color: var(--del-fg); font-weight: 700; }
  .empty { color: var(--muted); font-style: italic; }
  footer {
    border-top: 1px solid var(--border);
    background: var(--panel);
    padding: 7px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  footer .counts { color: var(--muted); }
  footer .spacer { flex: 1; }
  .apply { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); font-weight: 700; }
  .apply:hover { filter: brightness(1.06); }
  footer input[type="text"] {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 4px 8px;
    max-width: 210px;
    min-width: 60px;
    flex: 0 1 auto;
  }
  footer input[type="text"]:focus { outline: none; border-color: var(--accent); }
  #commit-btn { color: var(--add-fg); border-color: var(--add-fg); font-weight: 700; }
  #commit-btn.armed { background: var(--add-bg); }
  .hidden { display: none !important; }
  h2.unsaved { color: var(--del-fg); }
  button.attn { box-shadow: 0 0 0 2px var(--warn-border); }
  .filepick { display: inline-flex; align-items: center; gap: 5px; flex: 1 1 auto; min-width: 0; }
  .filepick #file-select { flex: 1 1 auto; min-width: 0; width: 100%; }
  #empty-msg { color: var(--muted); font-style: italic; padding: 16px 4px; }

  /* right panel: scroll area + change-map gutter side by side */
  .right-body { flex: 1; display: flex; min-height: 0; min-width: 0; }
  .right-body #right-scroll { flex: 1; position: relative; }  /* positioned: tick offsetTop reference */
  #diff .ln { display: block; padding-left: 4px; border-left: 3px solid transparent; }
  #diff .ln.changed { border-left-color: var(--border); }
  #diff .ln.current { background: rgba(127,127,127,.14); border-left-color: var(--accent); }
  .collapser {
    display: block; width: 100%; text-align: left;
    color: var(--muted); font-family: var(--mono); font-size: 12px; font-style: italic;
    background: none; border: none; border-top: 1px dashed var(--border);
    border-bottom: 1px dashed var(--border); padding: 2px 4px; margin: 2px 0; cursor: pointer;
  }
  .collapser:hover { color: var(--accent); border-color: var(--accent); }
  /* change-map gutter (minimap) */
  #minimap {
    flex: 0 0 14px; position: relative; background: var(--panel);
    border-left: 1px solid var(--border); cursor: pointer;
  }
  #minimap .tick { position: absolute; left: 2px; right: 2px; min-height: 3px; border-radius: 2px; }
  #minimap .tick.add { background: var(--add-fg); }
  #minimap .tick.del { background: var(--del-fg); }
  #minimap .tick.mix { background: var(--accent); }
  #minimap .view {
    position: absolute; left: 0; right: 0; background: rgba(127,127,127,.20);
    border: 1px solid var(--border); border-radius: 2px; pointer-events: none;
  }
  .nav { display: inline-flex; align-items: center; gap: 4px; }
  .nav button { padding: 0 7px; }
  .chgonly { color: var(--muted); display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
  /* untracked-file onboarding */
  .onboard { padding: 20px 8px; max-width: 46em; line-height: 1.7; }
  .onboard .muted { color: var(--muted); }
  .onboard button.linkish {
    background: none; border: none; padding: 0;
    color: var(--accent); text-decoration: underline; cursor: pointer;
    font-family: var(--mono); font-size: 12px;
  }

  /* editable preview: the rendered reading view doubles as a writing surface.
     Same reading typography as the read-only preview; a soft focus ring makes
     it obvious the text is editable. */
  #preview[contenteditable="true"] { outline: none; }
  #preview[contenteditable="true"]:focus { box-shadow: inset 0 0 0 2px var(--border); }
  #preview[contenteditable="true"] a { cursor: text; }

  /* About: a modal over a dimmed backdrop */
  #about {
    position: fixed; inset: 0; z-index: 50;
    display: none; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, .45);
    padding: 24px;
  }
  #about.show { display: flex; }
  #about .card {
    background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 10px;
    max-width: 40em; width: 100%; max-height: 82vh; overflow: auto;
    padding: 22px 26px; line-height: 1.6;
    box-shadow: 0 18px 50px rgba(0, 0, 0, .35);
  }
  #about h2 {
    margin: 0 0 4px; font-size: 18px; letter-spacing: .01em;
    display: inline-flex; align-items: center; gap: 8px;
    border: none; padding: 0; background: none; text-transform: none; color: var(--text);
  }
  #about .tag { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
  #about p { margin: .7em 0; font-family: var(--mono); font-size: 12.5px; }
  #about h3 {
    margin: 1.2em 0 .3em; font-size: 11px; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted);
  }
  #about code {
    font-family: var(--mono); font-size: .9em;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 3px; padding: 0 4px;
  }
  #about .about-close { position: sticky; top: 0; float: right; margin: -6px -8px 0 0; }
  #about a { color: var(--accent); }
  #about .about-meta {
    margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border);
    color: var(--muted); font-size: 11.5px; line-height: 1.7;
  }
</style>
</head>
<body>
<header>
  <div class="bar">
    <span class="title"><span class="mark"><span class="ma">+</span><span class="md">−</span></span>Draftwatch</span>
    <span class="filepick">
      <label class="status" for="file-select">file</label>
      <select id="file-select" title="pick a writing file in this repo"></select>
    </span>
    <button id="theme-toggle" title="theme: auto / light / dark">theme: auto</button>
    <button id="about-btn" title="what is Draftwatch?">about</button>
  </div>
  <div class="bar">
    <label class="status" for="baseline">baseline</label>
    <select id="baseline"></select>
    <span class="status" id="status">connecting&hellip;</span>
  </div>
  <div class="bar" id="edit-toolbar">
    <button id="view-toggle" title="switch the left panel between markdown source and a rendered reading view">preview</button>
    <span class="status" id="fmt-label">format</span>
    <span class="fmt">
      <button id="fmt-bold" title="bold (⌘/Ctrl-B)"><b>B</b></button>
      <button id="fmt-italic" title="italic (⌘/Ctrl-I)"><i>I</i></button>
      <button id="fmt-link" title="link (⌘/Ctrl-K)">link</button>
    </span>
    <button id="save" class="apply">save to file</button>
  </div>
</header>

<div id="banner">
  <span id="banner-text"></span>
  <span class="spacer"></span>
  <button id="banner-overwrite">keep my edits</button>
  <button id="banner-reload">reload</button>
</div>

<div id="about" role="dialog" aria-modal="true" aria-label="About Draftwatch">
  <div class="card">
    <button class="about-close" id="about-close" title="close (Esc)">close ✕</button>
    <h2><span class="mark"><span class="ma">+</span><span class="md">−</span></span>Draftwatch</h2>
    <p class="tag">Review an AI agent's edits as a real git diff — locally.</p>
    <p>An autonomous agent (Claude Code and the like) edits your files on disk.
      Draftwatch shows you exactly what changed, as the word-level diff
      <strong>git itself produces on your machine</strong> — not an AI's summary,
      not a JavaScript guess. You keep or revert each change, then commit.</p>
    <h3>The review loop</h3>
    <p>Read the diff on the right. Toggle <code>revert ✗</code> on any change you
      don't want. <strong>Apply</strong> writes the result to disk — keeping the
      rest, reverting the marked spans. <strong>Commit</strong> records the file
      to git; the baseline advances to that commit, the diff clears to zero, and
      the next agent pass starts clean.</p>
    <h3>Two panels</h3>
    <p>Left is your working text: edit the markdown source directly, or switch to
      the rendered <strong>Preview</strong>, which is also editable. Right is the
      diff against the baseline — added words in green, removed words struck in
      red — with a change map, next/previous navigation, and a changes-only view.</p>
    <h3>Editing in Preview</h3>
    <p>Typing in the rendered Preview rewrites the markdown source in a normalized
      style, so heading, emphasis, and spacing conventions can shift. When you
      need byte-exact control of the source, edit in the <strong>Source</strong>
      view instead.</p>
    <h3>Local and private</h3>
    <p>Draftwatch runs a small server bound to <code>127.0.0.1</code>, guarded by a
      per-session token. It never talks to an LLM and never sends your file
      anywhere.</p>
    <p class="about-meta">
      By <strong>Mike Konczal</strong> · vibe-coded with Fable 5<br>
      Draftwatch v{{VERSION}} · {{RELEASE_DATE}}
    </p>
  </div>
</div>

<main>
  <section class="panel left">
    <h2 id="left-title">working text</h2>
    <div id="editor-host"></div>
    <div id="preview" class="scroll hidden"></div>
  </section>
  <section class="panel right">
    <h2>diff vs baseline</h2>
    <div class="right-body">
      <div class="scroll" id="right-scroll">
        <div id="diff"></div>
      </div>
      <div id="minimap" title="change map — click to jump"></div>
    </div>
    <footer>
      <span class="counts" id="counts">0 hunks</span>
      <span class="nav hidden" id="nav">
        <button id="prev-change" title="previous change (p)">▲</button>
        <button id="next-change" title="next change (n)">▼</button>
        <span class="counts" id="nav-pos"></span>
      </span>
      <label class="chgonly"><input type="checkbox" id="changes-only"> changes only</label>
      <span class="spacer"></span>
      <button id="revert-all" title="mark every change to revert">revert all</button>
      <button id="keep-all" title="clear all revert marks (keep every change)">keep all</button>
      <button id="apply" class="apply">apply</button>
      <input type="text" id="commit-msg" class="hidden" placeholder="commit message (optional) — Enter to commit, Esc to cancel">
      <button id="commit-btn" title="git commit this version — the diff clears and future edits show against it">commit</button>
    </footer>
  </section>
</main>

<script>
"use strict";
(function () {
  // ---- state ----
  var dirty = false;            // unsaved edits in edit mode
  var segments = [];
  var hunks = [];
  var decisions = {};           // {hunkId: "reject"} for hunks marked to revert; absent = keep
  var rawText = "";
  var baseline = null;
  var diffEpoch = null;         // fingerprint of the rendered diff (echoed on apply)
  var currentFile = null;       // relpath of the open file, or null when none
  var WRITING_EXT = [".md", ".markdown", ".qmd", ".txt", ".rst"];
  var changesOnly = false;      // "just changes" collapsed view
  var showEmptyTreeDiff = false; // untracked file: user opted into the full "all added" diff
  var hunkOrder = [];           // hunk ids in document order (next/prev + minimap)
  var hunkKind = {};            // hunkId -> "add" | "del" | "mix"
  var hunkLine = {};            // hunkId -> 0-based line index in the working text (rawText)
  var totalLines = 1;           // line count of rawText; denominator for proportional positions
  var currentIdx = -1;          // index into hunkOrder for next/prev
  var CONTEXT_LINES = 3;        // lines kept around each change in "changes only"

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---- session token (from the URL draftwatch printed/opened) ----
  var TOKEN = (function () {
    var m = /(?:\?|&)t=([^&]+)/.exec(window.location.search);
    return m ? decodeURIComponent(m[1]) : "";
  })();

  // ---- launch surface (native window vs browser) ----
  // app=1: running inside the pywebview window -> app chrome styling.
  // appfallback=1: the native window was requested but unavailable -> say so
  // in the UI instead of leaving the silent switch to the browser unexplained.
  var APP_MODE = /(?:\?|&)app=1(?:&|$)/.test(window.location.search);
  var APP_FALLBACK = /(?:\?|&)appfallback=1(?:&|$)/.test(window.location.search);
  if (APP_MODE) document.body.classList.add("app-mode");

  // ---- server notification of client state (turn-based contract) ----
  function postJSON(url, obj, cb) {
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Draftwatch-Token": TOKEN },
      body: JSON.stringify(obj || {})
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) { if (cb) cb(d); })
      .catch(function () { if (cb) cb({ error: "request failed" }); });
  }
  function getJSON(url) {
    return fetch(url, { headers: { "X-Draftwatch-Token": TOKEN } })
      .then(function (r) { return r.json(); });
  }
  function reportClientState() {
    postJSON("/api/client_state", { dirty: dirty });
  }

  // ---- left panel: CodeMirror 6 editor ----
  // Markdown syntax highlighting, real undo history, search panel (Mod-F),
  // soft wrap. Theme rides on the app's CSS variables, so dark mode is free.
  var applyingRemote = false;   // programmatic doc swaps must not mark dirty
  var editorFile = null;        // file the current editor state belongs to

  var mdHighlight = CM.HighlightStyle.define([
    { tag: CM.tags.heading, fontWeight: "700", color: "var(--accent)" },
    { tag: CM.tags.strong, fontWeight: "700" },
    { tag: CM.tags.emphasis, fontStyle: "italic" },
    { tag: CM.tags.strikethrough, textDecoration: "line-through" },
    { tag: CM.tags.link, color: "var(--accent)", textDecoration: "underline" },
    { tag: CM.tags.url, color: "var(--accent)" },
    { tag: CM.tags.monospace, color: "var(--del-fg)" },
    { tag: CM.tags.quote, color: "var(--muted)", fontStyle: "italic" },
    { tag: CM.tags.contentSeparator, color: "var(--muted)" },
    { tag: CM.tags.meta, color: "var(--muted)" },
    { tag: CM.tags.processingInstruction, color: "var(--muted)" },
    { tag: CM.tags.labelName, color: "var(--muted)" }
  ]);

  // typing a markup char over a selection wraps it instead of replacing it
  // (the same affordance the old textarea handler provided)
  var WRAP_PAIRS = { "*": "*", "_": "_", "`": "`", "(": ")", "[": "]", "{": "}", '"': '"', "'": "'" };
  var wrapInput = CM.EditorView.inputHandler.of(function (view, from, to, text) {
    if (!Object.prototype.hasOwnProperty.call(WRAP_PAIRS, text)) return false;
    var sel = view.state.selection.main;
    if (sel.empty) return false;
    view.dispatch({
      changes: [
        { from: sel.from, insert: text },
        { from: sel.to, insert: WRAP_PAIRS[text] }
      ],
      selection: { anchor: sel.from + text.length, head: sel.to + text.length }
    });
    return true;
  });

  // Wrap (or, when already wrapped and toggle is set, unwrap) the selection.
  function surroundSelection(before, after, toggle) {
    var st = ed.state, sel = st.selection.main;
    var from = sel.from, to = sel.to, doc = st.doc;
    if (toggle && to > from && from >= before.length &&
        doc.sliceString(from - before.length, from) === before &&
        doc.sliceString(to, to + after.length) === after) {
      ed.dispatch({
        changes: [
          { from: from - before.length, to: from, insert: "" },
          { from: to, to: to + after.length, insert: "" }
        ],
        selection: { anchor: from - before.length, head: to - before.length }
      });
    } else {
      ed.dispatch({
        changes: [
          { from: from, insert: before },
          { from: to, insert: after }
        ],
        selection: (to > from)
          ? { anchor: from + before.length, head: to + before.length }
          : { anchor: from + before.length }
      });
    }
    ed.focus();
  }

  // Insert a [text](url) link around the selection, caret dropped in the url slot.
  function insertLink() {
    var sel = ed.state.selection.main;
    var txt = ed.state.doc.sliceString(sel.from, sel.to);
    ed.dispatch({
      changes: { from: sel.from, to: sel.to, insert: "[" + txt + "]()" },
      selection: { anchor: sel.from + txt.length + 3 }   // between ( and )
    });
    ed.focus();
  }

  var fmtKeymap = [
    { key: "Mod-b", run: function () { surroundSelection("**", "**", true); return true; } },
    { key: "Mod-i", run: function () { surroundSelection("*", "*", true); return true; } },
    { key: "Mod-e", run: function () { surroundSelection("`", "`", true); return true; } },
    { key: "Mod-k", run: function () { insertLink(); return true; } }
  ];

  function editorExtensions() {
    return [
      CM.history(),
      CM.drawSelection(),
      CM.EditorView.lineWrapping,
      CM.markdown(),
      CM.syntaxHighlighting(mdHighlight),
      CM.syntaxHighlighting(CM.defaultHighlightStyle, { fallback: true }),
      CM.search({ top: true }),
      CM.highlightSelectionMatches(),
      wrapInput,
      CM.keymap.of(fmtKeymap.concat(CM.searchKeymap, CM.historyKeymap, CM.defaultKeymap)),
      CM.placeholder("No file open — pick one with the “file” control above."),
      CM.EditorView.updateListener.of(function (u) {
        if (u.docChanged && !applyingRemote) {
          if (!dirty) { dirty = true; reportClientState(); }
          updateLeftTitle();
        }
      })
    ];
  }

  var ed = new CM.EditorView({
    state: CM.EditorState.create({ doc: "", extensions: editorExtensions() }),
    parent: $("editor-host")
  });

  function editorText() { return ed.state.doc.toString(); }

  // Replace the buffer programmatically (never marks dirty). A file switch
  // resets the whole state so undo history can't cross files.
  function setEditorDoc(text, newFile) {
    applyingRemote = true;
    try {
      if (newFile) {
        ed.setState(CM.EditorState.create({ doc: text, extensions: editorExtensions() }));
      } else if (editorText() !== text) {
        var selHead = Math.min(ed.state.selection.main.head, text.length);
        ed.dispatch({
          changes: { from: 0, to: ed.state.doc.length, insert: text },
          selection: { anchor: selHead }
        });
      }
    } finally {
      applyingRemote = false;
    }
  }

  function renderLeft() {
    var isNewFile = (editorFile !== currentFile);
    if (!currentFile) {
      editorFile = null;
      setEditorDoc("", isNewFile);       // placeholder shows through
      return;
    }
    // only replace the buffer when not dirty (do not clobber edits)
    if (!dirty) {
      setEditorDoc(rawText, isNewFile);
      editorFile = currentFile;
    }
    // keep the reading view live, but never overwrite in-progress preview edits
    if (previewMode && !dirty) renderPreview();
  }

  // ---- rendering: right panel (diff), line by line ----

  // Group segments into display lines. Each line ends at a newline segment.
  function buildLines() {
    var lines = [];
    var cur = { segs: [], changed: false, hunks: [] };
    for (var i = 0; i < segments.length; i++) {
      var s = segments[i];
      if (s.type === "newline") {
        lines.push(cur);
        cur = { segs: [], changed: false, hunks: [] };
      } else {
        cur.segs.push(s);
        if (s.type === "add" || s.type === "del") {
          cur.changed = true;
          if (cur.hunks.indexOf(s.hunk) === -1) cur.hunks.push(s.hunk);
        }
      }
    }
    // a trailing partial line (no closing newline) — include if it has content
    if (cur.segs.length) lines.push(cur);
    return lines;
  }

  // Render one display line's segments into HTML (with hunk wrappers + controls).
  function renderLineHTML(line) {
    var parts = [];
    var j = 0;
    while (j < line.segs.length) {
      var s = line.segs[j];
      if (s.type === "context") {
        parts.push(esc(s.text));
        j++;
      } else {
        var h = s.hunk;
        var reverted = decisions[h] === "reject";
        var inner = [];
        while (j < line.segs.length &&
               (line.segs[j].type === "add" || line.segs[j].type === "del") &&
               line.segs[j].hunk === h) {
          inner.push('<span class="' + line.segs[j].type + '">' + esc(line.segs[j].text) + "</span>");
          j++;
        }
        parts.push('<span class="hunk' + (reverted ? " hunk-revert" : "") + '" id="hunk-' + h +
                   '" data-hunk="' + h + '">' + inner.join("") + "</span>");
        parts.push(
          '<span class="ctrl" data-hunk="' + h + '">' +
          '<button class="rev' + (reverted ? " on" : "") + '" data-hunk="' + h +
          '" title="' + (reverted ? "reverted — click to keep" : "revert this change to the baseline") +
          '">' + (reverted ? "reverted ✗" : "revert ✗") + "</button>" +
          "</span>"
        );
      }
    }
    // a blank line still needs height
    return parts.length ? parts.join("") : "&nbsp;";
  }

  function renderDiff() {
    var scroll = $("right-scroll");
    var top = scroll.scrollTop;
    var diff = $("diff");

    // Untracked-file onboarding: with no committed version there is no baseline,
    // so the "diff" is the entire document as one added line per line — thousands
    // of revert controls for content that isn't a reviewable change. Offer to
    // start tracking (first commit) instead; the raw empty-tree diff stays
    // one click away.
    if (currentFile && baseline && baseline.kind === "empty" && !showEmptyTreeDiff) {
      hunkOrder = []; hunkKind = {}; hunkLine = {}; currentIdx = -1;
      diff.innerHTML =
        '<div class="onboard">' +
        '<p><strong>' + esc(currentFile) + " isn’t being tracked by git yet.</strong></p>" +
        "<p>There’s no earlier version to compare against, so the whole document " +
        "would show as one giant “added” change — not a useful review.</p>" +
        '<p><button id="start-tracking" class="apply">start tracking this file</button></p>' +
        '<p class="muted">This saves the current version as the first commit (a git commit). ' +
        "From then on, the panel shows only what actually changed.</p>" +
        '<p class="muted"><button id="show-empty-diff" class="linkish">show the full file as additions instead</button></p>' +
        "</div>";
      $("counts").textContent = "not tracked yet";
      $("nav").classList.add("hidden");
      buildMinimap();
      return;
    }

    // (re)compute hunk order + per-hunk kind/line position for navigation/minimap.
    // Line position is derived arithmetically — counting newlines that survive into
    // the working text as we walk segments once — rather than by reading DOM
    // offsetTop per hunk. That offsetTop approach is what made the minimap (and the
    // left-editor scroll-sync below) freeze the tab on very large documents: it
    // forces a synchronous layout on every one of thousands of reads.
    hunkOrder = hunks.slice();
    if (currentIdx >= hunkOrder.length) currentIdx = -1;   // clamp after live updates
    hunkKind = {};
    hunkLine = {};
    var lineIdx = 0;
    for (var a = 0; a < segments.length; a++) {
      var sg = segments[a];
      if (sg.type === "add" || sg.type === "del") {
        var k = hunkKind[sg.hunk];
        hunkKind[sg.hunk] = !k ? sg.type : (k === sg.type ? k : "mix");
        if (!(sg.hunk in hunkLine)) hunkLine[sg.hunk] = lineIdx;
      } else if (sg.type === "newline" && (sg.side === "common" || sg.side === "add")) {
        lineIdx++;
      }
    }
    totalLines = Math.max(1, rawText.split("\n").length);

    if (!segments.length || !hunks.length) {
      diff.innerHTML = '<span class="empty">' +
        (currentFile ? "No changes vs baseline." : "No file open.") + "</span>";
      scroll.scrollTop = top;
      buildMinimap();
      updateCounts();
      updateNav();
      return;
    }

    var lines = buildLines();

    // decide which lines are visible (all, or changed +/- CONTEXT in "changes only")
    var visible = new Array(lines.length);
    if (!changesOnly) {
      for (var v = 0; v < lines.length; v++) visible[v] = true;
    } else {
      for (var v2 = 0; v2 < lines.length; v2++) visible[v2] = false;
      for (var c = 0; c < lines.length; c++) {
        if (lines[c].changed) {
          for (var d = Math.max(0, c - CONTEXT_LINES);
               d <= Math.min(lines.length - 1, c + CONTEXT_LINES); d++) {
            visible[d] = true;
          }
        }
      }
    }

    var html = [];
    var gapIdx = 0;
    var li = 0;
    while (li < lines.length) {
      if (visible[li]) {
        var ln = lines[li];
        html.push('<div class="ln' + (ln.changed ? " changed" : "") + '">' +
                  renderLineHTML(ln) + "</div>");
        li++;
      } else {
        // collapse a run of hidden lines into a clickable expander
        var start = li;
        while (li < lines.length && !visible[li]) li++;
        var count = li - start;
        var gid = "gap-" + (gapIdx++);
        var inner = [];
        for (var g = start; g < li; g++) {
          inner.push('<div class="ln">' + renderLineHTML(lines[g]) + "</div>");
        }
        gapContent[gid] = inner.join("");
        html.push('<button class="collapser" data-gap="' + gid + '">⋯ ' + count +
                  " unchanged line" + (count === 1 ? "" : "s") + " — click to expand ⋯</button>");
      }
    }
    diff.innerHTML = html.join("");
    scroll.scrollTop = top;
    buildMinimap();
    updateCounts();
    updateNav();
  }

  var gapContent = {};

  // ---- change-map gutter (minimap) ----
  function buildMinimap() {
    var mm = $("minimap");
    mm.innerHTML = "";
    if (!hunkOrder.length) return;
    var mh = mm.clientHeight || 1;
    // Positioned by line fraction (hunkLine[id] / totalLines, computed once in
    // renderDiff()) instead of document.getElementById + el.offsetTop per hunk.
    for (var i = 0; i < hunkOrder.length; i++) {
      var id = hunkOrder[i];
      if (!(id in hunkLine)) continue;
      var frac = hunkLine[id] / totalLines;
      var tick = document.createElement("div");
      tick.className = "tick " + (hunkKind[id] || "mix");
      tick.style.top = (frac * mh) + "px";
      tick.setAttribute("data-idx", i);
      mm.appendChild(tick);
    }
    var view = document.createElement("div");
    view.className = "view";
    mm.appendChild(view);
    updateViewport();
  }

  function updateViewport() {
    var mm = $("minimap");
    var view = mm.querySelector(".view");
    if (!view) return;
    var scroll = $("right-scroll");
    var total = scroll.scrollHeight || 1;
    var mh = mm.clientHeight || 1;
    view.style.top = (scroll.scrollTop / total * mh) + "px";
    view.style.height = Math.max(8, (scroll.clientHeight / total * mh)) + "px";
  }

  // ---- navigation: prev / next change ----

  // Exact scroll-sync: bring the same change into view in the left editor when
  // you jump to it on the right (minimap click, or n/p). CodeMirror knows its
  // line geometry, so this centers the actual line — the old <textarea> version
  // could only approximate by document fraction.
  function scrollEditorToLine(lineIdx) {
    if (!ed || lineIdx == null) return;
    var docLines = ed.state.doc.lines;
    var line = ed.state.doc.line(Math.max(1, Math.min(lineIdx + 1, docLines)));
    ed.dispatch({ effects: CM.EditorView.scrollIntoView(line.from, { y: "center" }) });
  }

  function goToChange(idx) {
    if (!hunkOrder.length) return;
    if (idx < 0) idx = hunkOrder.length - 1;       // wrap
    if (idx >= hunkOrder.length) idx = 0;
    currentIdx = idx;
    var el = document.getElementById("hunk-" + hunkOrder[idx]);
    if (!el) return;
    el.scrollIntoView({ block: "center" });
    scrollEditorToLine(hunkLine[hunkOrder[idx]]);
    var prev = $("diff").querySelector(".ln.current");
    if (prev) prev.classList.remove("current");
    var lnEl = el.closest ? el.closest(".ln") : null;
    if (lnEl) lnEl.classList.add("current");
    updateNav();
  }
  function nextChange() { goToChange(currentIdx + 1); }
  function prevChange() { goToChange(currentIdx - 1); }

  function updateNav() {
    var nav = $("nav");
    if (!hunkOrder.length) { nav.classList.add("hidden"); return; }
    nav.classList.remove("hidden");
    var pos = (currentIdx >= 0 ? (currentIdx + 1) : "–");
    $("nav-pos").textContent = pos + " / " + hunkOrder.length;
  }

  function updateCounts() {
    var revert = 0;
    for (var k = 0; k < hunks.length; k++) {
      if (decisions[hunks[k]] === "reject") revert++;
    }
    $("counts").textContent = hunks.length + " change" + (hunks.length === 1 ? "" : "s") +
      " · " + revert + " to revert";
    $("apply").textContent = revert ? ("apply (revert " + revert + ")") : "apply";
  }

  // ---- nav / minimap / collapse event wiring ----
  $("next-change").addEventListener("click", nextChange);
  $("prev-change").addEventListener("click", prevChange);
  $("changes-only").addEventListener("change", function () {
    changesOnly = this.checked;
    renderDiff();
    if (currentIdx >= 0) goToChange(currentIdx); else $("right-scroll").scrollTop = 0;
  });
  $("right-scroll").addEventListener("scroll", updateViewport);
  window.addEventListener("resize", function () { buildMinimap(); });

  $("minimap").addEventListener("click", function (e) {
    var scroll = $("right-scroll");
    if (e.target && e.target.classList.contains("tick")) {
      goToChange(parseInt(e.target.getAttribute("data-idx"), 10));
      return;
    }
    var rect = this.getBoundingClientRect();
    var frac = (e.clientY - rect.top) / (rect.height || 1);
    scroll.scrollTop = frac * scroll.scrollHeight - scroll.clientHeight / 2;
  });

  // untracked-file onboarding controls (rendered inside #diff)
  $("diff").addEventListener("click", function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "start-tracking") {
      t.disabled = true;
      postJSON("/api/commit", { message: "start tracking: " + currentFile }, function (res) {
        if (res.error) {
          t.disabled = false;
          setStatus("start tracking failed: " + res.error);
          return;
        }
        setStatus("tracking started · " + (res.short || ""));
        loadBaselines();          // HEAD/commit baselines just became available
      });
    } else if (t.id === "show-empty-diff") {
      showEmptyTreeDiff = true;
      renderDiff();
    }
  });

  // expand a collapsed run of unchanged lines in place
  $("diff").addEventListener("click", function (e) {
    var c = e.target && e.target.closest ? e.target.closest(".collapser") : null;
    if (!c) return;
    var gid = c.getAttribute("data-gap");
    if (gapContent[gid] != null) {
      var wrap = document.createElement("div");
      wrap.innerHTML = gapContent[gid];
      c.parentNode.replaceChild(wrap, c);
      buildMinimap();
    }
  });

  // keyboard: n / p to step through changes (ignored while typing)
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT")) return;
    if (t && t.closest && t.closest(".cm-editor")) return;   // typing in CodeMirror
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "n") { e.preventDefault(); nextChange(); }
    else if (e.key === "p") { e.preventDefault(); prevChange(); }
  });

  // ---- revert toggle (event delegation); keep is the default, so the only
  //      per-hunk decision is "reject" (= revert to baseline) or absent (= keep) ----
  $("diff").addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button.rev") : null;
    if (!btn) return;
    var h = btn.getAttribute("data-hunk");
    if (decisions[h] === "reject") delete decisions[h]; else decisions[h] = "reject";
    renderDiff();
  });

  $("revert-all").addEventListener("click", function () {
    for (var k = 0; k < hunks.length; k++) decisions[hunks[k]] = "reject";
    renderDiff();
  });
  $("keep-all").addEventListener("click", function () {
    decisions = {};                       // clear all revert marks (keep everything)
    renderDiff();
  });

  $("apply").addEventListener("click", function () {
    if (dirty) {
      if (!window.confirm("You have unsaved edits in the editor. Applying reverts will overwrite them. Proceed?")) {
        return;
      }
    }
    var d = {};
    for (var k = 0; k < hunks.length; k++) {
      if (decisions[hunks[k]] === "reject") d[hunks[k]] = "reject";
    }
    $("apply").disabled = true;
    postJSON("/api/apply", { decisions: d, diff_epoch: diffEpoch }, function (res) {
      $("apply").disabled = false;
      if (res.stale) {
        // the file/baseline changed under the rendered view; the server already
        // broadcast a fresh payload (decisions survive content-addressing)
        setStatus("apply refused: the file changed underneath — view refreshed, check your marks and apply again");
        return;
      }
      if (res.error) { setStatus("apply failed: " + res.error); return; }
      var n = Object.keys(d).length;
      setStatus(n ? ("applied · reverted " + n + " change" + (n === 1 ? "" : "s")) : "applied · no reverts");
    });
  });

  // ---- commit: the final step of the review loop ----
  // A git commit of the current file state; the server advances the baseline to
  // the new HEAD, so the diff clears to zero and the next agent pass starts clean.
  //
  // Two-step, so a commit is never a single stray click: the first click on
  // "commit" reveals an optional message box (and focuses it); the second click
  // — or Enter in the box — actually commits. Esc cancels.
  var commitArmed = false;
  function armCommit() {
    commitArmed = true;
    $("commit-msg").classList.remove("hidden");
    $("commit-btn").classList.add("armed");
    $("commit-msg").focus();
    setStatus("optional: type a message, then click commit again (Esc to cancel)");
  }
  function disarmCommit() {
    commitArmed = false;
    $("commit-msg").classList.add("hidden");
    $("commit-msg").value = "";
    $("commit-btn").classList.remove("armed");
  }
  function doCommit() {
    if (!currentFile) { setStatus("no file open"); return; }
    if (dirty) {
      if (!window.confirm("You have unsaved edits in the editor. Commit records the file as it is on disk, without them. Proceed?")) {
        return;
      }
    }
    var msg = $("commit-msg").value;
    $("commit-btn").disabled = true;
    postJSON("/api/commit", { message: msg }, function (res) {
      $("commit-btn").disabled = false;
      if (res.error) { setStatus("commit failed: " + res.error); return; }
      disarmCommit();
      decisions = {};              // reviewed content is committed; marks are spent
      setStatus("committed · " + (res.short || ""));
    });
  }
  $("commit-btn").addEventListener("click", function () {
    if (commitArmed) doCommit(); else armCommit();
  });
  $("commit-msg").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); doCommit(); }
    else if (e.key === "Escape") { e.preventDefault(); disarmCommit(); $("commit-btn").focus(); }
  });

  $("save").addEventListener("click", function () {
    var text = editorText();
    $("save").disabled = true;
    // Our own save is not a disk conflict. Clear dirty *now*, before the save's
    // own "update" echoes back over SSE, so it can't trip the conflict banner.
    dirty = false;
    reportClientState();
    updateLeftTitle();
    postJSON("/api/save", { text: text }, function (res) {
      $("save").disabled = false;
      if (res.error) {
        dirty = true;                 // save failed -> we still have unsaved edits
        reportClientState();
        updateLeftTitle();
        setStatus("save failed: " + res.error);
        return;
      }
      setStatus("saved");
    });
  });

  // ---- edit state tracking ----
  function updateLeftTitle() {
    $("left-title").textContent = dirty
      ? "working text — editing · UNSAVED (click “save to file”)"
      : "working text — editing";
    $("left-title").classList.toggle("unsaved", dirty);
    $("save").classList.toggle("attn", dirty);
  }

  // ---- markdown preview: an editable rendered view ----
  // Renders the CURRENT BUFFER (unsaved edits included) as HTML. Sanitization
  // is load-bearing: the file's author is a semi-trusted AI agent, and an
  // unsanitized <script> in a .md would run with access to the session token.
  // marked -> DOMPurify -> innerHTML, nothing else.
  //
  // The preview is also a *writing surface*: it is contenteditable, and edits
  // are converted back to markdown (Turndown) and pushed into the CodeMirror
  // source. That round-trip normalizes markdown (heading/emphasis/spacing
  // conventions can shift), so entering preview never rewrites the source on its
  // own — only an actual edit here does. Source view stays byte-exact.
  var previewMode = false;
  var TURN = new TurndownService({
    headingStyle: "atx",
    hr: "---",
    bulletListMarker: "-",
    codeBlockStyle: "fenced",
    emDelimiter: "*",
    strongDelimiter: "**",
    linkStyle: "inlined"
  });
  function renderPreview() {
    var out = "";
    try {
      out = DOMPurify.sanitize(marked.parse(editorText()),
                               { USE_PROFILES: { html: true } });
    } catch (e) {
      out = "";
    }
    // set a flag so the resulting DOM mutation is not mistaken for a user edit
    // (innerHTML assignment does not fire 'input', but keep the intent explicit)
    $("preview").innerHTML = out ||
      '<p class="empty">nothing to preview</p>';
  }

  // Convert the edited rendered HTML back to markdown and push it into the
  // source buffer, marking it dirty. Debounced so typing stays smooth.
  var previewSyncTimer = null;
  function syncPreviewToSource() {
    var md;
    try { md = TURN.turndown($("preview").innerHTML); }
    catch (e) { return; }
    if (md === editorText()) return;
    setEditorDoc(md, false);        // applyingRemote suppresses the CM dirty hook
    if (!dirty) { dirty = true; reportClientState(); }
    updateLeftTitle();
  }
  function schedulePreviewSync() {
    if (previewSyncTimer) clearTimeout(previewSyncTimer);
    previewSyncTimer = setTimeout(syncPreviewToSource, 350);
  }
  function flushPreviewSync() {
    if (previewSyncTimer) { clearTimeout(previewSyncTimer); previewSyncTimer = null; syncPreviewToSource(); }
  }

  function setPreviewMode(on) {
    if (!on) flushPreviewSync();    // don't lose a pending edit when leaving preview
    previewMode = on;
    $("preview").classList.toggle("hidden", !on);
    $("editor-host").classList.toggle("hidden", on);
    $("preview").setAttribute("contenteditable", on ? "true" : "false");
    // the format buttons + save stay available in preview — you can write here too
    $("view-toggle").textContent = on ? "source" : "preview";
    if (on) renderPreview();
  }
  $("view-toggle").addEventListener("click", function () { setPreviewMode(!previewMode); });

  $("preview").addEventListener("input", function () {
    if (previewMode) schedulePreviewSync();
  });
  // In the editable preview a plain click places the caret; hold Cmd/Ctrl to
  // open a link in a new tab (navigating this tab would tear down the session).
  $("preview").addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    if (previewMode && !(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    window.open(a.getAttribute("href"), "_blank", "noopener");
  });

  // ---- formatting toolbar ----
  // A toolbar button must not steal focus/selection from the editor, so cancel
  // the default mousedown focus shift and act on the retained selection.
  // (Bold/italic/code/link keyboard shortcuts and type-*-over-selection wrapping
  // live in the CodeMirror keymap/inputHandler above.)
  function wireFmt(id, fn) {
    var b = $(id);
    b.addEventListener("mousedown", function (e) { e.preventDefault(); });
    b.addEventListener("click", function () { fn(); });
  }
  // Wrap the current preview selection in a tag (used for inline code, which
  // execCommand has no command for). No-op on an empty selection.
  function wrapPreviewSelection(tag) {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
    var range = sel.getRangeAt(0);
    var el = document.createElement(tag);
    try {
      range.surroundContents(el);
    } catch (e) {
      // surroundContents throws if the range crosses element boundaries;
      // extract-and-reinsert handles that case
      el.appendChild(range.extractContents());
      range.insertNode(el);
    }
    sel.removeAllRanges();
    var r = document.createRange();
    r.selectNodeContents(el);
    sel.addRange(r);
  }

  // In source view these act on the CodeMirror selection; in the editable
  // preview they act on the rendered DOM and sync back to source. Bold/italic
  // use execCommand (it toggles and sets typing state); code wraps the selection
  // in <code>; link uses createLink.
  function fmtBold() {
    if (previewMode) { document.execCommand("bold"); schedulePreviewSync(); }
    else surroundSelection("**", "**", true);
  }
  function fmtItalic() {
    if (previewMode) { document.execCommand("italic"); schedulePreviewSync(); }
    else surroundSelection("*", "*", true);
  }
  function fmtCode() {
    if (previewMode) { wrapPreviewSelection("code"); schedulePreviewSync(); }
    else surroundSelection("`", "`", true);
  }
  function fmtLink() {
    if (previewMode) {
      var url = window.prompt("Link URL:", "https://");
      if (url) { document.execCommand("createLink", false, url); schedulePreviewSync(); }
    } else insertLink();
  }
  wireFmt("fmt-bold",   fmtBold);
  wireFmt("fmt-italic", fmtItalic);
  wireFmt("fmt-link",   fmtLink);

  // Formatting shortcuts inside the editable preview. In source view CodeMirror's
  // own keymap handles these; in preview, focus is in the contenteditable (not
  // CodeMirror), so wire the same Cmd/Ctrl-B/I/E/K here. We preventDefault and
  // run the action ourselves, so behavior is identical across browsers and the
  // native window (some webviews don't wire Cmd-B/I to execCommand natively, and
  // Cmd-E/Cmd-K have no native contenteditable behavior at all).
  $("preview").addEventListener("keydown", function (e) {
    if (!previewMode) return;
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    var k = e.key.toLowerCase();
    if (k === "b") { e.preventDefault(); fmtBold(); }
    else if (k === "i") { e.preventDefault(); fmtItalic(); }
    else if (k === "e") { e.preventDefault(); fmtCode(); }
    else if (k === "k") { e.preventDefault(); fmtLink(); }
  });

  // guard against losing unsaved edits by closing/reloading the tab
  window.addEventListener("beforeunload", function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---- baseline dropdown ----
  function loadBaselines() {
    getJSON("/api/baselines").then(function (d) {
      var sel = $("baseline");
      sel.innerHTML = "";
      var opts = [];
      // Uncommitted file: HEAD holds no version of it, so the only git baseline
      // is the empty tree (whole file reads as new). For committed files, offer
      // "last push" (the default), HEAD, and the file's commit history.
      if (d.untracked) {
        var emptyLabel = (d.current && d.current.kind === "empty" && d.current.label)
          ? d.current.label : "empty tree · file not yet committed";
        opts.push({ value: "empty|empty", text: emptyLabel });
      } else {
        if (d.push) opts.push({ value: "push|" + d.push.ref, text: d.push.label });
        if (d.head) opts.push({ value: "head|HEAD", text: d.head.label });
        (d.commits || []).forEach(function (c) {
          opts.push({ value: "commit|" + c.ref, text: c.label, label: c.label });
        });
      }
      opts.forEach(function (o) {
        var el = document.createElement("option");
        el.value = o.value;
        el.textContent = o.text;
        if (o.label) el.setAttribute("data-label", o.label);
        sel.appendChild(el);
      });
      // select current
      if (d.current) {
        var want = d.current.kind + "|" + (d.current.ref || "");
        for (var i = 0; i < sel.options.length; i++) {
          if (sel.options[i].value === want) { sel.selectedIndex = i; break; }
        }
      }
    }).catch(function () {});
  }

  $("baseline").addEventListener("change", function () {
    var sel = $("baseline");
    var parts = sel.value.split("|");
    var kind = parts[0];
    var ref = parts.slice(1).join("|");
    var body = { kind: kind };
    if (kind === "commit") {
      body.ref = ref;
      var opt = sel.options[sel.selectedIndex];
      if (opt) body.label = opt.getAttribute("data-label") || opt.textContent;
    }
    postJSON("/api/baseline", body, function (res) {
      if (res.error) setStatus("baseline error: " + res.error);
    });
  });

  // ---- file picker (open / switch the watched file) ----
  function addFileOption(sel, f) {
    var o = document.createElement("option");
    o.value = f; o.textContent = f; sel.appendChild(o);
  }
  function loadFiles() {
    getJSON("/api/files").then(function (d) {
      var files = d.files || [];
      var cur = d.current || currentFile;
      var sel = $("file-select"); sel.innerHTML = "";       // dropdown: writing files
      var ph = document.createElement("option");
      ph.value = ""; ph.textContent = "— pick a file —"; sel.appendChild(ph);
      var listed = {};
      files.forEach(function (f) {
        var low = f.toLowerCase();
        for (var i = 0; i < WRITING_EXT.length; i++) {
          if (low.lastIndexOf(WRITING_EXT[i]) === low.length - WRITING_EXT[i].length) {
            addFileOption(sel, f); listed[f] = true; break;
          }
        }
      });
      if (cur && !listed[cur]) addFileOption(sel, cur);
      if (cur) sel.value = cur;
    }).catch(function () {});
  }
  function syncFileControls() {
    var sel = $("file-select");
    if (currentFile) {
      var found = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === currentFile) { found = true; break; }
      }
      if (!found) addFileOption(sel, currentFile);
      sel.value = currentFile;
    } else if (sel.options.length) {
      sel.value = "";
    }
  }
  function openFile(path) {
    if (!path) return;
    if (dirty) {
      if (!window.confirm("You have unsaved edits.\n\nOK = discard them and open " + path +
                          ".\nCancel = stay on the current file.")) return;
      dirty = false;
    }
    postJSON("/api/open", { path: path }, function (res) {
      if (res.error) { setStatus("open failed: " + res.error); return; }
      dirty = false;
      currentFile = res.file;
      lastBaselineKind = null;
      currentIdx = -1;
      reportClientState();
      updateLeftTitle();
      loadBaselines();
      // the server's SSE 'update' will populate the panels
    });
  }
  $("file-select").addEventListener("change", function () { if (this.value) openFile(this.value); });

  // loadFiles() previously only ran once at page load, so a file added (or removed)
  // on disk after that never showed up in the dropdown for the rest of the session.
  // Refresh right before the control is used — mousedown fires before a <select>
  // opens its popup in every browser that matters here — plus a periodic poll as a
  // fallback for whichever browser doesn't actually repaint an already-open native
  // dropdown from a DOM mutation.
  $("file-select").addEventListener("mousedown", loadFiles);
  setInterval(loadFiles, 4000);

  // ---- banner (disk_changed) ----
  var pendingDisk = null;   // payload waiting for the user's decision
  function showBanner(payload) {
    pendingDisk = payload;
    $("banner-text").textContent = "file changed on disk while you were editing.";
    $("banner").classList.add("show");
  }
  function hideBanner() { $("banner").classList.remove("show"); pendingDisk = null; }
  $("banner-reload").addEventListener("click", function () {
    if (pendingDisk) applyPayload(pendingDisk, true);
    dirty = false;
    reportClientState();
    updateLeftTitle();
    hideBanner();
    renderLeft();
  });
  $("banner-overwrite").addEventListener("click", function () {
    // keep the user's edits, dismiss the banner
    hideBanner();
  });

  // ---- apply an SSE payload to local state ----
  function applyPayload(p, force) {
    if ("file" in p) {
      if (p.file !== currentFile) {
        decisions = {};                              // decisions never cross files
        showEmptyTreeDiff = false;                   // onboarding opt-out is per file
      }
      currentFile = p.file;                          // relpath or null
    }
    rawText = p.raw_text;
    segments = p.segments || [];
    hunks = p.hunks || [];
    if ("diff_epoch" in p) diffEpoch = p.diff_epoch;
    baseline = (p.baseline !== undefined) ? p.baseline : baseline;
    // Hunk ids are content-addressed (server-side hash of the change + its
    // context), so a decision stays valid across payloads as long as that
    // change still exists. Keep decisions; prune only ids that vanished.
    var live = {};
    for (var li = 0; li < hunks.length; li++) live[hunks[li]] = true;
    for (var dk in decisions) {
      if (Object.prototype.hasOwnProperty.call(decisions, dk) && !live[dk]) {
        delete decisions[dk];
      }
    }
    syncFileControls();
    setStatusBaseline();
    renderDiff();
    // refresh left unless we are protecting a dirty edit buffer
    if (force || !dirty) renderLeft();
  }

  var lastTransientTs = 0;
  function setStatus(msg) {
    $("status").textContent = msg;
    if (msg) lastTransientTs = Date.now();
  }
  // The baseline dropdown right next to this status line already shows the full
  // label ("last push · origin/main · 28 minutes ago", etc.) — repeating all of it
  // here is pure duplication. For the two default-assigned baselines (push, head),
  // show just the recency, which is the one fact that's actually decision-relevant
  // (is this comparison fresh or stale); the ref/commit detail stays one click away
  // in the dropdown. Manually-picked commits and the empty-tree baseline keep their
  // full label, since there the specific choice is the point.
  // The baseline dropdown itself already shows the full label ("last push ·
  // origin/main · 28 minutes ago", etc.) and the file dropdown above already shows
  // the filename — restating either here would just be duplication. #status is left
  // free for what it's actually for: transient connection/save/error feedback (set
  // directly via setStatus() elsewhere — "disconnected – retrying…", "saved",
  // "apply failed: ...", and so on).
  function setStatusBaseline() {
    if (!currentFile) { setStatus("no file open — pick a file above"); return; }
    // don't let a routine SSE payload wipe fresh transient feedback
    // ("saved", "committed · abc1234", ...) the instant it appears
    if (Date.now() - lastTransientTs > 4000) setStatus("");
    // keep dropdown selection in sync
    if (baseline) {
      var want = baseline.kind + "|" + (baseline.ref || "");
      var sel = $("baseline");
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === want) { sel.selectedIndex = i; break; }
      }
    }
  }

  // ---- SSE ----
  function connect() {
    // EventSource cannot set headers, so the token rides in the query string
    var es = new EventSource("/events?t=" + encodeURIComponent(TOKEN));
    es.onmessage = function (e) {
      var p;
      try { p = JSON.parse(e.data); } catch (err) { return; }
      // A banner is only warranted when the file on disk genuinely changed
      // *underneath unsaved edits*. We confirm the content actually differs from
      // what we last loaded (p.raw_text !== rawText), so neither an SSE reconnect
      // echo nor our own save (which re-broadcasts an "update" with no outside
      // change) can trip it. Without unsaved edits, just apply the update.
      var editingUnsaved = dirty;
      var diskActuallyChanged = (p.raw_text !== rawText);
      if (editingUnsaved && diskActuallyChanged &&
          (p.type === "disk_changed" || p.type === "update")) {
        showBanner(p);          // keep the buffer untouched until the user decides
        return;
      }
      applyPayload(p, false);
      if (baseline && baseline.kind) loadBaselinesLabelRefresh();
    };
    es.onerror = function () { setStatus("disconnected – retrying…"); };
    es.onopen = function () { /* status set on first message */ };
  }

  var lastBaselineKind = null;
  function loadBaselinesLabelRefresh() {
    // refresh the commit dropdown if the baseline kind changed (e.g. file committed)
    if (baseline && baseline.kind !== lastBaselineKind) {
      lastBaselineKind = baseline.kind;
      loadBaselines();
    }
  }

  // ---- theme toggle (auto / light / dark) ----
  // Manual choice is stored in localStorage and set as data-theme on <html>
  // (the pre-paint script in <head> applies it on load, so there is no flash);
  // "auto" removes the attribute and defers to the OS via prefers-color-scheme.
  var THEMES = ["auto", "light", "dark"];
  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    return (t === "light" || t === "dark") ? t : "auto";
  }
  function applyTheme(t) {
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    try {
      if (t === "auto") localStorage.removeItem("draftwatch-theme");
      else localStorage.setItem("draftwatch-theme", t);
    } catch (e) {}
    $("theme-toggle").textContent = "theme: " + t;
  }
  applyTheme(currentTheme());     // sync the button label to the pre-paint state
  $("theme-toggle").addEventListener("click", function () {
    var i = THEMES.indexOf(currentTheme());
    applyTheme(THEMES[(i + 1) % THEMES.length]);
  });

  // ---- about modal ----
  function showAbout(on) { $("about").classList.toggle("show", on); }
  $("about-btn").addEventListener("click", function () { showAbout(true); });
  $("about-close").addEventListener("click", function () { showAbout(false); });
  $("about").addEventListener("click", function (e) {
    if (e.target === this) showAbout(false);   // click the dimmed backdrop to close
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $("about").classList.contains("show")) showAbout(false);
  });

  // ---- init ----
  loadFiles();
  loadBaselines();
  reportClientState();
  connect();
  if (APP_FALLBACK) {
    setStatus("native window unavailable — running in the browser · pip install 'draftwatch[app]'");
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def fail(msg, code=2):
    sys.stderr.write("draftwatch: " + msg + "\n")
    sys.exit(code)


def _webview_available():
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def _run_app_window(url, title):
    """Open the UI in a native window (PyWebView). Returns True when the window
    ran and was closed by the user; False when unavailable, in which case the
    caller falls back to browser mode. The GUI event loop must own the main
    thread (a hard Cocoa requirement on macOS), which is why app mode moves the
    HTTP server to a background thread before calling this."""
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "draftwatch: the native window needs pywebview, which isn't installed:\n"
            "    pip install 'draftwatch[app]'\n"
            "falling back to the browser.\n")
        return False
    try:
        webview.create_window(title, url, width=1280, height=860,
                              min_size=(880, 560))
        webview.start()
        return True
    except Exception as e:
        sys.stderr.write(
            "draftwatch: could not open a native window (%s) — "
            "falling back to the browser.\n" % e)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="draftwatch",
        description="Watch a .md file and review an agent's edits as a real git diff.",
    )
    parser.add_argument(
        "target", nargs="?", default=None,
        help="path to the file to watch (relative or absolute). Optional: if omitted, "
             "draftwatch starts in the current git repo and you pick a file in the browser.")
    parser.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind host (default 127.0.0.1). WARNING: changing this exposes the tool "
             "on your network and is not recommended.",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="do not auto-open the browser on startup (it opens by default).",
    )
    parser.add_argument(
        "--app", action="store_true",
        help="force the native window (the default when pywebview is installed). "
             "Needs pywebview (pip install 'draftwatch[app]'); falls back to the "
             "browser if it isn't available.",
    )
    parser.add_argument(
        "--no-app", action="store_true",
        help="never open the native window; use the browser even when pywebview "
             "is installed.",
    )
    args = parser.parse_args(argv)

    if args.app and args.no_app:
        fail("--app and --no-app are mutually exclusive")

    # --- resolve the git repo root ---
    # With a target: from the target's directory. Without one: from the current dir.
    if args.target is not None:
        if not os.path.exists(args.target):
            fail("target file does not exist: %s" % args.target)
        if os.path.isdir(args.target):
            fail("target is a directory, not a file: %s" % args.target)
        base_dir = os.path.dirname(os.path.realpath(os.path.abspath(args.target)))
    else:
        base_dir = os.getcwd()

    if not is_inside_work_tree(base_dir):
        fail("'%s' is not inside a git repository. Run draftwatch from a git working tree."
             % base_dir)
    root = git_toplevel(base_dir)
    if root is None:
        fail("could not resolve the git repository root for: %s" % base_dir)

    state = State(root)

    init_note = "no file open — choose one in the browser"
    if args.target is not None:
        try:
            state.open_file(os.path.abspath(args.target))
        except ValueError as e:
            fail(str(e))
        init_note = "baseline: " + state.baseline.get("label", "HEAD")

    # --- HTTP server ---
    # per-session token: gates every /api/* and /events request (see Handler._guard)
    token = secrets.token_urlsafe(32)
    Handler.state = state
    Handler.token = token
    Handler.allowed_hosts = {"127.0.0.1:%d" % args.port, "localhost:%d" % args.port}
    if args.host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        Handler.allowed_hosts.add("%s:%d" % (args.host, args.port))
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        fail("could not bind %s:%d (%s)" % (args.host, args.port, e), code=1)
    httpd.daemon_threads = True

    stop_event = threading.Event()
    watcher = threading.Thread(target=watch_loop, args=(state, stop_event), daemon=True)
    watcher.start()

    # a browser cannot navigate to 0.0.0.0; always send it to loopback.
    # The token rides in the URL so the page can authenticate its requests.
    open_url = "http://127.0.0.1:{}/?t={}".format(args.port, token)

    # Launch surface: the native window is the flagship — used by default when
    # pywebview is installed. --app forces the attempt; --no-app disables it;
    # --no-open signals headless intent, so the default (but not an explicit
    # --app) skips the window too.
    want_app = False
    if not args.no_app:
        if args.app:
            want_app = True
        elif not args.no_open and _webview_available():
            want_app = True

    if state.has_file():
        print("draftwatch · watching {}".format(state.relpath))
    else:
        print("draftwatch · no file open")
    print(init_note)
    if args.host != "127.0.0.1":
        print("WARNING: bound to {} — the tool is exposed on your network.".format(args.host))
    print("open {}   (ctrl-c to stop)".format(open_url), flush=True)
    if want_app:
        print("mode: native window — close the window to stop", flush=True)
    elif not args.no_app and not args.no_open and not args.app:
        print("tip: pip install 'draftwatch[app]' to get the native window", flush=True)

    def open_browser():
        # open the browser in a background thread so a slow/headless launcher
        # can't delay or crash startup
        def _open():
            try:
                webbrowser.open(open_url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    if want_app:
        # native window mode: the server serves from a background thread while
        # the GUI owns the main thread; closing the window shuts everything down.
        # `app=1` lets the page style itself as app chrome (no redundant
        # wordmark — the native title bar carries it).
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        win_title = "Draftwatch · {}".format(state.relpath) if state.has_file() else "Draftwatch"
        if _run_app_window(open_url + "&app=1", win_title):
            print("\ndraftwatch: window closed — stopping")
            stop_event.set()
            httpd.shutdown()
            return
        # pywebview unavailable: keep the already-running server and behave
        # like browser mode; `appfallback=1` makes the page say why the native
        # window didn't appear instead of leaving the switch silent.
        if not args.no_open:
            def _open_fb():
                try:
                    webbrowser.open(open_url + "&appfallback=1")
                except Exception:
                    pass
            threading.Thread(target=_open_fb, daemon=True).start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\ndraftwatch: stopping")
        finally:
            stop_event.set()
            httpd.shutdown()
        return

    if not args.no_open:
        open_browser()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ndraftwatch: stopping")
    finally:
        stop_event.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
