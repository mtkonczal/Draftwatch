#!/usr/bin/env python3
"""End-to-end acceptance tests for draftwatch (spec section 11).

Drives a real running server over localhost with stdlib urllib, reads the SSE
stream, exercises every API route, and checks the round-trip invariants against
real git. Records ACTUAL observed output.
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# run the installed-style entry point (`python -m draftwatch`) against the
# package in this repo, from any cwd
WT_CMD = [sys.executable, "-m", "draftwatch"]
WT_ENV = dict(os.environ, PYTHONPATH=REPO_ROOT + os.pathsep +
              os.environ.get("PYTHONPATH", ""))
RESULTS = []

# session token of the currently running Server (set in Server.__enter__ by
# parsing the tokenized URL draftwatch prints to stdout — so the tests
# exercise the real token path end to end)
TOKEN = None


def record(num, name, ok, detail=""):
    RESULTS.append((num, name, ok, detail))
    print("[{}] test {}: {}".format("PASS" if ok else "FAIL", num, name))
    if detail:
        for line in detail.splitlines():
            print("    " + line)


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def git_out(args, cwd):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return p.stdout


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_repo(content):
    d = tempfile.mkdtemp(prefix="wt-acc-")
    sh(["git", "init", "-q"], d)
    sh(["git", "config", "user.email", "t@t.com"], d)
    sh(["git", "config", "user.name", "t"], d)
    f = os.path.join(d, "draft.md")
    with open(f, "w", newline="") as fh:
        fh.write(content)
    sh(["git", "add", "draft.md"], d)
    sh(["git", "commit", "-qm", "init"], d)
    return d, f


def post(port, path, obj, token=None, host=None):
    data = json.dumps(obj).encode()
    headers = {"Content-Type": "application/json"}
    tok = token if token is not None else TOKEN
    if tok:
        headers["X-Draftwatch-Token"] = tok
    if host:
        headers["Host"] = host
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # the API returns JSON error bodies on 4xx/5xx — surface them, don't raise
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"error": "HTTP %d" % e.code}
        body["_code"] = e.code
        return body


def get(port, path, token=None):
    headers = {}
    tok = token if token is not None else TOKEN
    if tok:
        headers["X-Draftwatch-Token"] = tok
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


class SSEReader(threading.Thread):
    """Collect SSE JSON events into a list."""
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.events = []
        self._stop = False

    def run(self):
        try:
            url = "http://127.0.0.1:%d/events" % self.port
            if TOKEN:
                url += "?t=" + TOKEN     # EventSource-style query token
            with urllib.request.urlopen(url, timeout=30) as r:
                buf = ""
                while not self._stop:
                    line = r.readline()
                    if not line:
                        break
                    line = line.decode("utf-8")
                    if line.startswith("data: "):
                        try:
                            self.events.append(json.loads(line[6:].strip()))
                        except Exception:
                            pass
        except Exception:
            pass

    def stop(self):
        self._stop = True


class Server:
    def __init__(self, repo, target, port, extra=None, env=None):
        self.repo = repo
        self.target = target
        self.port = port
        self.extra = extra or []
        self.env = env or WT_ENV
        self.proc = None
        self.stdout = ""

    def __enter__(self):
        cmd = list(WT_CMD)
        if self.target is not None:
            cmd.append(self.target)
        cmd += ["--port", str(self.port), "--no-open"] + self.extra
        self.proc = subprocess.Popen(
            cmd, cwd=self.repo, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # wait for the port
        for _ in range(100):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
                s.close()
                break
            except OSError:
                time.sleep(0.05)
        # parse the session token from the tokenized URL printed at startup
        global TOKEN
        TOKEN = None
        deadline = time.time() + 5
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            m = re.search(r"[?&]t=([A-Za-z0-9_\-]+)", line)
            if m:
                TOKEN = m.group(1)
                break
        time.sleep(0.2)
        return self

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                # read() on the buffered stream, not communicate(timeout=...):
                # communicate with a timeout bypasses the text wrapper that the
                # __enter__ readline loop already used, silently dropping output
                out = self.proc.stdout.read() if self.proc.stdout else ""
                self.proc.wait(timeout=5)
                self.stdout = out
            except Exception:
                self.proc.kill()


def latest_diff_event(reader):
    """Return the most recent event carrying segments."""
    for e in reversed(reader.events):
        if "segments" in e:
            return e
    return None


# --------------------------------------------------------------------------- #

def test_1_not_a_repo():
    # New contract (write-only mode): starting outside a git repo no longer
    # fails. The server comes up, reports repo:false with no diff, and offers a
    # one-click `git init` via /api/init-repo that flips it into review mode.
    d = tempfile.mkdtemp(prefix="wt-norepo-")
    port = free_port()
    try:
        f = os.path.join(d, "draft.md")
        with open(f, "w") as fh:
            fh.write("hi\n")
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            files = json.loads(get(port, "/api/files"))
            ev0 = latest_diff_event(reader)
            write_only_ok = (files.get("repo") is False
                             and "draft.md" in files.get("files", [])
                             and ev0 is not None and ev0.get("repo") is False
                             and ev0.get("segments") == [] and ev0.get("baseline") is None)
            # baseline/commit are blocked until git exists
            blocked = post(port, "/api/commit", {"message": "x"})
            blocked_ok = ("error" in blocked and "write-only" in blocked["error"])
            # initialize git in place, then confirm we flip into repo mode
            init = post(port, "/api/init-repo", {})
            time.sleep(0.6)
            evN = latest_diff_event(reader)
            reader.stop()
            init_ok = (init.get("ok") is True
                       and os.path.isdir(os.path.join(d, ".git"))
                       and evN is not None and evN.get("repo") is True)
            no_trace = "Traceback" not in (srv.stdout or "")
        ok = write_only_ok and blocked_ok and init_ok and no_trace
        record(1, "no repo -> write-only mode, then /api/init-repo enables review", ok,
               "write_only={}  commit_blocked={}  init->repo={}  no_traceback={}".format(
                   write_only_ok, blocked_ok, init_ok, no_trace))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_2_clean_start():
    d, f = make_repo("alpha\nbeta\ngamma\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            home = get(port, "/")
            ev = latest_diff_event(reader)
            reader.stop()
            ok_home = "<title>Draftwatch</title>" in home
            ok_empty = ev is not None and ev["segments"] == [] and ev["hunks"] == []
            record(2, "clean start: server up, URL reachable, empty diff",
                   ok_home and ok_empty,
                   "home_ok={}  initial segments={}  hunks={}".format(
                       ok_home, ev and len(ev["segments"]), ev and ev["hunks"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_3_agent_edit():
    d, f = make_repo("the quick brown fox\njumps over\nthe lazy dog\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.5)
            n_before = len(reader.events)
            t0 = time.time()
            with open(f, "w", newline="") as fh:
                fh.write("the quick red fox\njumps over\nthe lazy dog\n")
            # wait for an update event
            ev = None
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and any(s["type"] in ("add", "del") for s in e["segments"]):
                    ev = e
                    break
                time.sleep(0.05)
            latency = time.time() - t0
            reader.stop()
            has_add = ev is not None and any(s["type"] == "add" for s in ev["segments"])
            has_del = ev is not None and any(s["type"] == "del" for s in ev["segments"])
            ok = has_add and has_del and latency < 2.0
            record(3, "agent edit: panels update within ~1s, green/del present", ok,
                   "latency={:.2f}s  add={}  del={}".format(latency, has_add, has_del))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_4_accept_all():
    base = "the quick brown fox\njumps over\nthe lazy dog\n"
    work = "the quick red fox\njumps over\nthe sleepy dog\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write(work)
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader); reader.stop()
            decisions = {str(h): "accept" for h in ev["hunks"]}
            res = post(port, "/api/apply",
                       {"decisions": decisions, "diff_epoch": ev["diff_epoch"]})
            with open(f, newline="") as fh:
                written = fh.read()
            ok = (written == work)
            record(4, "round-trip accept-all == working file", ok,
                   "hunks={}  applied_ok={}  equal={}".format(
                       ev["hunks"], res.get("ok"), ok))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_5_reject_all():
    base = "the quick brown fox\njumps over\nthe lazy dog\n"
    work = "the quick red fox\njumps over\nthe sleepy dog\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write(work)
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader); reader.stop()
            decisions = {str(h): "reject" for h in ev["hunks"]}
            post(port, "/api/apply",
                 {"decisions": decisions, "diff_epoch": ev["diff_epoch"]})
            with open(f, newline="") as fh:
                written = fh.read()
            head_blob = git_out(["show", "HEAD:draft.md"], d)
            ok = (written == head_blob == base)
            record(5, "round-trip reject-all == baseline (git show HEAD:draft.md)", ok,
                   "equal_to_HEAD={}".format(written == head_blob))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_6_mixed():
    # Two changes separated by unchanged context. Decisions are mapped to hunks by
    # inspecting segment content (robust to whatever alignment git chooses).
    base = "keep_top\nAAAA\nkeep_mid\nBBBB\nkeep_bot\n"
    work = "keep_top\nZZZZ\nkeep_mid\nYYYY\nkeep_bot\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write(work)
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader); reader.stop()
            # accept the change that introduces ZZZZ; reject the change touching BBBB
            accept_hunk = next(s["hunk"] for s in ev["segments"]
                               if s["type"] == "add" and "ZZZZ" in s["text"])
            reject_hunk = next(s["hunk"] for s in ev["segments"]
                               if s["type"] == "del" and "BBBB" in s["text"])
            decisions = {str(accept_hunk): "accept", str(reject_hunk): "reject"}
            post(port, "/api/apply",
                 {"decisions": decisions, "diff_epoch": ev["diff_epoch"]})
            with open(f, newline="") as fh:
                written = fh.read()
            ok = ("ZZZZ" in written and "BBBB" in written
                  and "AAAA" not in written and "YYYY" not in written)
            record(6, "mixed decisions: accepted span kept new, rejected span restored old",
                   ok, "accept_hunk={} reject_hunk={}  result={!r}".format(
                       accept_hunk, reject_hunk, written))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_7_edit_conflict():
    d, f = make_repo("alpha\nbeta\ngamma\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.5)
            # simulate the client having a dirty buffer
            post(port, "/api/client_state", {"dirty": True})
            time.sleep(0.2)
            n_before = len(reader.events)
            with open(f, "w", newline="") as fh:
                fh.write("alpha\nBETA changed on disk\ngamma\n")
            # wait for the event
            disk_changed = False
            t0 = time.time()
            while time.time() - t0 < 3:
                for e in reader.events[n_before:]:
                    if e.get("type") == "disk_changed":
                        disk_changed = True
                if disk_changed:
                    break
                time.sleep(0.05)
            reader.stop()
            # the server never wrote the buffer; the file on disk is the agent's
            # version, and the contract is signalled by the disk_changed event type.
            record(7, "edit conflict: disk_changed event (buffer not clobbered)",
                   disk_changed,
                   "received disk_changed event = {}".format(disk_changed))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_8_commit_list():
    d, f = make_repo("v1\n")
    port = free_port()
    try:
        # add a couple more commits
        with open(f, "w", newline="") as fh:
            fh.write("v2\n")
        sh(["git", "commit", "-qam", "second"], d)
        with open(f, "w", newline="") as fh:
            fh.write("v3\n")
        sh(["git", "commit", "-qam", "third"], d)
        with Server(d, "draft.md", port) as srv:
            time.sleep(0.4)
            data = json.loads(get(port, "/api/baselines"))
            commits = data.get("commits", [])
            ok = len(commits) >= 3 and all("ref" in c and "label" in c for c in commits)
            # readable relative date present in label (e.g. "seconds ago"/"minutes ago")
            has_reldate = any("ago" in c["label"] or "second" in c["label"]
                              for c in commits)
            record(8, "commit list: dropdown lists recent commits w/ relative dates",
                   ok and has_reldate,
                   "n_commits={}  sample_label={!r}".format(
                       len(commits), commits[0]["label"] if commits else None))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_9_no_file_start():
    # start with NO target: server runs, empty state, picker can list files
    d, f = make_repo("alpha\nbeta\n")
    port = free_port()
    try:
        with Server(d, None, port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader); reader.stop()
            files = json.loads(get(port, "/api/files"))
            ok = (ev is not None and ev.get("file") is None and ev["segments"] == []
                  and "draft.md" in (files.get("files") or []))
            record(9, "no-file start: empty state + /api/files lists the repo", ok,
                   "initial file={!r}  files={}".format(
                       ev and ev.get("file"), files.get("files")))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_10_open_switch():
    # open a file at runtime via /api/open and confirm the diff follows it
    d, f = make_repo("alpha\nbeta\n")
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write("alpha\nBETA\n")
        with Server(d, None, port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.5)
            res = post(port, "/api/open", {"path": "draft.md"})
            time.sleep(0.6)
            ev = latest_diff_event(reader); reader.stop()
            has_change = ev is not None and any(s["type"] in ("add", "del")
                                                for s in ev["segments"])
            ok = (res.get("ok") and ev.get("file") == "draft.md" and has_change)
            record(10, "open file at runtime: /api/open switches the watched file", ok,
                   "open_ok={}  file={!r}  change={}".format(
                       res.get("ok"), ev and ev.get("file"), has_change))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_11_open_outside_repo_refused():
    d, f = make_repo("x\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            time.sleep(0.4)
            res1 = post(port, "/api/open", {"path": "/etc/hosts"})           # absolute, outside
            res2 = post(port, "/api/open", {"path": "../../../../etc/hosts"})  # traversal
            ok = ("error" in res1 and "error" in res2)
            record(11, "open outside repo refused (absolute + traversal)", ok,
                   "abs={!r}  traversal={!r}".format(res1.get("error"), res2.get("error")))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_12_no_open_flag():
    # --no-open is honored (passed by the Server helper); server still serves
    d, f = make_repo("a\nb\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            time.sleep(0.4)
            home = get(port, "/")
            ok = "<title>Draftwatch</title>" in home
            record(12, "--no-open: server starts and serves without launching a browser",
                   ok, "home_ok={}".format(ok))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_13_content_addressed_ids():
    # A hunk's id is a content hash of the change plus a ~32-char window of
    # surrounding text, so it must survive an agent save that edits elsewhere
    # (outside the window), and a decision made against it must still target
    # the same content. The padding lines keep the two edits farther apart
    # than the hash window.
    pad = ("lorem ipsum dolor sit amet consectetur\n"
           "adipiscing elit sed do eiusmod tempor\n")
    base = "one AAA\n" + pad + "two BBB\nthree CCC\n"
    work1 = "one ZZZ\n" + pad + "two BBB\nthree CCC\n"
    work2 = "one ZZZ\n" + pad + "two YYY\nthree CCC\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.5)
            # first agent edit: AAA -> ZZZ
            with open(f, "w", newline="") as fh:
                fh.write(work1)
            ev = None
            t0 = time.time()
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and any(s["type"] == "del" and "AAA" in s["text"]
                             for s in e["segments"]):
                    ev = e
                    break
                time.sleep(0.05)
            h = next(s["hunk"] for s in ev["segments"]
                     if s["type"] == "del" and "AAA" in s["text"])
            # unrelated second edit elsewhere in the file: BBB -> YYY
            with open(f, "w", newline="") as fh:
                fh.write(work2)
            ev2 = None
            t0 = time.time()
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and any(s["type"] == "add" and "YYY" in s["text"]
                             for s in e["segments"]):
                    ev2 = e
                    break
                time.sleep(0.05)
            reader.stop()
            survived = ev2 is not None and h in ev2["hunks"]
            # the surviving id must still revert the same span (AAA restored,
            # the unrelated YYY edit untouched)
            post(port, "/api/apply", {"decisions": {str(h): "reject"},
                                      "diff_epoch": ev2["diff_epoch"]})
            with open(f, newline="") as fh:
                written = fh.read()
            expected = "one AAA\n" + pad + "two YYY\nthree CCC\n"
            ok = survived and written == expected
            record(13, "content-addressed ids: decision survives unrelated edit", ok,
                   "id={!r} survived={} result={!r}".format(h, survived, written))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_14_stale_epoch_rejected():
    # /api/apply must refuse decisions rendered against an out-of-date diff
    # (wrong-span reverts otherwise) and succeed with a fresh epoch.
    base = "the quick brown fox\njumps over\nthe lazy dog\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write("the quick red fox\njumps over\nthe lazy dog\n")
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader)
            stale_epoch = ev["diff_epoch"]
            # the file changes after that render (no need to wait for SSE — the
            # server recomputes the epoch from disk at apply time)
            with open(f, "w", newline="") as fh:
                fh.write("the quick red fox\njumps over\nthe sleepy dog\n")
            res_stale = post(port, "/api/apply",
                             {"decisions": {str(ev["hunks"][0]): "reject"},
                              "diff_epoch": stale_epoch})
            stale_rejected = bool(res_stale.get("stale")) and "error" in res_stale
            # wait for the refreshed payload, then apply with the fresh epoch
            fresh = None
            t0 = time.time()
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and e.get("diff_epoch") not in (None, stale_epoch):
                    fresh = e
                    break
                time.sleep(0.05)
            reader.stop()
            res_fresh = post(port, "/api/apply",
                             {"decisions": {str(h): "reject" for h in fresh["hunks"]},
                              "diff_epoch": fresh["diff_epoch"]})
            with open(f, newline="") as fh:
                written = fh.read()
            ok = stale_rejected and res_fresh.get("ok") and written == base
            record(14, "stale diff_epoch rejected; fresh epoch applies", ok,
                   "stale_rejected={} fresh_ok={} reverted_to_base={}".format(
                       stale_rejected, res_fresh.get("ok"), written == base))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_15_token_and_host_validation():
    # /api/* and /events require the per-session token; Host must be loopback.
    d, f = make_repo("x\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            time.sleep(0.3)
            # no token -> 403 on a state-changing API
            res_no = post(port, "/api/save", {"text": "pwned\n"}, token="")
            no_tok_403 = res_no.get("_code") == 403
            # no token -> 403 on /events
            try:
                get(port, "/events", token="")
                events_403 = False
            except urllib.error.HTTPError as e:
                events_403 = (e.code == 403)
            # valid token but bad Host (DNS-rebinding shape) -> 403
            res_host = post(port, "/api/save", {"text": "pwned\n"},
                            host="evil.example:%d" % port)
            bad_host_403 = res_host.get("_code") == 403
            # file untouched by the rejected writes
            with open(f, newline="") as fh:
                untouched = fh.read() == "x\n"
            # normal tokenized flow still green
            res_ok = post(port, "/api/client_state", {"dirty": False})
            ok = (no_tok_403 and events_403 and bad_host_403
                  and untouched and res_ok.get("ok") is True)
            record(15, "session token + Host validation (403s; normal flow green)", ok,
                   "no_token_403={} events_403={} bad_host_403={} untouched={} ok_flow={}".format(
                       no_tok_403, events_403, bad_host_403, untouched, res_ok.get("ok")))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_16_commit_advances_baseline():
    # commit + baseline auto-advance to HEAD -> diff clears to zero
    base = "alpha\nbeta\ngamma\n"
    d, f = make_repo(base)
    port = free_port()
    try:
        with open(f, "w", newline="") as fh:
            fh.write("alpha\nBETA improved\ngamma\n")
        with Server(d, "draft.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader)
            had_hunks = ev is not None and len(ev["hunks"]) > 0
            res = post(port, "/api/commit", {"message": "reviewed: beta improved"})
            # wait for the baseline_changed payload with an empty diff
            cleared = None
            t0 = time.time()
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and e.get("type") == "baseline_changed":
                    cleared = e
                    break
                time.sleep(0.05)
            reader.stop()
            head_now = git_out(["show", "HEAD:draft.md"], d)
            n_commits = git_out(["rev-list", "--count", "HEAD"], d).strip()
            ok = (had_hunks and res.get("ok") is True and bool(res.get("short"))
                  and cleared is not None
                  and cleared["baseline"]["kind"] == "head"
                  and cleared["segments"] == [] and cleared["hunks"] == []
                  and head_now == "alpha\nBETA improved\ngamma\n"
                  and n_commits == "2")
            record(16, "commit: baseline -> HEAD + empty diff", ok,
                   "commit_ok={} short={!r} baseline={} hunks_after={} commits={}".format(
                       res.get("ok"), res.get("short"),
                       cleared and cleared["baseline"], cleared and cleared["hunks"],
                       n_commits))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_17_start_tracking_untracked():
    # open an untracked file -> baseline is the empty tree (the UI shows the
    # start-tracking prompt); the commit endpoint makes the first commit,
    # flips the baseline to HEAD, and the diff is empty.
    d, f = make_repo("committed other file\n")   # gives the repo a HEAD
    port = free_port()
    try:
        novel = os.path.join(d, "novel.md")
        with open(novel, "w", newline="") as fh:
            fh.write("chapter one\nit was a dark and stormy night\n")
        with Server(d, "novel.md", port) as srv:
            reader = SSEReader(port); reader.start(); time.sleep(0.6)
            ev = latest_diff_event(reader)
            was_empty_baseline = (ev is not None
                                  and ev["baseline"]["kind"] == "empty")
            res = post(port, "/api/commit", {"message": "start tracking: novel.md"})
            done = None
            t0 = time.time()
            while time.time() - t0 < 3:
                e = latest_diff_event(reader)
                if e and e.get("type") == "baseline_changed":
                    done = e
                    break
                time.sleep(0.05)
            reader.stop()
            tracked = "novel.md" in git_out(["ls-files"], d)
            ok = (was_empty_baseline and res.get("ok") is True
                  and done is not None
                  and done["baseline"]["kind"] == "head"
                  and done["hunks"] == [] and tracked)
            record(17, "untracked file: start tracking -> tracked, baseline HEAD, zero hunks",
                   ok,
                   "empty_baseline_before={} commit_ok={} baseline_after={} hunks={} tracked={}".format(
                       was_empty_baseline, res.get("ok"),
                       done and done["baseline"], done and done["hunks"], tracked))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_18_static_assets():
    # vendored assets serve 200 with sane sizes; anything off-allowlist is 404
    d, f = make_repo("x\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port) as srv:
            time.sleep(0.3)
            sizes = {}
            for name in ("codemirror.js", "marked.js", "purify.js"):
                body = get(port, "/static/" + name, token="")   # token-exempt
                sizes[name] = len(body)
            ok_sizes = (sizes["codemirror.js"] > 100000
                        and sizes["marked.js"] > 10000
                        and sizes["purify.js"] > 10000)
            codes = []
            for bad in ("/static/nope.js", "/static/../app.py",
                        "/static/codemirror.js.bak", "/static/"):
                try:
                    get(port, bad, token="")
                    codes.append(200)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
            ok_404 = all(c == 404 for c in codes)
            record(18, "vendored /static assets: allowlist 200, unknown 404",
                   ok_sizes and ok_404,
                   "sizes={} bad_request_codes={}".format(sizes, codes))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_19_app_flag_fallback():
    # --app without pywebview must not crash: friendly message + the server
    # keeps serving in browser mode. A stub webview module that raises
    # ImportError makes this deterministic (never opens a real window in CI).
    d, f = make_repo("x\n")
    stub = tempfile.mkdtemp(prefix="wt-stub-")
    port = free_port()
    try:
        with open(os.path.join(stub, "webview.py"), "w") as fh:
            fh.write("raise ImportError('stub: pywebview not installed')\n")
        env = dict(WT_ENV)
        env["PYTHONPATH"] = stub + os.pathsep + env.get("PYTHONPATH", "")
        with Server(d, "draft.md", port, extra=["--app"], env=env) as srv:
            time.sleep(0.5)
            home = get(port, "/")
            ok_home = "<title>Draftwatch</title>" in home
            res = post(port, "/api/client_state", {"dirty": False})
        fell_back = "falling back to the browser" in srv.stdout
        ok = ok_home and res.get("ok") is True and fell_back
        record(19, "--app without pywebview: friendly fallback, server serves", ok,
               "home_ok={} api_ok={} fallback_msg={}".format(
                   ok_home, res.get("ok"), fell_back))
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(stub, ignore_errors=True)


def test_20_terminal_roundtrip_and_guards():
    # Embedded terminal (POSIX): open spawns a shell, input round-trips through
    # the PTY to the SSE output stream, close kills the whole process group.
    # Security: no token -> 403, and the input route is header-only — a
    # query-string token (fine for EventSource reads) must NOT authorize it.
    if os.name != "posix":
        record(20, "terminal round-trip (skipped: not POSIX)", True)
        return
    d, f = make_repo("hello\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port):
            # guards first
            res_no_tok = post(port, "/api/term/open", {}, token="")
            no_tok_403 = res_no_tok.get("_code") == 403
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/term/input?t=%s" % (port, TOKEN),
                data=json.dumps({"data": "x"}).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                query_tok_403 = False
            except urllib.error.HTTPError as e:
                query_tok_403 = (e.code == 403)

            # open + SSE round-trip
            res = post(port, "/api/term/open", {"cols": 100, "rows": 30})
            opened = res.get("running") is True and res.get("pid")
            pid = res.get("pid")
            out = bytearray()
            done = threading.Event()

            def read_term_sse():
                r = urllib.request.Request(
                    "http://127.0.0.1:%d/term/events?t=%s" % (port, TOKEN))
                try:
                    with urllib.request.urlopen(r, timeout=15) as resp:
                        while not done.is_set():
                            line = resp.readline()
                            if not line:
                                break
                            if line.startswith(b"data: "):
                                ev = json.loads(line[6:])
                                if ev.get("data"):
                                    out.extend(base64.b64decode(ev["data"]))
                                if b"term_acc_42" in out:
                                    done.set()
                except Exception:
                    pass

            t = threading.Thread(target=read_term_sse, daemon=True)
            t.start()
            time.sleep(0.5)
            res_in = post(port, "/api/term/input",
                          {"data": "echo term_acc_$((40+2))\r"})
            done.wait(timeout=8)
            roundtrip = b"term_acc_42" in bytes(out)
            res_rs = post(port, "/api/term/resize", {"cols": 120, "rows": 40})

            # close kills the process group — the shell must be gone
            res_cl = post(port, "/api/term/close", {})
            killed = None
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    killed = True
                    break
            ok = (no_tok_403 and query_tok_403 and bool(opened)
                  and res_in.get("ok") is True and roundtrip
                  and res_rs.get("ok") is True
                  and res_cl.get("running") is False and killed is True)
            record(20, "terminal: PTY round-trip, header-only token, group kill", ok,
                   "no_tok_403={} query_tok_403={} opened={} roundtrip={} "
                   "resize={} closed={} killed={}".format(
                       no_tok_403, query_tok_403, bool(opened), roundtrip,
                       res_rs.get("ok"), res_cl.get("running") is False, killed))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_21_no_terminal_flag():
    # --no-terminal: the routes are absent (404, not 403), and the served page
    # carries TERM=0 so the UI never renders the button. This is also the
    # code path Windows takes (no pty module -> feature off).
    d, f = make_repo("hello\n")
    port = free_port()
    try:
        with Server(d, "draft.md", port, extra=["--no-terminal"]):
            codes = []
            for path in ("/api/term/open", "/api/term/input",
                         "/api/term/resize", "/api/term/close"):
                res = post(port, path, {"data": "x", "cols": 80, "rows": 24})
                codes.append(res.get("_code"))
            try:
                get(port, "/term/events?t=" + TOKEN)
                sse_code = 200
            except urllib.error.HTTPError as e:
                sse_code = e.code
            page = get(port, "/")
            flag_off = 'TERM_ENABLED = "0"' in page
            ok = all(c == 404 for c in codes) and sse_code == 404 and flag_off
            record(21, "--no-terminal: routes 404, page flag off", ok,
                   "codes={} sse={} flag_off={}".format(codes, sse_code, flag_off))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=== draftwatch acceptance tests (git %s, py %s) ===" % (
        git_out(["--version"], ".").strip(),
        sys.version.split()[0]))
    test_1_not_a_repo()
    test_2_clean_start()
    test_3_agent_edit()
    test_4_accept_all()
    test_5_reject_all()
    test_6_mixed()
    test_7_edit_conflict()
    test_8_commit_list()
    test_9_no_file_start()
    test_10_open_switch()
    test_11_open_outside_repo_refused()
    test_12_no_open_flag()
    test_13_content_addressed_ids()
    test_14_stale_epoch_rejected()
    test_15_token_and_host_validation()
    test_16_commit_advances_baseline()
    test_17_start_tracking_untracked()
    test_18_static_assets()
    test_19_app_flag_fallback()
    test_20_terminal_roundtrip_and_guards()
    test_21_no_terminal_flag()
    npass = sum(1 for r in RESULTS if r[2])
    nfail = len(RESULTS) - npass
    print("\n=== %d passed, %d failed ===" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())

