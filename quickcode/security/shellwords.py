"""Shell words as the shell will read them, for the permission engine.

The engine decides on a command line before any shell has seen it, so every
difference between how it reads a word and how bash reads the same word is a
way past it. The 2.4.1 sweep found the first of these (``.en''v`` is ``.env``);
the rest are the same bug in other clothes:

- escapes: ``.e\\nv`` is ``.env`` once the backslash is removed;
- ANSI-C quoting: ``$'\\x2eenv'`` is ``.env``;
- brace expansion: ``{.env,x}`` is two words, the first of them ``.env``;
- globs: ``.e?v`` and ``.en*`` expand to ``.env`` if it exists;
- option values: ``--from-file=.env`` and ``-f.env`` name ``.env`` too.

Nothing here decides anything. It turns one word into every string the shell
might open, and the engine tests each of them.
"""

from __future__ import annotations

import os
import re

# Whether a glob that opens with a wildcard can match a leading dot. Bash says
# no; PowerShell and cmd, which the bash tool may fall back to on Windows, say
# yes -- so there `cat *` reads `.env`.
GLOB_MATCHES_DOTFILES = os.name == "nt"

GLOB_CHARS = frozenset("*?[")

# More alternatives than this and the word is treated as unknowable, which the
# engine reads as protected. `{a..z}{a..z}{a..z}` is not a path anybody means.
BRACE_LIMIT = 64
BRACE_WORD_LIMIT = 16_384

_ANSI_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_ANSI_NUMERIC = (("x", 2), ("u", 4), ("U", 8))
_REDIRECT_PREFIX = re.compile(r"^\d*(?:[<>]+&?|&>+)")


