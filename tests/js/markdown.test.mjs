import test from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown } from "../../quickcode/frontend/js/markdown.js";
import { esc } from "../../quickcode/frontend/js/util.js";

// Everything the renderer emits, as tag names and attribute names. A payload
// only matters if it becomes one of these, so the assertions are about the
// markup the browser would build, not about substrings of the text.
function markup(html) {
  const tags = [...html.matchAll(/<([a-z0-9]+)((?:\s+[a-z-]+="[^"]*")*)\s*>/gi)];
  return tags.map(([, tag, attrs]) => ({
    tag: tag.toLowerCase(),
    attrs: [...attrs.matchAll(/([a-z-]+)="([^"]*)"/gi)].map(([, k, v]) => [k.toLowerCase(), v]),
  }));
}

const ALLOWED = new Set(["p", "br", "code", "pre", "strong", "em", "a", "h1", "h2", "h3", "h4",
  "ul", "ol", "li", "blockquote"]);

function assertSafe(html) {
  // No attribute can be opened by the text: every quote in the output belongs
  // to an attribute the renderer wrote itself.
  const stripped = html.replace(/\s(href|target|rel)="[^"]*"/g, "");
  assert.ok(!stripped.includes('"'), `stray quote in ${html}`);
  for (const { tag, attrs } of markup(html)) {
    assert.ok(ALLOWED.has(tag), `unexpected <${tag}> in ${html}`);
    for (const [name, value] of attrs) {
      assert.ok(["href", "target", "rel"].includes(name), `attribute ${name} in ${html}`);
      if (name === "href") assert.match(value, /^https?:/, `href ${value}`);
    }
  }
}

const PAYLOADS = [
  '<img src=x onerror="alert(1)">',
  "<script>alert(1)</script>",
  "<a href=javascript:alert(1)>x</a>",
  "[x](javascript:alert(1))",
  "[x](JaVaScRiPt:alert(1))",
  "[x](data:text/html,<script>alert(1)</script>)",
  "[x](vbscript:msgbox(1))",
  "[x](https://a.com/\"onmouseover=\"alert(1))",
  "[x](https://a.com/'onmouseover='alert(1))",
  "[x](https://a.com/`\"onmouseover=\"`)",
  "[x](https://a.com/**\"x\"**)",
  "![img](https://example.com/x.png)",
  "![img](javascript:alert(1))",
  "```html\" onmouseover=\"alert(1)\n<b>code</b>\n```",
  "```<script>\nx\n```",
  "&lt;script&gt; &#60;img src=x onerror=alert(1)&#62;",
  "`<img src=x onerror=alert(1)>`",
  "**<svg onload=alert(1)>**",
  "*<iframe src=javascript:alert(1)>*",
  "# <img src=x onerror=alert(1)>",
  "> <img src=x onerror=alert(1)>",
  "- <img src=x onerror=alert(1)>",
  "1. <img src=x onerror=alert(1)>",
  "a\0<img src=x onerror=alert(1)>\0b `c` \u00000\u0000",
];

test("esc neutralises every character that can open markup or an attribute", () => {
  assert.equal(esc(`<a href="x" title='y'>&amp;</a>`),
    "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;amp;&lt;/a&gt;");
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(42), "42");
});

test("hostile markdown renders as text, never as markup or a script URL", () => {
  for (const payload of PAYLOADS) {
    const html = renderMarkdown(payload);
    assertSafe(html);
    assert.ok(!/<(img|script|svg|iframe|a href=")(?!https?:)/i.test(html.replace(/<a href="https?:/g, "")),
      `live element from ${payload}: ${html}`);
  }
});

test("only http(s) links become anchors, and they open safely", () => {
  assert.equal(renderMarkdown("[docs](https://example.com/a?b=1&c=2)"),
    '<p><a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a></p>');
  assert.equal(renderMarkdown("[x](javascript:alert(1))"), "<p>[x](javascript:alert(1))</p>");
  assert.equal(renderMarkdown("[x](data:text/html,hi)"), "<p>[x](data:text/html,hi)</p>");
});

test("a code span stays literal and never ends up inside a link", () => {
  assert.equal(renderMarkdown("`[a](https://x.io)` and `**b**`"),
    "<p><code>[a](https://x.io)</code> and <code>**b**</code></p>");
  const html = renderMarkdown("[x](https://a.com/`b`)");
  assert.ok(!html.includes("href"), html);
  assert.ok(!renderMarkdown("[x](https://a.com/**b**)").includes("href"));
  assert.equal(renderMarkdown("see [the `api`](https://x.io)"),
    '<p>see <a href="https://x.io" target="_blank" rel="noopener noreferrer">the <code>api</code></a></p>');
});

test("a fence's info string is dropped rather than written into an attribute", () => {
  assert.equal(renderMarkdown('```js" onmouseover="alert(1)\nlet a = "<b>";\n```'),
    "<pre><code>let a = &quot;&lt;b&gt;&quot;;</code></pre>");
});
