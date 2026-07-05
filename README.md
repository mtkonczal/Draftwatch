# Draftwatch

Draftwatch is a lightweight IDE for writers that all them to review an AI agent's edits to their writing the way you'd review a pull request. Draftwatch shows the exact, git-backed word-diff of what changed in a file, lets you keep or revert each change, and commits when you're done.

The diff comes from git on your machine, not from the AI vendor and not from a JavaScript approximation. You get independent verification of what the agent actually did.

Python 3.9+ and git are the only requirements. The front-end libraries (CodeMirror 6, marked, DOMPurify, Turndown) are vendored and served locally, so it works offline and binds to localhost only.

## Install

Install straight from GitHub, no clone needed:

```bash
# uv (recommended): run without installing
uvx --from git+https://github.com/mtkonczal/Draftwatch.git draftwatch draft.md

# pipx: install the `draftwatch` command onto your PATH
pipx install git+https://github.com/mtkonczal/Draftwatch.git

# pip
pip install git+https://github.com/mtkonczal/Draftwatch.git
```

For the native app window, add the `app` extra:

```bash
pipx install "draftwatch[app] @ git+https://github.com/mtkonczal/Draftwatch.git"
```

## Usage

Run it from inside any git repository you write in:

```bash
draftwatch draft.md
```

Or to start with a specific file:

```bash
draftwatch draft.md
```

Draftwatch opens a two-panel review window: your source on the left (a real editor with markdown highlighting, search, and a live preview), the diff against your baseline on the right. Review the changes, revert the ones you don't want, apply, then commit. Committing advances the baseline, so the next agent pass starts clean.

Start without a file (`draftwatch`) to pick one in the window.

### Options

```
draftwatch [target] [--port 8787] [--host 127.0.0.1] [--no-open] [--app | --no-app]
```

- `target`: file to watch. Optional; omit it to pick one in the UI.
- `--port`: default `8787`.
- `--host`: default `127.0.0.1`. Changing this exposes the tool on your network and is not recommended.
- `--no-open`: don't auto-open a window (useful headless or over SSH).
- `--app` / `--no-app`: force or disable the native window. It is on by default when pywebview is installed and falls back to the browser otherwise.

## Features

- Real git word-diffs, so what you see is exactly what git sees.
- Keep or revert changes one hunk at a time, or all at once, then commit from the UI.
- Switchable baseline: last push, HEAD, or an earlier commit.
- Jump between changes or collapse to a changes-only view for long documents.
- Editable markdown preview alongside the raw source.
- You and the agent can both edit; your unsaved work is never clobbered when the file changes on disk.

## Security

Draftwatch binds `127.0.0.1` only. Every request carries a per-session token, the Host and Origin headers are validated to defeat DNS-rebinding, and the markdown preview is sanitized with DOMPurify before rendering. The tool never talks to any LLM. Don't change `--host` unless you understand the exposure.

## Tests

```bash
python3 testing/test_reconstruct.py   # reconstruction invariants
python3 testing/test_acceptance.py    # end-to-end server tests
```

## Development

The front-end libraries are vendored into `draftwatch/assets/` and committed, so end users never need Node. To rebuild them: `npm install && npm run build:vendor`.

## Author

Built by Mike Konczal. Vibe-coded with Fable 5.

## License

MIT. See `LICENSE`.
