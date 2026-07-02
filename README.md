# Draftwatch

Draftwatch is a read-and-review tool for writers whose files are being edited by an
autonomous AI agent (Claude Code, etc.). It shows you the exact, git-backed word-diff
of what changed, lets you keep or revert changes hunk by hunk, and closes the loop
with a git commit. The diff is produced by **git on your machine**, never by the AI
vendor and never by a JS approximation — independent verification of what the agent
actually did.

Zero runtime dependencies (Python standard library + git). All front-end libraries
(CodeMirror 6, marked, DOMPurify, Turndown) are vendored and served locally — no
CDN, works offline. Localhost only. Python 3.9+.

## Install

You don't need to clone this repo. Install straight from GitHub in one terminal
command (replace the URL with your fork/repo if different):

```bash
# with uv (recommended) — run without installing
uvx --from git+https://github.com/mtkonczal/draftwatch.git draftwatch draft.md

# or with pipx — install the `draftwatch` command onto your PATH
pipx install git+https://github.com/mtkonczal/draftwatch.git

# or plain pip (a virtualenv is a good idea)
pip install git+https://github.com/mtkonczal/draftwatch.git
```

To get the native window, install the `app` extra:
`pipx install "draftwatch[app] @ git+https://github.com/mtkonczal/draftwatch.git"`
(or `pip install "draftwatch[app] @ git+…"`).

Pin a specific version by appending a tag, e.g. `…draftwatch.git@v0.1.0`.

Once it's published to PyPI, the shorter forms work too:

```bash
uvx draftwatch draft.md          # run without installing
pipx run draftwatch draft.md     # or pipx install draftwatch
pip install draftwatch           # then: draftwatch draft.md
```

## Quickstart

```bash
draftwatch draft.md
```

Run it from inside any git repository you write in. Output on the terminal:

```
draftwatch · watching draft.md
baseline: last push · origin/main · 2 hours ago
open http://127.0.0.1:8787/?t=<session-token>   (ctrl-c to stop)
```

With pywebview installed (`pip install 'draftwatch[app]'`), Draftwatch opens in
its **own native window** by default — app chrome, no browser tabs or URL bar.
Without it, it opens your browser with a tokenized URL; either way you get the
same two-panel review UI. You can also start it **without a file** (`draftwatch`)
and pick one in the window.

## CLI

```
draftwatch [target] [--port 8787] [--host 127.0.0.1] [--no-open] [--app | --no-app]
```

- `target` (optional): the file to watch, relative or absolute. If omitted,
  Draftwatch starts in the current git repo and you pick a file in the UI.
- `--port` (default `8787`).
- `--host` (default `127.0.0.1`). Changing this exposes the tool on your network
  and is not recommended.
- `--no-open`: do not auto-open anything (useful over SSH or headless); also
  skips the default native window.
- `--app`: force the native window (the default when pywebview is installed).
  Falls back to the browser — with a visible notice — when unavailable.
- `--no-app`: never open the native window; always use the browser.

## How it works

A local HTTP server does the git work and watches the file with a ~300ms mtime
poll. The browser is only a display surface, fed over localhost with Server-Sent
Events. No build step, no framework.

- **Left panel** is a CodeMirror 6 editor showing the raw source: markdown syntax
  highlighting, real undo history, a search panel (⌘/Ctrl-F), soft wrap, and
  bold/italic/code/link shortcuts (⌘/Ctrl-B/I/E/K). You can type directly into it
  and save. A **preview** toggle renders the current buffer (unsaved edits
  included) as a sanitized reading view. The preview is also **editable**: type in
  it, and the change is converted back to markdown (via Turndown) and written into
  the source, so the format toolbar and save stay available there too. That
  round-trip normalizes markdown formatting (heading, emphasis, and spacing
  conventions can shift), so for byte-exact control of the source, edit in the
  source view; relative image paths may not resolve in preview.
- **Right panel** shows the diff against the baseline: added words on green,
  removed words on red with strikethrough. Each change is a *hunk* with a revert
  toggle.

All diffing goes through `git diff -U1000000 --word-diff=porcelain` (the
machine-readable word-diff variant, where every token carries a `+`/`-`/space
prefix and `~` marks newlines), so the whole document streams through with full
context, and what you see is exactly what git sees. Because every token is
prefixed, source text can never be mistaken for diff markup.

### The review loop

The agent's edits are already on disk, so every change is **kept by default**.
The loop is: review → revert what you don't want → apply → **commit**.

- **Revert** a hunk (the ✗ control, a toggle): restore the baseline version of
  that span. Click again to keep it.
- **Revert all** / **keep all** set or clear every hunk at once.
- **Apply** writes the result — reverting the marked hunks, keeping the rest. The
  footer shows `N changes · M to revert`.
- **Commit** runs a git commit of the current file state. It is deliberately
  two-step: the first click reveals an optional message box (and focuses it), the
  second click (or Enter) commits; Esc cancels. The baseline advances to the new
  commit, so the diff clears to zero and the next agent pass starts clean.

Two safety rails back this up:

