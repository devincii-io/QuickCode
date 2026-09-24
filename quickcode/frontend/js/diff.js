// Diffs, drawn one way wherever they appear: an edit's card in the transcript
// (the call's own old/new strings), the permission prompt (the unified diff
// the server built from the file, tools/fs/diffpreview.py), the rewind dialog
// and the Files panel (`git diff`). All become the same line records, and the
// same DOM — text nodes, never markup, so a line of the file cannot turn into
// an element.

const CLASS = {
  add: "diff-add", del: "diff-del", hunk: "diff-hunk", file: "diff-file", note: "diff-note",
};

/** Each line of a unified diff with its kind. `---`/`+++` are file headers
 *  only above the first hunk; below it they are a removed `--` line or an
 *  added `++` one. A `diff ` line (git's) opens a file's header block, where
 *  `index`, `new file mode` and the like are header lines too. */
export function unifiedLines(text) {
  let inHunk = false;
  let gitHeader = false;
  return String(text ?? "").split("\n").map((line) => {
    let kind = "ctx";
    if (line.startsWith("diff ")) { inHunk = false; gitHeader = true; kind = "file"; }
    else if (line.startsWith("@@")) { inHunk = true; gitHeader = false; kind = "hunk"; }
    else if (gitHeader) kind = "file";
    else if (!inHunk && (line.startsWith("--- ") || line.startsWith("+++ "))) kind = "file";
    else if (line.startsWith("… ")) kind = "note";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "del";
    return { kind, text: line };
  });
}

/** An edit call's two strings as removed lines, then added ones. */
export function replacementLines(oldText, newText) {
  const side = (text, kind, mark) =>
    String(text ?? "").split("\n").map((l) => ({ kind, text: `${mark} ${l}` }));
  return [...side(oldText, "del", "-"), ...side(newText, "add", "+")];
}

/** A `<pre>` holding the lines, one coloured span per changed line. */
export function diffNode(lines) {
  const pre = document.createElement("pre");
  pre.className = "diff";
  lines.forEach((line, i) => {
    if (i) pre.append("\n");
    const cls = CLASS[line.kind];
    if (!cls) return pre.append(line.text);
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = line.text;
    pre.append(span);
  });
  return pre;
}
