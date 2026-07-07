#!/usr/bin/env python3
"""draftwatch — watch a single .md file and review an agent's edits as a real git diff.

A read-and-review tool for writers whose .md files are being edited by an
autonomous AI agent. It shows the exact, git-backed diff of what changed and lets
the writer keep or revert the agent's changes hunk by hunk, or write into the file directly.

The diff is always real `git diff --word-diff`, never a JS/Python approximation.
"""

import argparse
import atexit
import errno
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

# Embedded terminal (see TERMINAL_PLAN.md). POSIX only: on Windows the pty
# module doesn't exist, the import fails, and Draftwatch runs exactly as it
# did without the feature — same codebase, graceful degradation, no fork.
try:
    from . import term as _term
except ImportError:
    try:
        import term as _term            # running app.py outside the package
    except ImportError:
        _term = None
TERM_SUPPORTED = _term is not None

# Single source of truth for the version and the date of the latest release.
# __init__.py re-exports __version__; the About panel shows both (injected into
# the served page from these constants).
__version__ = "0.2.0"
RELEASE_DATE = "2026-07-06"

# Preferred port. When --port is not given, Draftwatch tries this first and
# falls back to a free port if it is busy (so a second instance can start while
# the first is running). An explicit --port is honored exactly, with no fallback.
DEFAULT_PORT = 8787

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


def resolve_new_file(root, path):
    """Resolve a path for a NEW file inside the repo, or raise ValueError.

    Same containment rules as resolve_target (resolved path must live inside the
    repo root), but the file must NOT already exist — creating over an existing
    file would silently clobber it; the picker is the way to open one.
    """
    if not path or not path.strip():
        raise ValueError("no filename given")
    p = path.strip()
    cand = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    root_prefix = root.rstrip(os.sep) + os.sep
    if not cand.startswith(root_prefix):
        raise ValueError("refusing a path outside the repository")
    if os.path.isdir(cand):
        raise ValueError("path is a directory, not a file: %s" % path)
    if os.path.exists(cand):
        raise ValueError("file already exists: %s — pick it with the file control instead" % path)
    return cand


def list_repo_files(root, has_repo=True, limit=5000):
    """Files for the picker. In a git repo: tracked + non-ignored untracked,
    sorted, deduped. In write-only mode (no repo), fall back to a filesystem
    walk so the picker still works."""
    if not has_repo:
        return list_dir_files(root, limit)
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


# Directories a filesystem walk skips in write-only mode: version-control,
# dependency/vendor trees, and build/cache output. Any hidden dir (dot-prefixed)
# is skipped too. This keeps the picker to files a writer would actually open.
_WALK_SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".quarto", "_site", "_book", "renv", ".Rproj.user", ".ipynb_checkpoints",
}


