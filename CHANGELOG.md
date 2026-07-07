# Changelog

All notable changes to draftwatch are recorded here. Versions are git tags
(`vX.Y.Z`); the PyPI package tracks them.

## 0.2.0 — 2026-07-06

- Arming commit now gives the message box the footer's full width: the review
  controls (navigation arrows, changes-only, revert/keep all, apply) hide while
  the commit message is open — you've finished picking by then — and Esc brings
  them back. Prevents the commit button being pushed out of view in a narrow
  panel with the terminal open.
- **Resizable panels**: drag the dividers between panels to change the split;
  double-click a divider to reset. Splits last for the current session only —
  every launch starts at the default layout.
- **Embedded terminal panel** (macOS/Linux): a third, closable panel running a
  real shell in the repo, so an agent (Claude Code, Codex, anything) can work
  right next to the live diff. A toolbar button opens it; **hide** collapses
  the panel while the shell keeps running; **end session** kills the shell's
  whole process group. Output survives page reloads via scrollback replay over
  SSE. No snapshots are taken when commands run — edits accumulate against the
  selected baseline as usual. Security: same Host/Origin/token guards as every
  route, but the terminal's input routes accept the token only as a header
  (keystrokes never ride in URLs); input bytes are piped to the PTY, never
  parsed; `--no-terminal` removes the routes from the server entirely; and no
  shell outlives Draftwatch. On Windows (no pty) the feature is absent and
  Draftwatch behaves exactly as before. xterm.js and its fit addon are vendored
  like the other frontend libraries — local, offline, audited.
- Removed the `--host` flag: Draftwatch now always binds `127.0.0.1` and is
  never exposed on the network. The bind interface was configurable before
  anyone used it; locking it to loopback simplifies the security model ahead
  of the embedded terminal panel, where it becomes load-bearing.

## 0.1.1 — 2026-07-05

- Second instance no longer fails on a busy port. When `--port` is omitted,
  Draftwatch tries the default (8787) and, if it's taken (e.g. another instance
  is already running), automatically falls back to the next free port and
  prints which one it used. An explicit `--port` is still honored exactly and
  fails loudly on a conflict. The opened URL and the Host allowlist now derive
  from the actually-bound port.
- Runs outside a git repository instead of refusing to start. With no repo in
  the folder, Draftwatch opens in **write-only mode**: the editor, preview, and
  save all work, while the review loop (baselines, diff, revert, commit) is off.
  The right panel says so and offers a one-click **initialize git here** button
  that names the exact folder and runs `git init` via a new `/api/init-repo`
  endpoint (nothing is committed); the panel then switches into the normal
  start-tracking flow. The file picker falls back to a filesystem walk when
  there's no repo. Every SSE payload and `/api/files` now carry a `repo` flag.
- ⌘/Ctrl-S saves to file from anywhere — source view, editable preview, or the
  diff panel — suppressing the browser's own save dialog.
- New files: "+ start a new file…" at the top of the file dropdown switches to
  a blank, unnamed scratch buffer (the previously open file stays untouched on
  disk); starting draftwatch with no target gives the same buffer. Write
  first, name later: only saving (button or ⌘S) prompts for a name and creates
  the file via a new `/api/new` endpoint (subdirectories allowed, existing
  files refused, empty buffer fine), then starts watching it; the usual
  start-tracking flow follows. Detaching goes through a new `/api/close`. The
  empty-editor placeholder and status line say so.
- Header reorganized to two rows: file and baseline share the top row half
  each; the second row has preview / format / save on the left and theme /
  color / about on the right, with transient status in between.
- Per-hunk revert buttons are now a bare `✗` (state shown by highlight +
  tooltip); the diff panel header carries a matching "click ✗ to revert a
  change" hint, set in the same header type. Heavily edited lines stay
  readable.
- Color themes: blue (default), teal, iris, plum, graphite. Each restyles the
  whole surface — paper, panels, borders, ink, accent — the way blue does, in
  the same muted editorial register; graphite is the monochrome (Cursor-style)
  one. Each has a dark-mode variant, remembered in localStorage and applied
  before first paint, like the light/dark toggle.
- File creation lives on `/api/new`; `/api/save` refuses a `path` (no silent
  save-as).

## 0.1.0 — 2026-07-02

