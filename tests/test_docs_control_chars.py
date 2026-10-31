"""Docs must not carry stray control characters.

A Python string like "deploy\focus" once landed in MAINTENANCE.md as a form
feed ("deployocus-model-move.cmd"); this keeps any such escape slip out of
the Markdown docs.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_markdown_docs_have_no_control_characters():
    files = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md",
                                                     ROOT / "CLAUDE.md"]
    bad = []
    for p in files:
        if not p.exists():
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for ch in line:
                if ord(ch) < 32 and ch != "\t" or ord(ch) == 127:
                    bad.append(f"{p.name}:{n}: {ord(ch):#04x}")
    assert not bad, bad