def list_dir_files(root, limit=5000):
    """Plain filesystem walk (write-only mode, no git). Repo-relative paths,
    sorted, with VCS/vendor/build and hidden directories pruned."""
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _WALK_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            result.append(rel)
    result.sort()
    return result[:limit]


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
    def __init__(self, root, relpath=None, abspath=None, has_repo=True):
        self.root = root
        self.has_repo = has_repo         # False = write-only mode (no git in `root`)
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
        if not self.has_repo:
            # write-only mode: no git, so there is no baseline or diff. The
            # editor, preview and save still operate on the file directly.
            with self.lock:
                self.relpath = relpath
                self.abspath = abspath
                self.untracked = True
                self.last_mtime = file_mtime(abspath)
                self.baseline = {"kind": "none", "ref": "",
                                 "label": "no git repository — write-only"}
            return relpath
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
    """Read the file, run git diff, parse, and build an SSE payload dict.

    Every payload carries `repo` (is `root` a git repo) and `repo_dir` (the
    folder a UI-triggered `git init` would create the repo in). In write-only
    mode (`repo` False) there is no baseline or diff — the file content still
    rides along so the editor works."""
    with state.lock:
        baseline = dict(state.baseline)
        relpath = state.relpath
        abspath = state.abspath
        has_repo = state.has_repo
        root = state.root
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
            "repo": has_repo,
            "repo_dir": root,
        }
    raw_text = read_text(abspath)
    if not has_repo:
        return {
            "type": event_type,
            "file": relpath,
            "raw_text": raw_text,
            "baseline": None,
            "segments": [],
            "hunks": [],
            "file_mtime": file_mtime(abspath),
            "diff_epoch": None,
            "repo": False,
            "repo_dir": root,
        }
    diff_text = git_diff_text(root, baseline, relpath, abspath)
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
        "diff_epoch": compute_diff_epoch(root, baseline, raw_text),
        "repo": True,
        "repo_dir": root,
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
    "xterm.js": "application/javascript; charset=utf-8",
    "xterm.css": "text/css; charset=utf-8",
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
    term_enabled = False    # POSIX + not --no-terminal (set in main)
    term_session = None     # the one TermSession, or None when disabled

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
        if path.startswith("/api/") or path in ("/events", "/term/events"):
            tok = self.headers.get("X-Draftwatch-Token")
            if not tok:
                # Query-string fallback exists only for EventSource, which
                # cannot set headers. The terminal POST routes are header-only:
                # keystrokes must never ride in URLs (history, logs, referrers).
                if path.startswith("/api/term/"):
                    tok = ""
                else:
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
                    .replace("{{RELEASE_DATE}}", RELEASE_DATE)
                    .replace("{{TERM}}", "1" if self.term_enabled else "0"))
            self._send(200, page, "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/events":
            self._serve_events()
        elif path == "/term/events":
            self._serve_term_events()
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
            has_repo = st.has_repo
        if not has_repo:
            # write-only mode: no baselines to offer
            return self._json({"current": None, "head": None, "push": None,
                               "commits": [], "untracked": False, "repo": False})
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
            has_repo = st.has_repo
        self._json({"files": list_repo_files(st.root, has_repo),
                    "current": current, "repo": has_repo})

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
        elif path == "/api/new":
            self._api_new()
        elif path == "/api/close":
            self._api_close()
        elif path == "/api/init-repo":
            self._api_init_repo()
        elif path == "/api/term/open":
            self._api_term_open()
        elif path == "/api/term/input":
            self._api_term_input()
        elif path == "/api/term/resize":
            self._api_term_resize()
        elif path == "/api/term/close":
            self._api_term_close()
        else:
            self._send(404, "not found", "text/plain")

    # ---- terminal panel ----
    # All routes 404 when the feature is off (--no-terminal, or Windows where
    # the pty module doesn't exist) — disabled means absent, not forbidden.
    # _guard has already enforced Host, Origin, and the header-only token.

    def _term_ok(self):
        if not self.term_enabled or self.term_session is None:
            self._send(404, "not found", "text/plain")
            return False
        return True

    def _api_term_open(self):
        if not self._term_ok():
            return
        body = self._read_json()
        sess = self.term_session
        started = sess.start(body.get("cols", 80), body.get("rows", 24))
        self._json({"ok": True, "started": started,
                    "running": sess.running(), "pid": sess.pid()})

    def _api_term_input(self):
        if not self._term_ok():
            return
        data = self._read_json().get("data")
        if not isinstance(data, str) or data == "":
            return self._json({"error": "no input"}, 400)
        ok = self.term_session.write(data.encode("utf-8", "replace"))
        self._json({"ok": ok} if ok else {"error": "terminal not running"},
                   200 if ok else 409)

    def _api_term_resize(self):
        if not self._term_ok():
            return
        body = self._read_json()
        try:
            cols, rows = int(body.get("cols")), int(body.get("rows"))
        except (TypeError, ValueError):
            return self._json({"error": "cols/rows must be integers"}, 400)
        self._json({"ok": self.term_session.resize(cols, rows)})

    def _api_term_close(self):
        if not self._term_ok():
            return
        self.term_session.terminate()
        self._json({"ok": True, "running": self.term_session.running()})

    def _serve_term_events(self):
        if not self._term_ok():
            return
        sess = self.term_session
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q, hello = sess.subscribe()
        try:
            # scrollback replay first, so a reconnecting page repaints instantly
            self._sse_write(json.dumps(hello))
            while True:
                try:
                    self._sse_write(json.dumps(q.get(timeout=15)))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sess.unsubscribe(q)

    def _api_init_repo(self):
        """Turn the write-only root into a git repository (`git init`), then
        switch to review mode. Nothing is committed: a brand-new repo has no
        HEAD, so an open file lands on the empty-tree baseline and the normal
        'start tracking this file' onboarding takes over from there."""
        st = self.state
        with st.lock:
            if st.has_repo:
                return self._json({"ok": True, "already": True})
            root = st.root
        rc, out, err = run_git(["init"], root)
        if rc != 0:
            return self._json({"error": "git init failed: " + (err.strip() or out.strip())}, 500)
        with st.lock:
            st.has_repo = True
            cur = st.abspath
        # re-open the current file so its baseline reflects git (empty tree for
        # the fresh, commit-less repo); harmless if nothing is open.
        if cur is not None:
            try:
                st.open_file(cur)
            except Exception as e:
                sys.stderr.write("draftwatch: reopen after init failed: %s\n" % e)
        broadcast_diff(st, "update")
        self._json({"ok": True})

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
        if not st.has_repo:
            return self._json({"error": "no git repository — write-only mode; "
                                        "initialize git to review changes"}, 400)
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
        if not st.has_repo:
            return self._json({"error": "no git repository — write-only mode; "
                                        "nothing to apply"}, 400)
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

    def _api_close(self):
        """Detach from the watched file and return to the no-file scratch
        state. Used by the picker's "start a new file": the buffer opens blank
        and unnamed, and the name is asked at save time (via /api/new), not up
        front. The file on disk is untouched."""
        st = self.state
        with st.lock:
            st.relpath = None
            st.abspath = None
            st.untracked = False
            st.last_mtime = 0.0
            st.client_dirty = False
            st.baseline = {"kind": "head", "ref": "HEAD", "label": "HEAD"}
        broadcast_diff(st, "update")
        self._json({"ok": True})

    def _api_new(self):
        """Create a NEW file inside the repo and switch to watching it. This is
        how "start typing with no file open" becomes a real file, and what the
        picker's "start a new file" option calls. Never overwrites an existing
        file (resolve_new_file refuses); empty text is fine — you can create
        the file first and write into it after."""
        st = self.state
        body = self._read_json()
        text = body.get("text", "")
        if not isinstance(text, str):
            return self._json({"error": "text must be a string"}, 400)
        try:
            abspath = resolve_new_file(st.root, body.get("path", ""))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        try:
            parent = os.path.dirname(abspath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            write_file(abspath, text)
            relpath = st.open_file(abspath)
            with st.lock:
                st.client_dirty = False
            payload = broadcast_diff(st, "update")
            return self._json({"ok": True, "file": relpath,
                               "baseline": payload["baseline"]})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _api_save(self):
        st = self.state
        if not st.has_file():
            return self._json({"error": "no file open — use /api/new to create one"}, 400)
        body = self._read_json()
        if "text" not in body:
            return self._json({"error": "missing text"}, 400)
        if (body.get("path") or "").strip():
            # creation goes through /api/new; refusing here means a stray
            # "path" can never silently overwrite the open file
            return self._json({"error": "save does not take a path — use /api/new to create a file"}, 400)
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
        if not st.has_repo:
            return self._json({"error": "no git repository — write-only mode; "
                                        "initialize git to commit"}, 400)
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
<script src="/static/xterm.js"></script>
<link rel="stylesheet" href="/static/xterm.css">
<script>
  // Resolve the saved theme + accent before first paint so there is no
  // light/dark (or color) flash. "auto" (or nothing saved) leaves the theme to
  // prefers-color-scheme via CSS; no saved accent means the default blue. An
  // unknown stored accent value simply matches no CSS rule -> blue.
  try {
    var _t = localStorage.getItem("draftwatch-theme");
    if (_t === "light" || _t === "dark") {
      document.documentElement.setAttribute("data-theme", _t);
    }
    var _a = localStorage.getItem("draftwatch-accent");
    if (_a) document.documentElement.setAttribute("data-accent", _a);
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
  /* Color themes: the picker restyles the WHOLE look, not just the accent —
     each color re-tints the paper (--bg), panels, borders, and ink the same
     way the default blue does: tinted paper, darker panel, deeper accent that
     holds button text at >=4.5:1. All stay in the same muted editorial
     register; "graphite" is the monochrome one (Cursor-style neutral greys).
     The add-green / del-red / warn-amber stay strictly semantic and identical
     across themes. Chosen via data-accent on <html> (persisted in
     localStorage, applied pre-paint by the script in <head>); no attribute =
     blue. Dark values are declared twice (manual override + auto/OS path),
     mirroring how the base theme does it. */
  :root[data-accent="teal"] {
    --bg: #f1faf7; --panel: #e2f1ec; --border: #c8e0d8;
    --text: #1c2b27; --muted: #5c6f69;
    --accent: #1e7b70; --accent-fg: #f2fbf9;
  }
  :root[data-accent="iris"] {
    --bg: #f6f5fc; --panel: #eceaf6; --border: #d8d4ea;
    --text: #24222f; --muted: #67637a;
    --accent: #6a5a96; --accent-fg: #f7f5fc;
  }
  :root[data-accent="plum"] {
    --bg: #faf5f9; --panel: #f2e8ef; --border: #e2d0dc;
    --text: #2c222a; --muted: #74616e;
    --accent: #8a4a78; --accent-fg: #fdf6fb;
  }
  :root[data-accent="graphite"] {
    --bg: #f6f6f7; --panel: #ebebed; --border: #d7d7db;
    --text: #222327; --muted: #64666d;
    --accent: #44464d; --accent-fg: #f7f7f8;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme])[data-accent="teal"] {
      --bg: #101c19; --panel: #172622; --border: #2a3f38;
      --text: #e3efeb; --muted: #8fa8a0;
      --accent: #5cbcae; --accent-fg: #07211c;
    }
    :root:not([data-theme])[data-accent="iris"] {
      --bg: #16141f; --panel: #1f1c2b; --border: #353046;
      --text: #e9e6f2; --muted: #9d97b4;
      --accent: #a89bd8; --accent-fg: #171129;
    }
    :root:not([data-theme])[data-accent="plum"] {
      --bg: #1d141a; --panel: #291c25; --border: #43303e;
      --text: #f0e7ed; --muted: #ab93a3;
      --accent: #c893b6; --accent-fg: #260f1f;
    }
    :root:not([data-theme])[data-accent="graphite"] {
      --bg: #161718; --panel: #1e1f21; --border: #323438;
      --text: #e8e9ea; --muted: #989aa1;
      --accent: #b3b6bd; --accent-fg: #141517;
    }
  }
  :root[data-theme="dark"][data-accent="teal"] {
    --bg: #101c19; --panel: #172622; --border: #2a3f38;
    --text: #e3efeb; --muted: #8fa8a0;
    --accent: #5cbcae; --accent-fg: #07211c;
  }
  :root[data-theme="dark"][data-accent="iris"] {
    --bg: #16141f; --panel: #1f1c2b; --border: #353046;
    --text: #e9e6f2; --muted: #9d97b4;
    --accent: #a89bd8; --accent-fg: #171129;
  }
  :root[data-theme="dark"][data-accent="plum"] {
    --bg: #1d141a; --panel: #291c25; --border: #43303e;
    --text: #f0e7ed; --muted: #ab93a3;
    --accent: #c893b6; --accent-fg: #260f1f;
  }
  :root[data-theme="dark"][data-accent="graphite"] {
    --bg: #161718; --panel: #1e1f21; --border: #323438;
    --text: #e8e9ea; --muted: #989aa1;
    --accent: #b3b6bd; --accent-fg: #141517;
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
       row 1 = what you're looking at (title, file picker, baseline selector —
               file and baseline split the row roughly in half)
       row 2 = actions (preview/format/save on the left; theme, accent color,
               about on the right; transient status in between)
     The variable-length status line ellipsizes instead of pushing the
     controls around. */
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
  /* the accent-picker's color dot — bound to var(--accent), so it always
     shows the live color */
  #accent-toggle .swatch {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent); border: 1px solid var(--border);
    margin-right: 6px; vertical-align: -1px;
  }

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
    grid-template-columns: 1fr 6px 1fr;
    min-height: 0;
  }
  .panel { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
  .panel.left { border-right: 1px solid var(--border); }

  /* gutters: slim grid tracks between panels, draggable to resize (widths
     persist per two-/three-panel layout; double-click resets). */
  .gutter { cursor: col-resize; background: transparent; }
  .gutter:hover, .gutter.dragging { background: var(--accent); opacity: .35; }
  body.dragging-cols { cursor: col-resize; user-select: none; -webkit-user-select: none; }

  /* terminal panel: a third column that exists only while open. Hiding it
     (body class removed) keeps the shell running server-side. */
  .panel.term, .gutter.term-gutter { display: none; }
  .panel.term { border-left: 1px solid var(--border); }
  body.term-open main { grid-template-columns: 1fr 6px 1fr 6px minmax(280px, 0.8fr); }
  body.term-open .panel.term { display: flex; }
  body.term-open .gutter.term-gutter { display: block; }
  .panel.term h2 { display: flex; align-items: center; gap: 8px; }
  .panel.term h2 .spacer { flex: 1; }
  .panel.term h2 button {
    text-transform: none; letter-spacing: normal; font-weight: 400;
  }
  #term-host { flex: 1; min-height: 0; padding: 6px 2px 2px 8px; }
  #term-host .terminal { height: 100%; }
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
  /* diff panel header: title left, the one-line how-to right. The hint
     inherits the h2 type exactly (same font, size, weight, uppercase,
     letter-spacing) so the two read as one header line. The hint's ✗ is drawn
     like the per-hunk buttons so the mapping is visual, not verbal — the
     buttons themselves stay a bare ✗ to keep dense diffs calm. The chip's
     fixed line-height + negative margin keep it from stretching the header
     taller than the left panel's. */
  h2.diffhead { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
  .diffhint { white-space: nowrap; }
  .diffhint .xdemo {
    display: inline-block; background: var(--bg); border: 1px solid var(--border);
    border-radius: 3px; padding: 0 4px; margin: -2px 0; color: var(--del-fg);
    font-size: 10px; line-height: 13px;
  }
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
  /* armed commit is a mode: the review controls have done their job, so they
     step aside and the message box gets the whole footer — otherwise a narrow
     panel (terminal open) pushes the commit button out of reach. Esc restores. */
  body.commit-armed #counts,
  body.commit-armed #nav,
  body.commit-armed .chgonly,
  body.commit-armed #revert-all,
  body.commit-armed #keep-all,
  body.commit-armed #apply,
  body.commit-armed footer .spacer { display: none !important; }
  body.commit-armed #commit-msg { flex: 1 1 auto; max-width: none; }
  .hidden { display: none !important; }
  h2.unsaved { color: var(--del-fg); }
  button.attn { box-shadow: 0 0 0 2px var(--warn-border); }
  /* file + baseline share the top row, half each (flex-basis 0 splits the
     leftover space evenly regardless of content width) */
  .filepick, .basepick { display: inline-flex; align-items: center; gap: 5px; flex: 1 1 0; min-width: 0; }
  .filepick select, .basepick select { flex: 1 1 auto; min-width: 0; width: 100%; }
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

  /* write-only mode (no git repo): the review controls are meaningless, so
     hide them and explain the trade-off in the right panel. */
  body.no-repo .basepick,
  body.no-repo .diffhint,
  body.no-repo #nav,
  body.no-repo .chgonly,
  body.no-repo #revert-all,
  body.no-repo #keep-all,
  body.no-repo #apply,
  body.no-repo #commit-msg,
  body.no-repo #commit-btn { display: none !important; }
  .onboard .repo-path {
    display: block; margin: 10px 0; padding: 8px 10px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    font-family: var(--mono); font-size: 12px; word-break: break-all;
    color: var(--text);
  }
  .onboard .warn-note {
    margin-top: 14px; padding: 10px 12px;
    background: var(--warn-bg); border: 1px solid var(--warn-border);
    border-radius: 4px;
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
    <span class="basepick">
      <label class="status" for="baseline">baseline</label>
      <select id="baseline"></select>
    </span>
  </div>
  <div class="bar" id="edit-toolbar">
    <button id="view-toggle" title="switch the left panel between markdown source and a rendered reading view">preview</button>
    <span class="status" id="fmt-label">format</span>
    <span class="fmt">
      <button id="fmt-bold" title="bold (⌘/Ctrl-B)"><b>B</b></button>
      <button id="fmt-italic" title="italic (⌘/Ctrl-I)"><i>I</i></button>
      <button id="fmt-link" title="link (⌘/Ctrl-K)">link</button>
    </span>
    <button id="save" class="apply" title="write the buffer to disk (⌘/Ctrl-S)">save to file</button>
    <span class="status" id="status">connecting&hellip;</span>
    <button id="term-toggle" class="hidden" title="terminal panel — run your agent (or any shell command) next to the diff; hiding the panel keeps the shell running">terminal</button>
    <button id="theme-toggle" title="theme: auto / light / dark">theme: auto</button>
    <button id="accent-toggle" title="accent color — click to cycle"><span class="swatch"></span><span id="accent-name">blue</span></button>
    <button id="about-btn" title="what is Draftwatch?">about</button>
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
    <p>Read the diff on the right. Click the <code>✗</code> beside any change you
      don't want. <strong>Apply</strong> writes the result to disk — keeping the
      rest, reverting the marked spans. <strong>Commit</strong> records the file
      to git; the baseline advances to that commit, the diff clears to zero, and
      the next agent pass starts clean.</p>
    <h3>The panels</h3>
    <p>Left is your working text: edit the markdown source directly, or switch to
      the rendered <strong>Preview</strong>, which is also editable. Right is the
      diff against the baseline — added words in green, removed words struck in
      red — with a change map, next/previous navigation, and a changes-only view.
      The <strong>terminal</strong> button opens a third panel with a real shell
      (macOS/Linux) — run your agent there and watch its edits land in the diff;
      hiding the panel keeps the shell running.</p>
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
  <div class="gutter" id="gutter-1" title="drag to resize — double-click to reset"></div>
  <section class="panel right">
    <h2 class="diffhead"><span>diff vs baseline</span><span class="diffhint">click <span class="xdemo">✗</span> to revert a change</span></h2>
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
  <div class="gutter term-gutter" id="gutter-2" title="drag to resize — double-click to reset"></div>
  <section class="panel term">
    <h2><span>terminal</span><span class="spacer"></span><button id="term-hide" title="hide the panel — the shell keeps running">hide</button><button id="term-end" title="end the shell session — running programs are killed">end session</button></h2>
    <div id="term-host"></div>
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
  var hasRepo = true;           // false = write-only mode (no git repo in the folder)
  var repoDir = "";             // folder a UI-triggered `git init` would create the repo in
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
      CM.placeholder("No file open — pick one with the “file” control above, or start typing and save (⌘S) to create a new file."),
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
        // a bare ✗ (state carried by the .on highlight + the title text) —
        // the wordy revert/reverted labels made heavily edited lines unreadable
        parts.push(
          '<span class="ctrl" data-hunk="' + h + '">' +
          '<button class="rev' + (reverted ? " on" : "") + '" data-hunk="' + h +
          '" title="' + (reverted ? "reverted — click to keep this change" : "revert this change to the baseline") +
          '">✗</button>' +
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

    // Write-only mode: the folder isn't a git repo, so there is nothing to diff.
    // Explain the trade-off and offer a one-click `git init` (showing the exact
    // folder that would become a repo) to unlock the review loop.
    if (!hasRepo) {
      hunkOrder = []; hunkKind = {}; hunkLine = {}; currentIdx = -1;
      diff.innerHTML =
        '<div class="onboard">' +
        "<p><strong>This folder isn’t a git repository.</strong></p>" +
        "<p>You’re in <strong>write-only mode</strong>: the editor, preview, and " +
        "saving all work — but reviewing and reverting an agent’s edits, which is " +
        "what Draftwatch is for, needs git.</p>" +
        '<code class="repo-path">' + esc(repoDir || "this folder") + "</code>" +
        '<p><button id="init-repo" class="apply">initialize git here</button></p>' +
        '<p class="muted">Runs <code>git init</code> in the folder above. Nothing is ' +
        "committed until you choose to; you can keep writing either way.</p>" +
        '<div class="warn-note muted">Until you do, there’s no baseline to compare ' +
        "against, so an agent’s changes can’t be shown as a diff or reverted here.</div>" +
        "</div>";
      $("counts").textContent = "write-only · no git";
      $("nav").classList.add("hidden");
      buildMinimap();
      return;
    }

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
    } else if (t.id === "init-repo") {
      t.disabled = true;
      setStatus("initializing git…");
      postJSON("/api/init-repo", {}, function (res) {
        if (res.error) {
          t.disabled = false;
          setStatus("git init failed: " + res.error);
          return;
        }
        setStatus("git initialized — reviewing enabled");
        lastBaselineKind = null;
        loadBaselines();          // baselines are meaningful now
        // the server's SSE 'update' (repo:true) re-renders the panel
      });
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
    document.body.classList.add("commit-armed");   // review controls step aside
    $("commit-msg").classList.remove("hidden");
    $("commit-btn").classList.add("armed");
    $("commit-msg").focus();
    setStatus("optional: type a message, then click commit again (Esc to cancel)");
  }
  function disarmCommit() {
    commitArmed = false;
    document.body.classList.remove("commit-armed");
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

  // Save the buffer to disk, from either view (a pending preview edit is
  // flushed into the source buffer first, so what you see is what is saved).
  // With no file open, the buffer — even an empty one — is saved as a NEW
  // file: prompt for a name, create it in the repo, and start watching it.
  function doSave() {
    if (previewMode) flushPreviewSync();
    if (!currentFile) { saveAsNewFile(); return; }
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
  }

  // Prompt for a name and create a new file (via /api/new), then watch it.
  // `text` seeds the file's contents ("" for a fresh empty file). Used by
  // save-with-no-file-open and the picker's "start a new file" option;
  // onDone(false) lets the caller reset UI state after a cancel/failure.
  function createNewFile(text, onDone) {
    var name = window.prompt(
      "Create a new file in this repository.\n\nName (relative to the repo root):",
      "untitled.md");
    if (name == null || !name.trim()) { if (onDone) onDone(false); return; }
    $("save").disabled = true;
    var wasDirty = dirty;
    dirty = false;                    // our own save is not a disk conflict
    reportClientState();
    updateLeftTitle();
    postJSON("/api/new", { path: name.trim(), text: text }, function (res) {
      $("save").disabled = false;
      if (res.error) {
        dirty = wasDirty;
        reportClientState();
        updateLeftTitle();
        setStatus("new file failed: " + res.error);
        if (onDone) onDone(false);
        return;
      }
      currentFile = res.file;
      lastBaselineKind = null;
      currentIdx = -1;
      syncFileControls();
      loadBaselines();
      loadFiles();
      setStatus("created " + res.file);
      // the server's SSE 'update' populates the panels
      if (onDone) onDone(true);
    });
  }

  function saveAsNewFile() {
    createNewFile(editorText());
  }

  // The picker's "start a new file" option: switch to a blank, unnamed
  // scratch buffer and just let you write. No name is asked here — saving
  // (button or ⌘S) prompts for one, via the same no-file path as starting
  // draftwatch without a target. The previously open file stays untouched
  // on disk.
  function startNewFile() {
    if (currentFile === null) {       // already in the scratch buffer
      syncFileControls();
      ed.focus();
      return;
    }
    if (dirty &&
        !window.confirm("You have unsaved edits.\n\nOK = leave them behind and start a new file.\nCancel = stay on the current file.")) {
      syncFileControls();
      return;
    }
    var wasDirty = dirty;
    dirty = false;                    // cleared pre-post so the SSE echo can't trip the banner
    reportClientState();
    updateLeftTitle();
    postJSON("/api/close", {}, function (res) {
      if (res.error) {
        dirty = wasDirty;
        reportClientState();
        updateLeftTitle();
        setStatus("new file failed: " + res.error);
        syncFileControls();
        return;
      }
      currentFile = null;
      lastBaselineKind = null;
      currentIdx = -1;
      syncFileControls();
      loadBaselines();
      setStatus("new file — start typing; save (⌘S) will ask for a name");
      ed.focus();
      // the server's SSE 'update' (file: null) clears the panels
    });
  }

  $("save").addEventListener("click", doSave);

  // ⌘/Ctrl-S saves from anywhere — source view, editable preview, even with
  // focus in the diff panel. preventDefault suppresses the browser's own
  // save-page dialog, which is never what you want here.
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey &&
        (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      doSave();
    }
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
      if (d.repo === false) {           // write-only mode: no baselines to show
        hasRepo = false;
        document.body.classList.add("no-repo");
        $("baseline").innerHTML = "";
        return;
      }
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
      var nf = document.createElement("option");            // create-a-file entry, always on top
      nf.value = "__new__"; nf.textContent = "+ start a new file…"; sel.appendChild(nf);
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
  $("file-select").addEventListener("change", function () {
    if (this.value === "__new__") { startNewFile(); return; }
    if (this.value) openFile(this.value);
  });

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
    if ("repo" in p) hasRepo = p.repo;
    if ("repo_dir" in p) repoDir = p.repo_dir || repoDir;
    document.body.classList.toggle("no-repo", !hasRepo);
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
    if (!currentFile) { setStatus("no file open — pick a file above, or start typing and save"); return; }
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

  // ---- color theme (blue / teal / iris / plum / graphite) ----
  // Same persistence pattern as light/dark: data-accent on <html>, remembered
  // in localStorage, applied pre-paint by the script in <head>. "blue" is the
  // default (attribute removed). Each color restyles the whole surface — bg,
  // panels, borders, ink, accent — and defines a dark-mode variant, so the
  // light/dark toggle keeps working per color. An unknown stored value
  // matches no CSS and currentAccent() maps it back to "blue".
  var ACCENTS = ["blue", "teal", "iris", "plum", "graphite"];
  function currentAccent() {
    var a = document.documentElement.getAttribute("data-accent");
    return ACCENTS.indexOf(a) > 0 ? a : "blue";
  }
  function applyAccent(a) {
    if (a === "blue") document.documentElement.removeAttribute("data-accent");
    else document.documentElement.setAttribute("data-accent", a);
    try {
      if (a === "blue") localStorage.removeItem("draftwatch-accent");
      else localStorage.setItem("draftwatch-accent", a);
    } catch (e) {}
    $("accent-name").textContent = a;
  }
  applyAccent(currentAccent());   // sync the label (and prune a bad stored value)
  $("accent-toggle").addEventListener("click", function () {
    var i = ACCENTS.indexOf(currentAccent());
    applyAccent(ACCENTS[(i + 1) % ACCENTS.length]);
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

  // ---- resizable panels ----
  // The gutters between panels are real grid tracks; dragging one moves width
  // between its two neighbors (as fr fractions, so window resizes scale
  // proportionally). The two- and three-panel layouts each hold their own
  // split for this page load only — deliberately not persisted; every session
  // starts at the defaults. Double-click a gutter to reset.
  var GUTTER_PX = 6;
  var COL_MIN = 200;            // px floor for a panel mid-drag
  var colFr = { two: [1, 1], three: [1, 1, 0.8] };

  function colPanels() {
    var els = [document.querySelector(".panel.left"),
               document.querySelector(".panel.right")];
    if (document.body.classList.contains("term-open"))
      els.push(document.querySelector(".panel.term"));
    return els;
  }

  function applyCols() {
    // inline style overrides the CSS defaults, so this must run on every
    // layout switch (terminal open/hide), not just after drags
    var open = document.body.classList.contains("term-open");
    var fr = open ? colFr.three : colFr.two;
    var parts = [];
    for (var i = 0; i < fr.length; i++) {
      var track = fr[i].toFixed(4) + "fr";
      if (open && i === fr.length - 1) track = "minmax(240px, " + track + ")";
      parts.push(track);
    }
    document.querySelector("main").style.gridTemplateColumns =
      parts.join(" " + GUTTER_PX + "px ");
  }

  function initGutter(id, idx) {
    var g = $(id);
    g.addEventListener("dblclick", function () {
      colFr = { two: [1, 1], three: [1, 1, 0.8] };
      applyCols(); termFitSoon();
    });
    g.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var panels = colPanels();
      if (idx + 1 >= panels.length) return;
      var widths = [], total = 0, i;
      for (i = 0; i < panels.length; i++) widths.push(panels[i].offsetWidth);
      total = widths[idx] + widths[idx + 1];
      if (total <= 0) return;              // no layout yet — nothing to drag
      var startX = e.clientX;
      var mode = panels.length === 3 ? "three" : "two";
      g.classList.add("dragging");
      document.body.classList.add("dragging-cols");
      function move(ev) {
        var w = widths.slice();
        w[idx] = Math.max(COL_MIN, Math.min(widths[idx] + (ev.clientX - startX),
                                            total - COL_MIN));
        w[idx + 1] = total - w[idx];
        var sum = 0, fr = [];
        for (var j = 0; j < w.length; j++) sum += w[j];
        for (var k = 0; k < w.length; k++) fr.push(w[k] / sum * w.length);
        colFr[mode] = fr;
        applyCols();
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        g.classList.remove("dragging");
        document.body.classList.remove("dragging-cols");
        termFitSoon();                     // xterm re-measures its new width
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }
  initGutter("gutter-1", 0);
  initGutter("gutter-2", 1);
  applyCols();

  // ---- terminal panel ----
  // {{TERM}} is injected server-side: "0" when --no-terminal was passed or the
  // platform has no pty (Windows). Disabled means the routes don't exist, so
  // the button never renders and nothing here runs.
  var TERM_ENABLED = "{{TERM}}" === "1" && typeof XTerm !== "undefined";
  var term = null;              // xterm.js Terminal (created on first open)
  var termFit = null;           // fit addon
  var termES = null;            // /term/events EventSource
  var termExited = false;       // shell gone; "end session" becomes "restart"

  function b64ToBytes(b64) {
    var bin = atob(b64), arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }

  function termTheme() {
    // xterm needs concrete colors, not CSS vars; sample the app's theme once.
    var cs = getComputedStyle(document.body);
    return {
      background: (cs.getPropertyValue("--bg") || "").trim() || "#1e1e1e",
      foreground: (cs.getPropertyValue("--text") || "").trim() || "#dddddd",
      cursor: (cs.getPropertyValue("--accent") || "").trim() || "#6699ff",
      selectionBackground: "rgba(128,128,128,0.35)"
    };
  }

  function termButtons() {
    $("term-end").textContent = termExited ? "restart" : "end session";
  }

  function termFitSoon() {
    // fit after layout settles; xterm's resize event then informs the server
    requestAnimationFrame(function () {
      if (termFit && document.body.classList.contains("term-open")) {
        try { termFit.fit(); } catch (e) {}
      }
    });
  }

  function connectTermES() {
    if (termES) termES.close();
    // EventSource cannot set headers; the token rides in the query string here
    // (read-only output stream) — never on the input routes, which are
    // header-only server-side.
    termES = new EventSource("/term/events?t=" + encodeURIComponent(TOKEN));
    termES.onmessage = function (ev) {
      var p;
      try { p = JSON.parse(ev.data); } catch (e) { return; }
      if (p.type === "hello") {
        // (re)connect: repaint from scrollback so reloads lose nothing
        term.reset();
        if (p.data) term.write(b64ToBytes(p.data));
        termExited = !p.running;
        termButtons();
      } else if (p.type === "out") {
        term.write(b64ToBytes(p.data));
      } else if (p.type === "exit") {
        termExited = true;
        termButtons();
        term.write("\r\n\x1b[2m[shell exited" +
                   (p.code !== null && p.code !== undefined ? " (" + p.code + ")" : "") +
                   " — restart from the panel header]\x1b[0m\r\n");
      }
    };
  }

  function termStart() {
    postJSON("/api/term/open", { cols: term.cols, rows: term.rows }, function (res) {
      if (res.error) { setStatus("terminal: " + res.error); return; }
      termExited = !res.running;
      termButtons();
      if (res.started && termES) term.reset();   // fresh shell, fresh screen
      if (!termES) connectTermES();
      term.focus();
    });
  }

  function initTerm() {
    term = new XTerm.Terminal({
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13,
      cursorBlink: true,
      scrollback: 4000,
      theme: termTheme()
    });
    termFit = new XTerm.FitAddon();
    term.loadAddon(termFit);
    term.open($("term-host"));
    try { termFit.fit(); } catch (e) {}
    // raw keystrokes -> PTY; the server never parses them (and the token goes
    // in a header, so input never appears in a URL)
    term.onData(function (d) { postJSON("/api/term/input", { data: d }); });
    term.onResize(function (sz) {
      postJSON("/api/term/resize", { cols: sz.cols, rows: sz.rows });
    });
    termStart();
  }

  function openTermPanel() {
    document.body.classList.add("term-open");
    applyCols();                                   // three-panel split
    if (!term) initTerm();
    else { termFitSoon(); if (termExited) termStart(); else term.focus(); }
  }
  function hideTermPanel() {
    document.body.classList.remove("term-open");   // shell keeps running
    applyCols();                                   // back to the two-panel split
  }

  if (TERM_ENABLED) {
    $("term-toggle").classList.remove("hidden");
    $("term-toggle").addEventListener("click", function () {
      if (document.body.classList.contains("term-open")) hideTermPanel();
      else openTermPanel();
    });
    $("term-hide").addEventListener("click", hideTermPanel);
    $("term-end").addEventListener("click", function () {
      if (termExited) { termStart(); return; }
      if (!window.confirm("End the shell session? Running programs will be killed.")) return;
      postJSON("/api/term/close", {}, function () {
        termExited = true;
        termButtons();
      });
    });
    window.addEventListener("resize", termFitSoon);
  }

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


def bind_http_server(port, handler, explicit_port):
    """Create the HTTP server, returning a bound ThreadingHTTPServer.

    Always binds loopback (127.0.0.1): Draftwatch is a local, single-user tool
    and is never exposed on the network — there is deliberately no --host
    option (the security model, and the embedded terminal especially, depend
    on it).

    With an explicit --port, bind exactly that port (the caller reports a clean
    error if it is busy). Otherwise treat `port` as the *preferred* default:
    if it is already in use — the "Address already in use" case that stopped a
    second instance from starting — scan the next few ports and finally fall
    back to an OS-assigned free port (port 0). The real port is read from the
    bound socket afterward, so callers must not assume `port`.
    """
    host = "127.0.0.1"
    if explicit_port:
        # honor the user's choice; let EADDRINUSE propagate to the caller
        return ThreadingHTTPServer((host, port), handler)
    candidates = [port] + [port + i for i in range(1, 16)] + [0]
    last_err = None
    for cand in candidates:
        try:
            return ThreadingHTTPServer((host, cand), handler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                last_err = e
                continue
            raise
    # candidates ends with port 0, which effectively never collides, so we
    # only get here on an unusual persistent failure
    raise last_err


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
    parser.add_argument(
        "--port", type=int, default=None,
        help="port (default {}). If omitted and the default is busy — e.g. a "
             "second instance while the first is running — Draftwatch picks a "
             "free port automatically. Pass --port to pin an exact one."
             .format(DEFAULT_PORT))
    parser.add_argument(
        "--no-open", action="store_true",
        help="do not auto-open the browser on startup (it opens by default).",
    )
    parser.add_argument(
        "--no-terminal", action="store_true",
        help="disable the embedded terminal panel entirely (its routes are "
             "absent from the server, not just hidden in the UI).",
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

    # --- resolve the root directory (git repo if there is one) ---
    # With a target: from the target's directory. Without one: from the current dir.
    if args.target is not None:
        if not os.path.exists(args.target):
            fail("target file does not exist: %s" % args.target)
        if os.path.isdir(args.target):
            fail("target is a directory, not a file: %s" % args.target)
        base_dir = os.path.dirname(os.path.realpath(os.path.abspath(args.target)))
    else:
        base_dir = os.getcwd()

    # A git repo unlocks the whole review loop (diffs, baselines, revert, commit).
    # Without one, Draftwatch no longer refuses to start — it opens in write-only
    # mode (edit/preview/save work; review is off) and the UI offers a one-click
    # `git init` to turn the folder into a repo. This keeps the "write a new
    # document from scratch" path usable outside any repository.
    has_repo = is_inside_work_tree(base_dir)
    if has_repo:
        root = git_toplevel(base_dir)
        if root is None:
            fail("could not resolve the git repository root for: %s" % base_dir)
    else:
        root = os.path.realpath(os.path.abspath(base_dir))

    state = State(root, has_repo=has_repo)

    if has_repo:
        init_note = "no file open — choose one in the browser"
    else:
        init_note = "no git repository in {} — write-only mode (init from the UI to review)".format(root)
    if args.target is not None:
        try:
            state.open_file(os.path.abspath(args.target))
        except ValueError as e:
            fail(str(e))
        if has_repo:
            init_note = "baseline: " + state.baseline.get("label", "HEAD")

    # --- HTTP server ---
    # per-session token: gates every /api/* and /events request (see Handler._guard)
    token = secrets.token_urlsafe(32)
    Handler.state = state
    Handler.token = token
    # Embedded terminal: on when the platform supports it and the user didn't
    # opt out. The session is created lazily-ish here but the shell is only
    # spawned when the panel is first opened. atexit covers every exit path
    # (ctrl-c, window close, normal return) so no shell outlives Draftwatch.
    Handler.term_enabled = TERM_SUPPORTED and not args.no_terminal
    if Handler.term_enabled:
        Handler.term_session = _term.TermSession(root)
        atexit.register(Handler.term_session.terminate)
    explicit_port = args.port is not None
    preferred_port = args.port if explicit_port else DEFAULT_PORT
    try:
        httpd = bind_http_server(preferred_port, Handler, explicit_port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            fail("could not bind 127.0.0.1:%d (%s). Another Draftwatch is likely "
                 "running; omit --port to let it pick a free port automatically."
                 % (preferred_port, e), code=1)
        fail("could not bind 127.0.0.1:%d (%s)" % (preferred_port, e), code=1)
    httpd.daemon_threads = True
    # Bind may have landed on a different port than requested (auto-fallback).
    # Everything downstream — the opened URL and the Host allowlist — must use
    # the ACTUAL bound port, or the page would load on one port while the
    # server rejected its requests as a bad Host.
    bound_port = httpd.server_address[1]
    Handler.allowed_hosts = {"127.0.0.1:%d" % bound_port, "localhost:%d" % bound_port}

    stop_event = threading.Event()
    watcher = threading.Thread(target=watch_loop, args=(state, stop_event), daemon=True)
    watcher.start()

    # The token rides in the URL so the page can authenticate its requests.
    open_url = "http://127.0.0.1:{}/?t={}".format(bound_port, token)

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
    if not explicit_port and bound_port != DEFAULT_PORT:
        print("port {} was busy — using {} instead".format(DEFAULT_PORT, bound_port))
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
