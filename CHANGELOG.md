# Changelog

All notable changes to draftwatch are recorded here. Versions are git tags
(`vX.Y.Z`); the PyPI package tracks them.

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
