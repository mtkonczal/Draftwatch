// Maintainer-only build: bundle the vendored frontend libraries into
// draftwatch/assets/. The artifacts are committed, so end users never need
// Node; run `npm install && npm run build:vendor` only when upgrading a
// library. Everything is served locally from the package (no CDN — offline
// use is a design constraint).
import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const assets = join(root, "draftwatch", "assets");
mkdirSync(assets, { recursive: true });

// ---- CodeMirror 6: one IIFE exposing a `CM` global with everything the
//      editor (draftwatch/app.py) uses ----------------------------------------
const cmEntry = `
export { EditorState, Compartment } from "@codemirror/state";
export { EditorView, keymap, drawSelection, placeholder } from "@codemirror/view";
export { defaultKeymap, history, historyKeymap, undo, redo } from "@codemirror/commands";
export { syntaxHighlighting, defaultHighlightStyle, HighlightStyle } from "@codemirror/language";
export { markdown, markdownLanguage } from "@codemirror/lang-markdown";
export { searchKeymap, search, openSearchPanel, highlightSelectionMatches } from "@codemirror/search";
export { tags } from "@lezer/highlight";
`;

await esbuild.build({
  stdin: { contents: cmEntry, resolveDir: root, loader: "js" },
  bundle: true,
  minify: true,
  format: "iife",
  globalName: "CM",
  outfile: join(assets, "codemirror.js"),
  logLevel: "info",
});

// ---- marked + DOMPurify + Turndown: upstream builds, copied verbatim --------
// marked/DOMPurify render markdown -> sanitized HTML for the preview; Turndown
// runs the reverse (HTML -> markdown) so edits made in the editable preview can
// be written back to the markdown source.
copyFileSync(join(root, "node_modules", "marked", "marked.min.js"),
             join(assets, "marked.js"));
copyFileSync(join(root, "node_modules", "dompurify", "dist", "purify.min.js"),
             join(assets, "purify.js"));
copyFileSync(join(root, "node_modules", "turndown", "dist", "turndown.js"),
             join(assets, "turndown.js"));

// ---- xterm.js + fit addon: one IIFE exposing an `XTerm` global --------------
// Powers the embedded terminal panel. The CSS ships alongside (xterm needs its
// stylesheet for layout/selection/cursor rendering).
const xtermEntry = `
export { Terminal } from "@xterm/xterm";
export { FitAddon } from "@xterm/addon-fit";
`;

await esbuild.build({
  stdin: { contents: xtermEntry, resolveDir: root, loader: "js" },
  bundle: true,
  minify: true,
  format: "iife",
  globalName: "XTerm",
  outfile: join(assets, "xterm.js"),
  logLevel: "info",
});
copyFileSync(join(root, "node_modules", "@xterm", "xterm", "css", "xterm.css"),
             join(assets, "xterm.css"));

// ---- license roll-up --------------------------------------------------------
const lic = (pkg, file = "LICENSE") =>
  readFileSync(join(root, "node_modules", pkg, file), "utf8");
const licenses = [];
const add = (name, body) =>
  licenses.push("=".repeat(72) + `\n${name}\n` + "=".repeat(72) + `\n\n${body.trim()}\n`);

const pkgVersion = (pkg) =>
  JSON.parse(readFileSync(join(root, "node_modules", pkg, "package.json"), "utf8")).version;

for (const pkg of [
  "@codemirror/state", "@codemirror/view", "@codemirror/commands",
  "@codemirror/language", "@codemirror/lang-markdown", "@codemirror/search",
  "@lezer/highlight",
]) {
  add(`${pkg} ${pkgVersion(pkg)} (MIT)`, lic(pkg));
}
add(`@xterm/xterm ${pkgVersion("@xterm/xterm")} (MIT)`, lic("@xterm/xterm"));
add(`@xterm/addon-fit ${pkgVersion("@xterm/addon-fit")} (MIT)`, lic("@xterm/addon-fit"));
add(`marked ${pkgVersion("marked")} (MIT)`, lic("marked", "LICENSE.md"));
add(`dompurify ${pkgVersion("dompurify")} (Apache-2.0 OR MPL-2.0)`, lic("dompurify"));
add(`turndown ${pkgVersion("turndown")} (MIT)`, lic("turndown"));

writeFileSync(
  join(root, "THIRD_PARTY_LICENSES"),
  "Third-party libraries vendored into draftwatch/assets/ (all served\n" +
  "locally; no CDN). Regenerate with `npm install && npm run build:vendor`.\n\n" +
  licenses.join("\n"),
);

console.log("vendor build complete: draftwatch/assets/{codemirror,marked,purify,turndown,xterm}.js + xterm.css + THIRD_PARTY_LICENSES");
