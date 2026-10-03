"""Relative links in the repo's Markdown resolve to files that exist.

The RFC, its summary and the evidence set point at each other by path, and a
renamed page breaks those links without failing anything else. This does not
check ``#anchors`` or ``§`` numbers, only that the target is there. Links that
leave the repository (the sibling card checkout) are skipped: CI has no sibling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = sorted(
    [*REPO.glob("*.md"), *(REPO / "docs").rglob("*.md")],
    key=lambda p: p.as_posix(),
)
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
EXTERNAL = ("http://", "https://", "mailto:", "#")


def _relative_targets(doc: Path) -> list[str]:
    text = FENCE.sub("", doc.read_text(encoding="utf-8"))
    return [t for t in LINK.findall(text) if not t.startswith(EXTERNAL)]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_relative_links_resolve(doc: Path) -> None:
    missing = []
    for target in _relative_targets(doc):
        path = (doc.parent / target.split("#", 1)[0]).resolve()
        if path.is_relative_to(REPO) and not path.exists():
            missing.append(target)
    assert missing == []