def _ansi_c(word: str, i: int) -> tuple[str, int]:
    """Decode a ``$'...'`` body starting at ``i``; returns (text, index after)."""
    out: list[str] = []
    n = len(word)
    while i < n and word[i] != "'":
        c = word[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = word[i + 1]
        numeric = next((width for p, width in _ANSI_NUMERIC if p == e), 0)
        digits = re.match(rf"[0-9a-fA-F]{{1,{numeric}}}", word[i + 2:]) if numeric else None
        octal = re.match(r"[0-7]{1,3}", word[i + 1:])
        if e in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[e])
            i += 2
        elif digits:
            out.append(chr(min(int(digits.group(), 16), 0x10FFFF)))
            i += 2 + len(digits.group())
        elif octal:
            out.append(chr(int(octal.group(), 8) & 0xFF))
            i += 1 + len(octal.group())
        elif e == "c" and i + 2 < n:
            out.append(chr(ord(word[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + e)
            i += 2
    return "".join(out), i + 1


def dequote(word: str) -> str:
    """Bash's quote removal and escape processing for a single word."""
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "\\":
            if i + 1 < n and word[i + 1] != "\n":
                out.append(word[i + 1])
            i += 2
        elif c == "'":
            end = word.find("'", i + 1)
            end = n if end < 0 else end
            out.append(word[i + 1:end])
            i = end + 1
        elif c == "$" and word[i + 1:i + 2] == "'":
            text, i = _ansi_c(word, i + 2)
            out.append(text)
        elif c == "$" and word[i + 1:i + 2] == '"':
            i += 1
        elif c == '"':
            i += 1
            while i < n and word[i] != '"':
                if word[i] == "\\" and i + 1 < n and word[i + 1] in '$`"\\\n':
                    if word[i + 1] != "\n":
                        out.append(word[i + 1])
                    i += 2
                    continue
                out.append(word[i])
                i += 1
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class _Overflow(Exception):
    pass


def _brace_alternatives(inner: str) -> list[str] | None:
    """The alternatives of one ``{...}`` body, or None if it is not a brace
    expansion (bash leaves it literal)."""
    depth, start, parts = 0, 0, []
    for i, c in enumerate(inner):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    if parts:
        return [*parts, inner[start:]]
    seq = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", inner)
    if seq:
        lo, hi = int(seq.group(1)), int(seq.group(2))
        step = abs(int(seq.group(3) or 1)) or 1
        if abs(hi - lo) // step >= BRACE_LIMIT:
            raise _Overflow
        rng = range(lo, hi + 1, step) if lo <= hi else range(lo, hi - 1, -step)
        return [str(v) for v in rng]
    seq = re.fullmatch(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?", inner)
    if seq:
        lo, hi = ord(seq.group(1)), ord(seq.group(2))
        step = abs(int(seq.group(3) or 1)) or 1
        rng = range(lo, hi + 1, step) if lo <= hi else range(lo, hi - 1, -step)
        return [chr(v) for v in rng]
    return None


def _brace_pairs(word: str) -> list[tuple[int, int]]:
    """Matching ``{``/``}`` positions, in order of the opening brace."""
    pairs: list[tuple[int, int]] = []
    stack: list[int] = []
    for i, c in enumerate(word):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            pairs.append((stack.pop(), i))
    return sorted(pairs)


def brace_expand(word: str, limit: int = BRACE_LIMIT) -> list[str] | None:
    """Every word bash's brace expansion makes of ``word``; None past ``limit``
    (or for a word too long to read braces in, which is unknown, not safe)."""
    if "{" not in word:
        return [word]
    if len(word) > BRACE_WORD_LIMIT:
        return None
    pending, done = [word], []
    while pending:
        current = pending.pop()
        for i, j in _brace_pairs(current):
            if i and current[i - 1] == "$":
                continue
            try:
                alternatives = _brace_alternatives(current[i + 1:j])
            except _Overflow:
                return None
            if alternatives is not None:
                pending += [current[:i] + alt + current[j + 1:] for alt in alternatives]
                break
        else:
            done.append(current)
        if len(done) + len(pending) > limit:
            return None
    return done


def path_candidates(token: str) -> list[str] | None:
    """Every string ``token`` may name a file as; None when that is unknowable.

    Both readings of a backslash are kept: bash drops it (``.e\\nv``), a
    Windows path keeps it as a separator (``C:\\Users\\me\\.ssh``). An option
    contributes its value (``--from-file=.env``, ``-f.env``), an assignment its
    right-hand side, a redirection its target, and a PowerShell array
    (``x,.env``) each element.
    """
    out: list[str] = []
    unquoted = re.sub(r"\$(?=['\"])", "", token).replace("'", "").replace('"', "")
    for word in {unquoted, dequote(token)}:
        expanded = brace_expand(word)
        if expanded is None:
            return None
        for w in expanded:
            w = _REDIRECT_PREFIX.sub("", w)
            forms = [w]
            if w.startswith("-"):
                if "=" in w:
                    forms.append(w.split("=", 1)[1])
                elif not w.startswith("--") and len(w) > 2:
                    forms.append(w[2:])
                forms = forms[1:]
            elif "=" in w:
                forms.append(w.split("=", 1)[1])
            for form in forms:
                out += [p.strip("{}()") for p in [form, *form.split(",")]]
    return [c for c in dict.fromkeys(out) if c]


def has_glob(word: str) -> bool:
    return any(c in GLOB_CHARS for c in word)


def has_unquoted_paren(line: str) -> bool:
    """Whether ``line`` opens a parenthesis outside any quotes.

    In PowerShell ``cat (Remove-Item x)`` evaluates the parenthesised pipeline
    before ``cat`` sees a thing, and so does ``cat x,(Remove-Item y)``. In bash
    an unquoted ``(`` inside a word is a syntax error or a substitution, so no
    ordinary command line loses anything by being read as one.
    """
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote[-1]:
                quote = ""
            elif c == "\\" and quote != "'":
                i += 1
        elif c == "\\":
            i += 1
        elif c == "$" and line[i + 1:i + 2] == "'":
            quote = "$'"
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "(":
            return True
        i += 1
    return False


_OPERATOR_CHARS = frozenset(";&|\n()`<>")
# Runs the lexer can take whole: unquoted ordinary characters, and the inside of
# a double- or ANSI-C-quoted string up to its next quote or backslash.
_PLAIN_RUN = re.compile(r"[^\s'\"\\$;&|()`<>]+")
_QUOTED_RUN = {'"': re.compile(r'[^"\\]+'), "$'": re.compile(r"[^'\\]+")}


def segments(line: str) -> list[list[str]]:
    """``line`` as simple commands, each a list of dequoted words.

    Quote-aware, unlike the engine's first split, so ``bash -c "rm -rf x; y"``
    keeps its argument whole. Command and process substitution boundaries end
    a segment too; their bodies are also returned by ``substitutions``.
    """
    result: list[list[str]] = []
    words: list[str] = []
    raw: list[str] = []
    quote = ""
    i = 0

    def end_word() -> None:
        if raw:
            words.append(dequote("".join(raw)))
            raw.clear()

    def end_segment() -> None:
        end_word()
        if words:
            result.append(list(words))
            words.clear()

    n = len(line)
    while i < n:
        c = line[i]
        if quote == "'":
            end = line.find("'", i)
            end = n - 1 if end < 0 else end
            raw.append(line[i:end + 1])
            quote = ""
            i = end + 1
            continue
        if quote:
            run = _QUOTED_RUN[quote].match(line, i)
            if run:
                raw.append(run.group())
                i = run.end()
                continue
            raw.append(c)
            if c == quote[-1]:
                quote = ""
            elif c == "\\" and i + 1 < n:
                raw.append(line[i + 1])
                i += 1
            i += 1
            continue
        run = _PLAIN_RUN.match(line, i)
        if run:
            raw.append(run.group())
            i = run.end()
            continue
        if c == "\\" and i + 1 < n:
            raw.append(c + line[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            raw.append(c)
        elif c == "$" and line[i + 1:i + 2] == "'":
            quote = "$'"
            raw.append("$'")
            i += 1
        elif c in _OPERATOR_CHARS or (c == "$" and line[i + 1:i + 2] == "("):
            if c in "<>":
                end_word()
            else:
                end_segment()
        elif c.isspace():
            end_word()
        else:
            raw.append(c)
        i += 1
    end_segment()
    return result


def substitutions(line: str) -> list[str]:
    """The bodies of every ``$(...)``, ``<(...)``, ``>(...)`` and backtick span.

    Quotes are ignored on purpose: ``"$(rm -rf x)"`` runs inside double quotes,
    and a body found inside single quotes is at worst evaluated for nothing.
    """
    spans: list[tuple[int, int]] = []
    opened: list[tuple[int, bool]] = []
    tick = -1
    for i, c in enumerate(line):
        if c == "`":
            if tick < 0:
                tick = i + 1
            else:
                spans.append((tick, i))
                tick = -1
        elif c == "(":
            opened.append((i + 1, i > 0 and line[i - 1] in "$<>"))
        elif c == ")" and opened:
            start, substitutes = opened.pop()
            if substitutes:
                spans.append((start, i))
    if tick >= 0:
        spans.append((tick, len(line)))
    spans += [(start, len(line)) for start, substitutes in opened if substitutes]
    # Outermost bodies only: an inner one is part of its outer body, and is
    # reached when that body is evaluated in turn. Returning every level made
    # `$($($(...)))` cost the depth to the power of the nesting limit.
    outermost: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        if not outermost or start >= outermost[-1][1]:
            outermost.append((start, end))
    return [line[s:e] for s, e in outermost if line[s:e].strip()]