Initial release. Draftwatch watches a writing file in a git repo and shows an AI
agent's edits as the exact word-diff git produces on your machine — review,
keep/revert hunk by hunk, then commit.

### Diff engine

- All diffing goes through `git diff -U1000000 --word-diff=porcelain`. Every
  token carries a `+`/`-`/space prefix, so source text can never be mistaken for
  diff markup. Accept always reproduces the working file exactly; reject
  reproduces the baseline exactly (save a few documented blank-line/realignment
  edge cases).
- Hunk ids are content-addressed (a hash of the change plus a window of
  surrounding text), so review decisions survive an agent save mid-review; ids
  whose content vanished are pruned client-side.
- `/api/apply` carries a `diff_epoch` fingerprint of the rendered view; a stale
  apply (file or baseline changed underneath) is refused and the view refreshes,
  so reverts can never target the wrong spans.

### Review loop

- Review the diff, toggle **revert** on any hunk (with revert-all / keep-all),
  and **apply** to write the result. Switchable baseline: last push, HEAD, or an
  earlier commit.
- **Commit** runs a git commit of the current file state; the baseline advances
  to the new commit and the diff clears to zero. It is two-step: the first click
  reveals an optional message box (hidden until then) and focuses it; a second
  click, or Enter, commits; Esc cancels.
- Untracked files get a **start tracking** prompt (one-click first commit)
  instead of a one-hunk-per-line empty-tree diff flood.
- A change-map gutter, next/previous navigation (`n`/`p`), and a changes-only
  view make long documents navigable, with exact scroll-sync to the editor.

### Editor & editable preview

- The left panel is a CodeMirror 6 editor: markdown syntax highlighting, undo
  history, search (⌘/Ctrl-F), soft wrap, and bold/italic/code/link shortcuts
  (⌘/Ctrl-B/I/E/K).
- A **preview** toggle renders the buffer (unsaved edits included) via marked +
  DOMPurify. The preview is also a writing surface: type in it and the edit is
  converted back to markdown (Turndown) and written into the source, with the
  format toolbar, save, and the same shortcuts available there. The HTML→markdown
  round-trip normalizes formatting; the source view stays byte-exact.
- Sanitization is load-bearing (the file's author is a semi-trusted AI agent);
  the jsdom smoke test asserts script/handler stripping.

### Interface

- A cool, light-blue editorial palette with a steel-blue accent; green/red stay
  strictly semantic for the diff. A header control cycles the theme auto / light
  / dark, saved to localStorage and applied before first paint (no flash).
- An **About** panel explains the tool, the review loop, the editable-preview
  trade-off, and that it never talks to an LLM.
- The product name is capitalized **Draftwatch** in the UI, window/tab title, and
  prose; the package, module, CLI command, and file paths stay lowercase
  `draftwatch`.

### Security

- Binds `127.0.0.1` only. A per-session token (`secrets.token_urlsafe`) is
  required on all `/api/*` and `/events` requests; the tokenized URL is printed
  and auto-opened. Host-header allowlist (DNS-rebinding defense) and Origin
  validation; 403 otherwise.

### Packaging & distribution

- A `draftwatch/` package with `pyproject.toml` (Python 3.9+, zero runtime
  dependencies). `pip install draftwatch` provides the `draftwatch` console
  script; `python -m draftwatch` also works.
- The optional native window (`pip install 'draftwatch[app]'`) is the default
  launch surface when pywebview is installed (`--app` forces it, `--no-app`
  disables it, `--no-open` runs headless); the browser fallback says so in the
  status line instead of switching silently.
- CodeMirror 6, marked, DOMPurify, and Turndown are vendored into
  `draftwatch/assets/` (committed; `npm run build:vendor` is maintainer-only) and
  served from allowlisted `/static/` routes. `THIRD_PARTY_LICENSES` included.

### Tests

- Reconstruction harness: 10 required invariants (accept == working,
  reject == baseline).
- Acceptance harness: 19 end-to-end tests against a real server — content-address
  survival, stale-epoch rejection, token/Host 403s, the commit loop, start
  tracking, static assets, and native-window fallback.
- A maintainer-only jsdom smoke test boots the real page with the vendored
  bundles, feeds SSE payloads, and asserts editor/diff/preview/commit/theme
  wiring including XSS stripping.
