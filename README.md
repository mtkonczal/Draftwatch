# Draftwatch

![Draftwatch reviewing an agent's edits: your working text on the left in a real editor, the word diff against your baseline on the right. Click any change to revert it.](https://raw.githubusercontent.com/mtkonczal/Draftwatch/main/assets/draftwatch_screenshot.png)

It’s difficult for writers to have AI act as an editor, because it’s never clear what exactly it changed when it does edits. You can’t trust eyeballing it, but making changes manually yourself means you don’t save that much time in the process. So you either ignore this potentially powerful writing helper, or you trust it even at the risk it does far more than you asked.

Enter Draftwatch. Draftwatch is a lightweight editing tool for writers. You can use it to create and format new Markdown documents from scratch. But it’s more powerful use is to use it with AI to review an AI agent's edits to your writing exactly down to the word level. The changes are managed by git, the way a developer reviews changes to their code, but formatted for writers who need to see each exact word change to be comfortable.

When reviewing AI edits, instead of guessing what an LLM changed in your document, Draftwatch shows you the exact, git-backed word-diff. You can step through, keep or revert each change individually, and commit when you're done.

Crucially, the diff comes from your local git—not from the AI vendor and not from a JavaScript approximation. You get absolute, independent verification of what the agent or script actually did.

Why this vibe-coded app? I designed this to be an Integrated Development Environment (IDE) for writers. Most IDEs show “git diff” at the line level, which is appropriate for coders (where each command is its own line generally) but terrible for writers, since they work at the paragraph level. This IDE is built around displaying "git diff --word-diff=porcelain” which allows writers to see the specific words being edited. Even for the IDEs that do show this, it is generally more difficult for writers to track the edits and those IDEs have coding features and visual baggage that writers won’t need.

Python 3.9+ and git are the only requirements. If you are a writer, AI itself can help you install these widely used tools. The front-end libraries (CodeMirror 6, marked, DOMPurify, Turndown, xterm.js) are vendored and served locally, so Draftwatch works completely offline and binds to your local computer.

## Install

Draftwatch is available on PyPI. You can install or run it using your preferred Python package manager:

```bash
# pipx
pipx install draftwatch

# uv 
uv tool install draftwatch

# standard pip
pip install draftwatch
```

## Usage

Run it from inside a git repository you write in:

```bash
draftwatch
```

Or to start with a specific file:

```bash
draftwatch draft.md
```

You can also run it outside any git repository. Draftwatch starts in **write-only mode** — the editor, preview, and saving all work, but the review loop (diffs, revert, commit) is off because there's no git to compare against. The right panel offers a one-click **initialize git here** to turn the folder into a repo and switch on the full review loop.

Starting a second instance while one is already running just works: if the default port is busy, Draftwatch picks a free one and prints the URL.

Draftwatch opens a two-panel review window: your source on the left (a real editor with markdown highlighting, search, and a live preview), the diff against your baseline on the right. Review the changes, revert the ones you don't want, apply, then commit. Committing advances the baseline, so the next agent pass starts clean.

Start without a file (`draftwatch`) to pick one in the window.

## The terminal panel

Click **terminal** in the toolbar to open a third panel running a real shell in your repo (macOS/Linux). Launch `claude`, `codex`, or any command there: when the agent edits the file you're watching, the diff panel lights up live — prompt on the right, review in the middle, write on the left. **hide** collapses the panel and leaves the shell running (an agent mid-task keeps working); **end session** kills the shell and everything it started. No snapshots are taken when you run commands — edits accumulate against whatever baseline you've selected, and you review them on your schedule. On Windows the panel is unavailable and Draftwatch simply runs without it.

## Features

- Real git word-diffs, so what you see is exactly what git sees.
- Keep or revert changes one hunk at a time, or all at once, then commit from the UI.
- Switchable baseline: last push, HEAD, or an earlier commit.
- Jump between changes or collapse to a changes-only view for long documents.
- Editable markdown preview alongside the raw source.
- You and the agent can both edit; your unsaved work is never clobbered when the file changes on disk.
- Embedded terminal panel (macOS/Linux): run your agent next to the diff without leaving the window.
- Resizable panels: drag the dividers to change the split (double-click to reset).

### Options

```
draftwatch [target] [--port 8787] [--no-open] [--no-terminal] [--app | --no-app]
```

- `target`: file to watch. Optional; omit it to pick one in the UI.
- `--port`: default `8787`. If omitted and the default is busy, Draftwatch
  picks a free port automatically; pass `--port` to pin an exact one (it then
  fails loudly if that port is taken).
- `--no-open`: don't auto-open a window (useful headless or over SSH).
- `--no-terminal`: disable the embedded terminal panel entirely (its routes are removed from the server, not just hidden in the UI).
- `--app` / `--no-app`: force or disable the native window. It is on by default when pywebview is installed and falls back to the browser otherwise.

## Author

Built by Mike Konczal. You can find out more about me at my webpage [here](https://www.mikekonczal.com/). Vibe-coded with Fable 5.

## License

MIT. See `LICENSE`.
