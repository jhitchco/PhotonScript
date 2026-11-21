"""No JS string literal in a dashboard template may contain a raw newline:
one such literal is a SyntaxError that kills its whole <script> block
(2026-10-05: the PS-122 badge tooltip and the PS-124 Add alert)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "photonscript" / "scheduler" / "templates"


def _raw_newline_strings(code: str) -> list[int]:
    """Offsets of quote-delimited string literals that contain a raw newline.
    Skips comments, template literals and regex literals after '(' or ','."""
    hits, i, n = [], 0, len(code)
    while i < n:
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        c = code[i]
        if c == "/" and re.search(r"[(,=:!&|?]\s*$", code[max(0, i - 20):i]):
            j = i + 1
            while j < n and code[j] not in "/\n":
                j += 2 if code[j] == "\\" else 1
            i = j + 1
            continue
        if c == "`":
            j = code.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c in "'\"":
            j = i + 1
            while j < n and code[j] != c:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == "\n":
                    hits.append(i)
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return hits


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_no_raw_newline_in_js_strings(path):
    s = path.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.S):
        code = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "0", m.group(1), flags=re.S)
        for off in _raw_newline_strings(code):
            bad.append(s[:m.start(1)].count("\n") + 1)
    assert not bad, f"{path.name}: raw newline in a JS string near script lines {bad}"
