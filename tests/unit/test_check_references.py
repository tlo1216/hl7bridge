"""tools/check_references.py is neither blind nor self-matching.

The real rules are salted digests, so this file holds no real samples. The
matching is proved on stand-in words hashed the same way. Real samples live
in a local file outside the repository; set HL7BRIDGE_REFCHECK_SAMPLES to
its path (one ``<rule id><TAB><sample line>`` per line) to prove every real
rule catches its sample.
"""

from __future__ import annotations

import importlib.util
import os
import re
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

# Stand-in rules: the same tokenizer and digest, on harmless words.
STAND_IN_RULES = {
    "T01": frozenset({check.digest("zebra")}),
    "T02": frozenset({check.digest("quux #")}),
    "T03": frozenset({check.digest("alpha beta gamma")}),
}
STAND_IN_SAMPLES = {
    "T01": "a ZEBRA crossing",
    "T02": "see quux 42 for details",
    "T03": "the Alpha-Beta-Gamma trailer",
}


@pytest.fixture
def stand_in(monkeypatch):
    monkeypatch.setattr(check, "RULES", STAND_IN_RULES)


@pytest.mark.usefixtures("stand_in")
@pytest.mark.parametrize("rule", sorted(STAND_IN_SAMPLES))
def test_each_stand_in_sample_fails_the_scan(tmp_path, rule):
    (tmp_path / "doc.md").write_text(f"intro\n{STAND_IN_SAMPLES[rule]}\n", encoding="utf-8")
    hits = check.scan_tree(tmp_path)
    assert [(h.where, h.line, h.pattern) for h in hits] == [("doc.md", 2, rule)]
    assert check.main(["--root", str(tmp_path)]) == 1


@pytest.mark.usefixtures("stand_in")
def test_near_misses_pass(tmp_path):
    (tmp_path / "ok.md").write_text("zebras\nquux\nquux four\nalpha beta\n", encoding="utf-8")
    assert check.scan_tree(tmp_path) == []


def test_tokens_fold_case_split_punctuation_and_mask_digits():
    assert check.tokens("Co-Op 2026: A/B") == ["co", "op", "#", "a", "b"]


def test_rules_hold_digests_only():
    assert check.RULES, "no rules: the check would pass anything"
    for rule, digests in check.RULES.items():
        assert re.fullmatch(r"R\d\d", rule)
        assert digests
        for d in digests:
            assert re.fullmatch(rf"[0-9a-f]{{{check.DIGEST_CHARS}}}", d)


def test_common_words_never_match():
    """Guards against a rule that would flag ordinary project text."""
    text = "the HL7 bridge sends one explicit ACK per message and fails loudly\n" * 2
    assert list(check.scan_text(text, "x")) == []


def test_clean_tree_passes(tmp_path):
    (tmp_path / "ok.py").write_text("# fail loudly; one explicit ACK per message\n", "utf-8")
    assert check.scan_tree(tmp_path) == []
    assert check.main(["--root", str(tmp_path)]) == 0


def test_script_does_not_match_itself():
    """Scan the script's own text directly, without the self-path skip."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert list(check.scan_text(text, "tools/check_references.py")) == []


@pytest.mark.usefixtures("stand_in")
def test_binary_files_are_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0\0" + STAND_IN_SAMPLES["T01"].encode())
    assert check.scan_tree(tmp_path) == []


def test_allow_list_starts_empty():
    assert frozenset() == check.ALLOW


def test_repository_tree_is_clean():
    """The real tree: the same check CI runs."""
    hits = check.scan_tree(SCRIPT.parents[1])
    assert [str(h) for h in hits] == []


SAMPLES = os.environ.get("HL7BRIDGE_REFCHECK_SAMPLES")


@pytest.mark.skipif(not SAMPLES, reason="HL7BRIDGE_REFCHECK_SAMPLES not set (local only)")
def test_every_real_rule_catches_its_local_sample(tmp_path):
    assert SAMPLES is not None
    lines = Path(SAMPLES).read_text(encoding="utf-8").splitlines()
    samples = dict(line.split("\t", 1) for line in lines if line.strip())
    assert set(samples) == set(check.RULES), "one sample per rule"
    for rule, sample in sorted(samples.items()):
        hits = list(check.scan_text(sample, "sample"))
        assert [h.pattern for h in hits] == [rule], sample
