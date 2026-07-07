// Maintainer-only smoke test: load the embedded page in jsdom, stub the
// network (fetch/EventSource), and confirm the inline script boots without
// throwing — CodeMirror instantiates, the panels render, an SSE payload
// round-trips into the DOM. Catches wiring regressions the Python acceptance
// tests can't see (they never execute the JS). Run: node scripts/smoke_frontend.mjs
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

// pull INDEX_HTML out of the package without running a server
const html = execSync(
  `python3 -c "import sys; sys.path.insert(0,'.'); import draftwatch; sys.stdout.write(draftwatch.INDEX_HTML)"`,
  { maxBuffer: 64 * 1024 * 1024 }
).toString();

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));

const dom = new JSDOM(html.replace('<script src="/static/codemirror.js"></script>', ""), {
  url: "http://127.0.0.1:8787/?t=TESTTOKEN&app=1",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole: vc,
});
const w = dom.window;

// --- stubs the page needs ---------------------------------------------------
w.fetch = (url, opts) => Promise.resolve({ json: () => Promise.resolve({}) });
w.EventSource = class {
  constructor(url) { w.__lastES = this; this.url = url; }
  close() {}
};
if (!w.requestAnimationFrame) w.requestAnimationFrame = (f) => setTimeout(f, 0);
// jsdom implements neither execCommand nor prompt; stub them so the preview
// formatting paths can be exercised without a "Not implemented" jsdomError.
w.document.execCommand = (cmd) => { w.__lastExec = cmd; return true; };
w.prompt = () => null;

// --- load the vendored bundles, then the inline script -----------------------
try {
  w.eval(readFileSync("draftwatch/assets/codemirror.js", "utf8"));
  w.eval(readFileSync("draftwatch/assets/marked.js", "utf8"));
  w.eval(readFileSync("draftwatch/assets/purify.js", "utf8"));
  w.eval(readFileSync("draftwatch/assets/turndown.js", "utf8"));
  if (typeof w.CM !== "object") throw new Error("CM global missing");
  if (typeof w.marked !== "object" && typeof w.marked !== "function") throw new Error("marked global missing");
  if (typeof w.DOMPurify !== "function" && typeof w.DOMPurify !== "object") throw new Error("DOMPurify global missing");
  if (typeof w.TurndownService !== "function") throw new Error("TurndownService global missing");
  const inline = /<script>\n("use strict";[\s\S]*?)\n<\/script>/.exec(html);
  if (!inline) throw new Error("inline script not found in INDEX_HTML");
  w.eval(inline[1]);
} catch (e) {
  errors.push("boot: " + (e && e.stack || e));
}

// --- assertions ---------------------------------------------------------------
const must = (cond, name) => { if (!cond) errors.push("assert: " + name); };

must(w.document.querySelector("#editor-host .cm-editor"), "CodeMirror editor mounted");
must(w.__lastES && /\/events\?t=TESTTOKEN/.test(w.__lastES.url), "EventSource carries token");
must(w.document.body.classList.contains("app-mode"), "app=1 applies app-mode chrome");
must(w.document.querySelector("header .mark .ma"), "identity mark rendered");

