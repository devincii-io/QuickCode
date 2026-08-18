"""HTML to markdown, for a reader that pays by the token.

Handing a model raw HTML wastes most of a context window on attributes and
wrapper divs, and buries the two paragraphs that mattered. This converter keeps
what carries meaning -- headings, paragraphs, lists, links, code, tables,
quotes -- and drops what carries layout.

Chrome is dropped wholesale by tag name: ``script``, ``style``, ``nav``,
``header``, ``footer``, ``aside``, ``form`` and friends. That is a blunt rule
and it occasionally costs a breadcrumb, which is the right trade against
repeating a site's entire navigation menu on every fetch.

Built on ``html.parser`` from the standard library, deliberately: this runs on
attacker-supplied markup, and the failure mode of a tolerant, non-recursive
parser is a slightly wrong heading, while the failure mode of a dependency is a
dependency.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

# Never rendered, and neither is anything inside them.
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "iframe", "object",
    "embed", "template", "form", "button", "select", "option", "textarea",
    "nav", "header", "footer", "aside", "menu", "dialog",
}

BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "figure", "figcaption",
    "dl", "dt", "dd", "address", "details", "summary",
}

_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_WS = re.compile(r"\s+")
_BLANKS = re.compile(r"\n{3,}")


class _Converter(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._prefix = ""
        self._skip = 0
        self._pre = 0
        self._in_title = False
        self._quote = 0
        self._lists: list[list] = []      # [kind, counter] per nesting level
        self._links: list[str] = []       # hrefs of the open <a> tags
        self._row_cells = 0
        self._row_is_header = False

    # -- output plumbing ----------------------------------------------------

    def _flush(self) -> None:
        text = "".join(self._buf)
        self._buf.clear()
        if not self._pre:
            text = _WS.sub(" ", text).strip()
        if not text:
            self._prefix = ""
            return
        quote = "> " * self._quote
        self.lines.append(f"{quote}{self._prefix}{text}")
        self._prefix = ""

    def _emit(self, line: str) -> None:
        self._flush()
        self.lines.append(line)

    def _blank(self) -> None:
        self._flush()
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    # -- parser callbacks ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip:
            if tag in SKIP_TAGS:
                self._skip += 1
            return
        if tag in SKIP_TAGS:
            self._flush()
            self._skip = 1
            return

        attr = {k: (v or "") for k, v in attrs}

        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._blank()
            self._prefix = "#" * _HEADINGS[tag] + " "
        elif tag in BLOCK_TAGS:
            self._blank()
        elif tag == "br":
            self._flush()
        elif tag == "hr":
            self._blank()
            self.lines.append("---")
            self.lines.append("")
        elif tag in ("ul", "ol"):
            self._blank()
            self._lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            depth = max(0, len(self._lists) - 1)
            kind, count = self._lists[-1] if self._lists else ["ul", 0]
            if self._lists:
                self._lists[-1][1] = count + 1
            marker = f"{count + 1}. " if kind == "ol" else "- "
            self._prefix = "  " * depth + marker
        elif tag == "blockquote":
            self._blank()
            self._quote += 1
        elif tag == "pre":
            self._blank()
            self.lines.append("```")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self._buf.append("`")
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "a":
            href = attr.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "#", "data:")):
                self._links.append(urljoin(self.base_url, href) if self.base_url else href)
                self._buf.append("[")
            else:
                self._links.append("")
        elif tag == "img":
            alt = attr.get("alt", "").strip()
            if alt:
                self._buf.append(f"[image: {alt}]")
        elif tag == "table":
            self._blank()
        elif tag == "tr":
            self._flush()
            self._row_cells = 0
            self._row_is_header = False
            self._buf.append("|")
        elif tag in ("td", "th"):
            self._row_cells += 1
            self._row_is_header = self._row_is_header or tag == "th"
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag in SKIP_TAGS:
                self._skip -= 1
            return

        if tag == "title":
            self._in_title = False
        elif tag in _HEADINGS:
            self._flush()
            self.lines.append("")
        elif tag in BLOCK_TAGS or tag == "li":
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self._lists:
                self._lists.pop()
            if not self._lists:
                self.lines.append("")
        elif tag == "blockquote":
            self._flush()
            self._quote = max(0, self._quote - 1)
            self.lines.append("")
        elif tag == "pre":
            self._flush()
            self._pre = max(0, self._pre - 1)
            self.lines.append("```")
            self.lines.append("")
        elif tag == "code" and not self._pre:
            self._buf.append("`")
        elif tag in ("strong", "b"):
            self._buf.append("**")
        elif tag in ("em", "i"):
            self._buf.append("*")
        elif tag == "a":
            href = self._links.pop() if self._links else ""
            if href:
                self._buf.append(f"]({href})")
        elif tag in ("td", "th"):
            self._buf.append(" |")
        elif tag == "tr":
            self._flush()
            if self._row_is_header and self._row_cells:
                self.lines.append("|" + " --- |" * self._row_cells)
        elif tag == "table":
            self._flush()
            self.lines.append("")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title = (self.title + data).strip()
            return
        if self._pre:
            self._buf.append(data)
            # A code block keeps its line structure; flush at each newline so
            # the fenced block comes out as lines rather than as one long one.
            if "\n" in data:
                pieces = "".join(self._buf).split("\n")
                self._buf = [pieces.pop()]
                self.lines.extend(pieces)
            return
        self._buf.append(data)

    def result(self) -> str:
        self._flush()
        text = "\n".join(line.rstrip() for line in self.lines)
        return _BLANKS.sub("\n\n", text).strip()


def html_to_markdown(html: str, *, base_url: str = "") -> tuple[str, str]:
    """Convert a page to markdown. Returns ``(title, markdown)``.

    Never raises on bad markup: ``html.parser`` is tolerant by design and this
    runs on pages nobody vetted.
    """
    parser = _Converter(base_url=base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed input must not fail a fetch
        pass
    return parser.title, parser.result()