- Hunk ids are **content-addressed** (a hash of the change and its surrounding
  text), so your revert marks survive an agent save that lands mid-review; marks
  whose content vanished are pruned.
- Every apply carries a **diff epoch** (a fingerprint of the file content and
  baseline the view was rendered from). If the file changed underneath the
  render, the server refuses the apply, refreshes the view, and your surviving
  marks stay put — it never reverts the wrong spans.

Writes are always explicit (a button). The tool never auto-writes; the only
commits are the ones you ask for (commit / start tracking).

### Finding changes in a long document

- **Change-map gutter** — a thin strip on the right edge of the diff panel with a
  green/red tick for every change. Click a tick to jump; click anywhere in the
  strip to jump proportionally.
- **Next / previous** — the ▼ / ▲ buttons (or `n` / `p`) step through changes in
  order; a `3 / 12` counter shows where you are. Jumping also scrolls the left
  editor to exactly the same line (CodeMirror knows its line geometry), so the
  change is centered on both sides at once.
- **Changes only** — collapses unchanged text to a few lines of context around
  each change, with click-to-expand bars.

### Opening and switching files

The top bar has a **file** dropdown listing the repo's writing files (`.md`,
`.markdown`, `.qmd`, `.txt`, `.rst`). Picking a file switches what's watched
without restarting. To watch a file with a different extension, pass it on the
command line; `/api/open` accepts any in-repo path if you're scripting.

### Untracked files ("start tracking")

Opening a file git doesn't know yet would diff it against nothing — thousands of
"added" hunks with no reviewable meaning. Instead, Draftwatch shows a plain-language
prompt with a **start tracking** button: one click saves the current version as the
first commit, and from then on only actual edits show up as changes. The raw
all-additions view stays one click away.

### Baseline

The diff is always "baseline versus the working file on disk." Switchable live:

1. **Last push** (default when available) — the remote-tracking ref your branch
   pushes to; shows everything not yet pushed.
2. **HEAD** — your last commit.
3. **An earlier commit** — pick from the file's git log.

### Turn-based editing

The agent writes to disk; you may also edit in the tool. They do not clobber each
other. If you have unsaved changes and the file changes on disk, Draftwatch shows a
banner (`file changed on disk — [keep my edits] [reload]`) instead of overwriting
your buffer.

## Known limitations

Because all diffing goes through `git diff --word-diff=porcelain` (by design — the
diff is git, not a reimplementation), a few narrow cases are inherent:

1. **Pure blank-line insert/delete is ambiguous.** Adding a blank line to `a\nb`
   and removing the blank line from `a\n\nb` produce identical porcelain bodies (a
   bare `~`), so the format cannot express which side a content-free newline
   belongs to; Draftwatch treats it as common. For a pure blank-line *addition*,
   rejecting will not remove the blank line; for a pure *removal*, accepting will
   not drop it. Changes that touch actual text are unaffected.

2. **Word realignment can shift whitespace on the baseline side.** When git finds
   a word shared across a change boundary, reconstructing the *reject* side can be
   off by a space. The *accept* (working) side always reconstructs byte-for-byte.

3. **Trailing newline.** Round-trips are exact up to at most a single trailing
   newline.

(Earlier versions used plain `--word-diff`, whose unescapable `[-`/`{+` markers
could collide with literal text. Porcelain prefixes every token, so that failure
mode is gone and is covered by a required test.)

In short: **accept always reproduces the working file exactly.** Reject reproduces
the baseline exactly except in the cases above.

## Security

Draftwatch binds `127.0.0.1` only and defends the write endpoints in depth:

- A **per-session token** (`secrets.token_urlsafe`) is minted at startup and
  embedded in the URL it opens. Every `/api/*` and `/events` request must carry
  it (header or query param); anything without it gets a 403. A drive-by web
  page or another local process cannot save, revert, or commit.
- The **Host header must be exactly** `127.0.0.1:port`/`localhost:port`, which
  defeats DNS-rebinding; a mismatched **Origin** is also refused.
- The markdown **preview is sanitized** with DOMPurify before rendering — the
  file's author is a semi-trusted AI agent, and an unsanitized `<script>` in a
  `.md` would otherwise run with access to the session token.

Do not change `--host` unless you understand the exposure. The tool does not talk
to any LLM.

## Tests

Two standalone harnesses (standard library only) validate correctness against
real git:

```bash
python3 testing/test_reconstruct.py   # reconstruction invariants: accept==working, reject==baseline
python3 testing/test_acceptance.py    # end-to-end: real server, every route, 19 tests
```

There is also a maintainer-only frontend smoke test (`node scripts/smoke_frontend.mjs`,
needs `npm install`) that boots the real page in jsdom, feeds it SSE payloads, and
asserts the editor/diff/preview wiring including sanitization.

## Development

The frontend libraries (CodeMirror 6, marked, DOMPurify, Turndown) are vendored
into `draftwatch/assets/` and committed, so end users never need Node. To upgrade
them: `npm install && npm run build:vendor` (see `scripts/build_vendor.mjs`,
licenses in `THIRD_PARTY_LICENSES`).

## License

MIT. See `LICENSE`.