// simulate an SSE payload with one substitution hunk
try {
  const payload = {
    type: "update",
    file: "draft.md",
    raw_text: "the quick red fox\n",
    baseline: { kind: "head", ref: "HEAD", label: "HEAD · now" },
    segments: [
      { type: "context", text: "the quick " },
      { type: "del", text: "brown", hunk: "abc123" },
      { type: "add", text: "red", hunk: "abc123" },
      { type: "context", text: " fox" },
      { type: "newline", text: "\n", side: "common", hunk: null },
    ],
    hunks: ["abc123"],
    file_mtime: 1,
    diff_epoch: "e1",
  };
  w.__lastES.onmessage({ data: JSON.stringify(payload) });
  const diffHTML = w.document.getElementById("diff").innerHTML;
  must(diffHTML.includes("red") && diffHTML.includes("brown"), "diff renders add+del");
  must(w.document.querySelector('#diff button.rev'), "revert control rendered");
  must(w.document.getElementById("counts").textContent.includes("1 change"), "counts updated");
  // editor received the working text
  const cmText = w.document.querySelector("#editor-host .cm-content").textContent;
  must(cmText.includes("the quick red fox"), "editor shows working text");

  // markdown preview: renders the buffer, strips scripts/handlers (the
  // sanitizer is load-bearing — the .md author is a semi-trusted AI agent)
  const evil = Object.assign({}, payload, {
    raw_text: "# Title\n\n*hi*\n\n<script>window.__pwned=1</" + "script>\n" +
              '<img src=x onerror="window.__pwned=2">\n',
    segments: [], hunks: [], diff_epoch: "e1b",
  });
  w.__lastES.onmessage({ data: JSON.stringify(evil) });
  w.document.getElementById("view-toggle").click();
  const pv = w.document.getElementById("preview");
  must(!pv.classList.contains("hidden"), "preview visible after toggle");
  // the preview is now an editable writing surface, so the format toolbar and
  // save stay available (they used to be hidden in preview)
  must(!w.document.getElementById("save").classList.contains("hidden"), "save available in preview");
  must(pv.getAttribute("contenteditable") === "true", "preview is editable");
  must(!w.document.querySelector("#edit-toolbar .fmt").classList.contains("hidden"), "format tools available in preview");
  must(pv.querySelector("h1"), "preview rendered markdown (h1)");
  must(!pv.querySelector("script"), "preview stripped <script>");
  must(!pv.innerHTML.includes("onerror"), "preview stripped event handlers");
  must(w.__pwned === undefined, "no script executed from preview");
  w.document.getElementById("view-toggle").click();   // back to source
  must(pv.classList.contains("hidden"), "source view restored");
  must(pv.getAttribute("contenteditable") === "false", "preview not editable in source view");

  // untracked onboarding payload
  const untracked = Object.assign({}, payload, {
    baseline: { kind: "empty", ref: "empty", label: "empty tree" },
    segments: [], hunks: [], raw_text: "novel\n", file: "novel.md", diff_epoch: "e2",
  });
  w.__lastES.onmessage({ data: JSON.stringify(untracked) });
  must(w.document.getElementById("start-tracking"), "start-tracking prompt rendered");

  // write-only mode (no git repo): the panel explains the trade-off and offers
  // a one-click init that names the exact folder; review controls hide.
  const writeOnly = Object.assign({}, payload, {
    segments: [], hunks: [], baseline: null, raw_text: "draft\n",
    file: "draft.md", diff_epoch: "ewo", repo: false, repo_dir: "/tmp/myfolder",
  });
  w.__lastES.onmessage({ data: JSON.stringify(writeOnly) });
  must(w.document.body.classList.contains("no-repo"), "no-repo class applied in write-only mode");
  must(w.document.getElementById("init-repo"), "init-repo button rendered in write-only mode");
  must(w.document.getElementById("diff").innerHTML.includes("/tmp/myfolder"),
       "write-only notice shows the exact folder path");
  // a repo payload clears write-only mode and restores the review panel
  const backToRepo = Object.assign({}, payload, { diff_epoch: "e1", repo: true, repo_dir: "/tmp/myfolder" });
  w.__lastES.onmessage({ data: JSON.stringify(backToRepo) });
  must(!w.document.body.classList.contains("no-repo"), "no-repo class cleared when repo returns");

  // commit is two-step: the message box is hidden until the first click reveals
  // it; a second click (not exercised here) would commit.
  const commitBtn = w.document.getElementById("commit-btn");
  const commitMsg = w.document.getElementById("commit-msg");
  must(commitBtn && commitMsg, "commit controls present");
  must(commitMsg.classList.contains("hidden"), "commit message box hidden initially");
  commitBtn.click();   // arm
  must(!commitMsg.classList.contains("hidden"), "commit message box shown after first click");
  must(commitBtn.classList.contains("armed"), "commit button armed after first click");
  // arming is a mode: the body class hides the review controls so the message
  // box gets the footer's full width; Esc restores everything
  must(w.document.body.classList.contains("commit-armed"), "commit-armed class set on arm");
  commitMsg.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", cancelable: true, bubbles: true }));
  must(!w.document.body.classList.contains("commit-armed"), "Esc disarms: commit-armed class removed");
  must(commitMsg.classList.contains("hidden"), "Esc disarms: message box hidden again");

  // about modal opens and closes
  const about = w.document.getElementById("about");
  must(about && !about.classList.contains("show"), "about modal hidden initially");
  w.document.getElementById("about-btn").click();
  must(about.classList.contains("show"), "about modal opens");
  w.document.getElementById("about-close").click();
  must(!about.classList.contains("show"), "about modal closes");

  // theme toggle cycles auto -> light -> dark -> auto via data-theme on <html>
  const themeBtn = w.document.getElementById("theme-toggle");
  must(themeBtn, "theme toggle present");
  themeBtn.click();
  must(w.document.documentElement.getAttribute("data-theme") === "light", "theme -> light");
  themeBtn.click();
  must(w.document.documentElement.getAttribute("data-theme") === "dark", "theme -> dark");
  themeBtn.click();
  must(!w.document.documentElement.getAttribute("data-theme"), "theme -> auto clears attribute");

  // Formatting shortcuts in the editable preview. Done last: they leave the
  // buffer dirty (an edit synced from preview), which is the intended turn-based
  // behavior but would trip the conflict banner on any later SSE payload.
  // In preview, focus is in the contenteditable (not CodeMirror), so the
  // shortcuts are wired on #preview directly.
  w.document.getElementById("view-toggle").click();   // into preview
  const pv2 = w.document.getElementById("preview");
  pv2.focus();
  const pvKey = (k) => {
    const ev = new w.KeyboardEvent("keydown", { key: k, metaKey: true, cancelable: true, bubbles: true });
    pv2.dispatchEvent(ev);
    return ev;
  };
  must(pvKey("b").defaultPrevented && w.__lastExec === "bold", "Cmd-B bolds in preview");
  must(pvKey("i").defaultPrevented && w.__lastExec === "italic", "Cmd-I italicizes in preview");
  // select some rendered text and wrap it as inline code with Cmd-E
  const target = pv2.querySelector("p, h1, h2, li") || pv2;
  const sel = w.getSelection(); sel.removeAllRanges();
  const rng = w.document.createRange(); rng.selectNodeContents(target); sel.addRange(rng);
  must(pvKey("e").defaultPrevented, "Cmd-E handled in preview");
  must(pv2.querySelector("code"), "Cmd-E wraps selection in <code>");
  must(pvKey("k").defaultPrevented, "Cmd-K handled in preview");

  // resizable panels: gutters exist, the JS-managed grid template is applied,
  // and a double-click reset doesn't throw (drag itself needs real layout,
  // which jsdom doesn't do — covered by the no-op guard).
  must(w.document.getElementById("gutter-1") && w.document.getElementById("gutter-2"),
       "panel gutters present");
  const mainCols = w.document.querySelector("main").style.gridTemplateColumns;
  must(/fr/.test(mainCols), "applyCols set the two-panel grid template");
  w.document.getElementById("gutter-1").dispatchEvent(
    new w.MouseEvent("mousedown", { bubbles: true, clientX: 100 }));
  w.document.getElementById("gutter-1").dispatchEvent(
    new w.MouseEvent("dblclick", { bubbles: true }));

  // terminal disabled: the raw template still carries the {{TERM}} placeholder
  // (!== "1"), which is exactly the served page when --no-terminal / Windows —
  // the button must stay hidden and no term-open class can appear.
  must(w.document.getElementById("term-toggle").classList.contains("hidden"),
       "terminal button hidden when feature is off");
  must(!w.document.body.classList.contains("term-open"),
       "no term-open class when feature is off");
} catch (e) {
  errors.push("payload: " + (e && e.stack || e));
}

