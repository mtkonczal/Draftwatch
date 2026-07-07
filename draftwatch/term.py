"""PTY-backed terminal session for the embedded terminal panel.

POSIX only: importing this module raises ImportError on Windows (no `pty`),
and app.py treats that as "terminal unsupported" — the rest of Draftwatch is
unaffected. See TERMINAL_PLAN.md.

Security model (enforced by the HTTP layer in app.py, restated here because
this module is the payload): the server binds loopback only, every terminal
route requires the per-session token — header-only for the POST routes — and
this module never interprets input. Bytes from the browser go straight to the
PTY master; bytes from the PTY go straight back, base64-encoded. There is no
command API and nothing to inject into.
"""

import base64
import fcntl
import os
import pty
import queue
import select
import signal
import struct
import subprocess
import termios
import threading
import time

# Cap on retained output. A reconnecting client gets this replayed so the
# terminal survives a page reload; anything older is gone (it's a terminal,
# not a log).
SCROLLBACK_LIMIT = 200 * 1024

_READ_CHUNK = 4096
_TERM_GRACE = 1.5          # seconds between SIGHUP and SIGKILL on close


class TermSession:
    """At most one interactive shell on a PTY, fanned out to SSE subscribers.

    Lifecycle: start() spawns the user's shell in `root`; a reader thread pumps
    PTY output into a bounded scrollback plus per-subscriber queues; terminate()
    kills the whole process group. start() after exit is a fresh shell.
    """

    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.proc = None
        self.master_fd = None
        self.scrollback = bytearray()
        self.subscribers = []          # queue.Queue of dict events
        self.exit_code = None

    # ---- queries ----

    def running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def pid(self):
        with self.lock:
            return self.proc.pid if self.proc is not None else None

    # ---- lifecycle ----

    def start(self, cols=80, rows=24):
        """Spawn the shell if it isn't already running. Returns True if a new
        process was started, False if one was already live."""
        with self.lock:
            if self.running():
                return False
            # previous session (if any) is dead; start clean
            self._close_master()
            self.scrollback = bytearray()
            self.exit_code = None

            shell = os.environ.get("SHELL") or "/bin/sh"
            master, slave = pty.openpty()
            self._set_winsize(master, cols, rows)

            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"

            def _lead_session():
                # child: own session + the PTY as controlling terminal, so the
                # shell gets job control and ctrl-c reaches its children.
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            self.proc = subprocess.Popen(
                [shell],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=self.root, env=env,
                preexec_fn=_lead_session,   # noqa: PLW1509 — standard PTY recipe
                close_fds=True,
            )
            os.close(slave)
            self.master_fd = master

        t = threading.Thread(target=self._read_loop, args=(master,), daemon=True)
        t.start()
        return True

    def terminate(self):
        """Kill the whole process group: SIGHUP, short grace, then SIGKILL.
        Idempotent; safe to call at shutdown regardless of state."""
        with self.lock:
            proc = self.proc
        if proc is None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None
        if pgid is not None and proc.poll() is None:
            try:
                os.killpg(pgid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.time() + _TERM_GRACE
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        with self.lock:
            self._close_master()

    # ---- I/O ----

    def write(self, data):
        """Raw input bytes -> PTY master. Never parsed, never logged."""
        with self.lock:
            fd = self.master_fd
            if fd is None or not self.running():
                return False
        try:
            os.write(fd, data)
            return True
        except OSError:
            return False

    def resize(self, cols, rows):
        with self.lock:
            fd = self.master_fd
        if fd is None:
            return False
        try:
            self._set_winsize(fd, cols, rows)
            return True
        except OSError:
            return False

    # ---- SSE fan-out (mirrors State.subscribe in app.py) ----

    def subscribe(self):
        """Register a listener; returns (queue, hello_event). The hello event
        carries the scrollback so a (re)connecting client repaints instantly."""
        q = queue.Queue(maxsize=256)
        with self.lock:
            self.subscribers.append(q)
            hello = {
                "type": "hello",
                "running": self.running(),
                "exit_code": self.exit_code,
                "data": base64.b64encode(bytes(self.scrollback)).decode("ascii"),
            }
        return q, hello

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def _publish(self, event):
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # slow client: drop the chunk for that client; scrollback
                # replay on its next reconnect repairs the view
                pass

    # ---- internals ----

    def _read_loop(self, fd):
        """Pump PTY output until the shell exits, then announce the exit."""
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if not r:
                if not self.running():
                    break
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:      # EIO: slave side closed — shell exited
                break
            if not chunk:
                break
            with self.lock:
                self.scrollback.extend(chunk)
                if len(self.scrollback) > SCROLLBACK_LIMIT:
                    del self.scrollback[:len(self.scrollback) - SCROLLBACK_LIMIT]
            self._publish({"type": "out",
                           "data": base64.b64encode(chunk).decode("ascii")})
        code = None
        with self.lock:
            if self.proc is not None:
                code = self.proc.poll()
                if code is None:
                    try:
                        code = self.proc.wait(timeout=2)
                    except Exception:
                        code = None
            self.exit_code = code
            self._close_master()
        self._publish({"type": "exit", "code": code})

    def _close_master(self):
        # caller holds self.lock
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    @staticmethod
    def _set_winsize(fd, cols, rows):
        cols = max(2, min(int(cols), 1000))
        rows = max(2, min(int(rows), 1000))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
