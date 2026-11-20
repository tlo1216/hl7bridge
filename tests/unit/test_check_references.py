"""tools/check_references.py is neither blind nor self-matching.

The planted words are assembled from pieces at run time, so this test file
does not itself hold any of the strings the check looks for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "check_references.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_references", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_references"] = module
    spec.loader.exec_module(module)
    return module


check = _load()

# One planted sample per pattern, built from pieces.
PLANTED = {
    "planning-note numbering": "see " + "pro" + "ject 4 for details",
    "planning-note rule numbering": "per " + "engin" + "eering rule 3",
    "planning brief": "specs in the " + "pro" + "ject brief",
    "client name": "the " + "SI" + "IM server",
    "assistant name": "written by " + "Cla" + "ude",
    "assistant vendor": "noreply@" + "anthro" + "pic.com",
    "review tool name": "ask " + "Co" + "dex",
    "review agent name": "a " + "Fa" + "ble review",
    "agent wording": "an adversarial " + "sub" + "-agent",
    "attribution trailer": "Co-" + "Authored-By: someone",
}


def test_every_pattern_has_a_planted_sample():
    assert set(PLANTED) == set(check.PATTERNS)


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_each_planted_word_fails_the_scan(tmp_path, name):
    (tmp_path / "doc.md").write_text(f"intro\n{PLANTED[name]}\n", encoding="utf-8")
    hits = check.scan_tree(tmp_path)
    assert [(h.where, h.line, h.pattern) for h in hits] == [("doc.md", 2, name)]
    assert check.main(["--root", str(tmp_path)]) == 1


def test_clean_tree_passes(tmp_path):
    (tmp_path / "ok.py").write_text("# fail loudly; one explicit ACK per message\n", "utf-8")
    assert check.scan_tree(tmp_path) == []
    assert check.main(["--root", str(tmp_path)]) == 0


def test_script_does_not_match_itself():
    """Scan the script's own text directly, without the self-path skip."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert list(check.scan_text(text, "tools/check_references.py")) == []


def test_binary_files_are_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0\0" + PLANTED["client name"].encode())
    assert check.scan_tree(tmp_path) == []


def test_allow_list_starts_empty():
    assert frozenset() == check.ALLOW


def test_repository_tree_is_clean():
    """The real tree: the same check CI runs."""
    hits = check.scan_tree(SCRIPT.parents[1])
    assert [str(h) for h in hits] == []