// --- terminal enabled: separate dom with TERM=1 and a stubbed xterm ----------
// (jsdom can't render a real xterm; the stub verifies the page's wiring —
// button visible, open/hide toggles the grid class, open POSTs to the server.)
try {
  const htmlT = html.replace(/\{\{TERM\}\}/g, "1");
  const domT = new JSDOM(htmlT.replace('<script src="/static/codemirror.js"></script>', ""), {
    url: "http://127.0.0.1:8787/?t=TESTTOKEN&app=1",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const wt = domT.window;
  const posts = [];
  wt.fetch = (url, opts) => { if (opts && opts.method === "POST") posts.push(url);
    return Promise.resolve({ json: () => Promise.resolve({}) }); };
  wt.EventSource = class { constructor(url) { this.url = url; } close() {} };
  if (!wt.requestAnimationFrame) wt.requestAnimationFrame = (f) => setTimeout(f, 0);
  wt.document.execCommand = () => true;
  wt.prompt = () => null;
  wt.confirm = () => true;
  // xterm stub with the exact surface the page uses
  wt.XTerm = {
    Terminal: class {
      constructor() { this.cols = 80; this.rows = 24; }
      loadAddon() {} open() {} onData() {} onResize() {}
      write() {} reset() {} focus() {}
    },
    FitAddon: class { activate() {} fit() {} },
  };
  wt.eval(readFileSync("draftwatch/assets/codemirror.js", "utf8"));
  wt.eval(readFileSync("draftwatch/assets/marked.js", "utf8"));
  wt.eval(readFileSync("draftwatch/assets/purify.js", "utf8"));
  wt.eval(readFileSync("draftwatch/assets/turndown.js", "utf8"));
  const inlineT = /<script>\n("use strict";[\s\S]*?)\n<\/script>/.exec(htmlT);
  wt.eval(inlineT[1]);

  const btn = wt.document.getElementById("term-toggle");
  must(!btn.classList.contains("hidden"), "terminal button visible when enabled");
  btn.click();
  must(wt.document.body.classList.contains("term-open"), "terminal opens (grid class)");
  must(/minmax\(240px/.test(wt.document.querySelector("main").style.gridTemplateColumns),
       "three-panel grid template applied when terminal opens");
  await new Promise((r) => setTimeout(r, 20));   // let the fetch promise settle
  must(posts.some((u) => String(u).endsWith("/api/term/open")), "open POSTs /api/term/open");
  wt.document.getElementById("term-hide").click();
  must(!wt.document.body.classList.contains("term-open"), "hide collapses the panel");
  btn.click();
  must(wt.document.body.classList.contains("term-open"), "reopen restores the panel");
  domT.window.close();
} catch (e) {
  errors.push("terminal: " + (e && e.stack || e));
}

if (errors.length) {
  console.error("FRONTEND SMOKE FAILED:");
  for (const e of errors) console.error("  - " + e);
  process.exit(1);
}
console.log("frontend smoke: all checks passed");
// The page schedules a recurring poll (setInterval), which keeps jsdom's event
// loop alive; close the window and exit explicitly so the run terminates.
dom.window.close();
process.exit(0);
