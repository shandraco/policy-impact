"""Clause-level diff between two versions of a policy."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .documents import Clause, terms_defined

RENUMBER_MATCH = 0.6  # similarity needed to treat a moved clause as the same clause
# Matching moved clauses compares every unmatched old clause with every unmatched new one. An uploaded file
# can make that huge, so cap the pairs, the text compared per clause, and the time spent. Past a cap the
# remaining clauses are reported as removed and added, which still sends them to the judge.
MAX_RENUMBER_PAIRS = 1_000_000
MATCH_CHARS = 4_000
DIFF_SECONDS = 5.0


@dataclass
class Change:
    number: str
    kind: str  # modified | added | removed | renumbered
    heading: str
    old_text: str
    new_text: str
    terms: list[str] = field(default_factory=list)
    old_number: str | None = None

    @property
    def label(self) -> str:
        title = f" ({self.heading})" if self.heading else ""
        return {
            "modified": f"§{self.number} changed{title}",
            "added": f"§{self.number} was added{title}",
            "removed": f"§{self.number} was removed{title}",
            "renumbered": f"§{self.old_number} is now §{self.number}{title}",
        }[self.kind]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _sort_key(number: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in number.split("~")[0].split("."))


def _change(kind: str, old: Clause | None, new: Clause | None) -> Change:
    old_text = old.text if old else ""
    new_text = new.text if new else ""
    terms = sorted(set(terms_defined(old_text)) | set(terms_defined(new_text)))
    number = new.number if new else old.number  # type: ignore[union-attr]
    old_number = old.number if old and new and old.number != new.number else None
    heading = (new or old).heading  # type: ignore[union-attr]
    return Change(number, kind, heading, old_text, new_text, terms, old_number)


def diff_clauses(old: list[Clause], new: list[Clause], seconds: float = DIFF_SECONDS) -> list[Change]:
    old_by = {c.number: c for c in old}
    new_by = {c.number: c for c in new}
    changes: list[Change] = []

    for c in new:
        o = old_by.get(c.number)
        if o and _norm(o.text) != _norm(c.text):
            changes.append(_change("modified", o, c))

    unmatched_old = [c for c in old if c.number not in new_by]
    unmatched_new = [c for c in new if c.number not in old_by]
    if unmatched_old and unmatched_new and len(unmatched_old) * len(unmatched_new) <= MAX_RENUMBER_PAIRS:
        deadline = time.monotonic() + seconds
        old_norm = {id(o): _norm(o.text)[:MATCH_CHARS] for o in unmatched_old}
        sm = SequenceMatcher()
        for n in list(unmatched_new):
            sm.set_seq2(_norm(n.text)[:MATCH_CHARS])  # seq2 is the side SequenceMatcher indexes, so set it once
            best, score = None, 0.0
            for o in unmatched_old:
                if time.monotonic() > deadline:
                    break
                sm.set_seq1(old_norm[id(o)])
                # real_quick_ratio and quick_ratio are cheap upper bounds on ratio: skip pairs that can't win
                bar = max(RENUMBER_MATCH, score)
                if sm.real_quick_ratio() < bar or sm.quick_ratio() < bar:
                    continue
                s = sm.ratio()
                if s > score:
                    best, score = o, s
            if best is not None and score >= RENUMBER_MATCH:
                unmatched_old.remove(best)
                unmatched_new.remove(n)
                same = _norm(best.text) == _norm(n.text)
                changes.append(_change("renumbered" if same else "modified", best, n))

    changes += [_change("added", None, n) for n in unmatched_new]
    changes += [_change("removed", o, None) for o in unmatched_old]
    return sorted(changes, key=lambda ch: _sort_key(ch.number))
