"""Benchmark metrics.

Exact-field accuracy is the headline number, not CER. A single wrong digit in
`庶三百四十九` scores 0.83 CER-wise and is still a different person — for a
family tree the field is either right or it is not. CER is reported alongside
because it says *how* wrong a backend is when it misses.

False substitutions are tracked separately from insertions and deletions for the
same reason the project brief insists on preserving uncertainty: a dropped
character is visibly missing, while a substituted one is a confident lie.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from foresight_ocr.ocr.fields import FIELDS, parse_entry


@dataclass
class EditCounts:
    substitutions: int = 0
    insertions: int = 0
    deletions: int = 0
    matches: int = 0

    @property
    def total_reference(self) -> int:
        return self.substitutions + self.deletions + self.matches

    @property
    def errors(self) -> int:
        return self.substitutions + self.insertions + self.deletions


def align(reference: str, hypothesis: str) -> EditCounts:
    """Levenshtein alignment with the operation mix retained."""
    r, h = list(reference or ""), list(hypothesis or "")
    n, m = len(r), len(h)
    # dp[i][j] = (cost, sub, ins, del, match)
    dp: list[list[tuple[int, int, int, int, int]]] = [
        [(0, 0, 0, 0, 0)] * (m + 1) for _ in range(n + 1)
    ]
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, 0, i, 0)
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, j, 0, 0)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if r[i - 1] == h[j - 1]:
                c, s, ins, dele, mat = dp[i - 1][j - 1]
                diag = (c, s, ins, dele, mat + 1)
            else:
                c, s, ins, dele, mat = dp[i - 1][j - 1]
                diag = (c + 1, s + 1, ins, dele, mat)
            c, s, ins, dele, mat = dp[i][j - 1]
            insert = (c + 1, s, ins + 1, dele, mat)
            c, s, ins, dele, mat = dp[i - 1][j]
            delete = (c + 1, s, ins, dele + 1, mat)
            dp[i][j] = min(diag, insert, delete, key=lambda t: t[0])

    _, subs, ins, dels, matches = dp[n][m]
    return EditCounts(subs, ins, dels, matches)


@dataclass
class Score:
    backend: str
    variant: str
    context: str
    model_version: str = ""
    samples: int = 0
    produced: int = 0  # crops that yielded any text at all
    cer: float = 0.0
    substitution_rate: float = 0.0
    deletion_rate: float = 0.0
    insertion_rate: float = 0.0
    exact_entry: float = 0.0
    field_exact: dict[str, float] = field(default_factory=dict)
    rare_char_accuracy: float | None = None
    unknown_rate: float = 0.0
    latency_ms_median: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Pair:
    """One gold/hypothesis pair for a single crop."""

    crop_id: str
    reference: str
    hypothesis: str | None
    latency_ms: float | None = None
    error: str | None = None
    # Band the crop came from. Without it the own/parent id split is a guess.
    own_label: str | None = None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def rare_characters(references: Iterable[str], max_count: int = 2) -> set[str]:
    """Characters appearing at most `max_count` times across the gold set.

    On this corpus the common set is numerals and band labels, so what falls out
    as rare is precisely the personal-name characters — the ones a language
    model is most tempted to 'correct' into something more probable.
    """
    counts = Counter(ch for ref in references for ch in ref)
    return {ch for ch, n in counts.items() if n <= max_count}


def score_pairs(
    pairs: Sequence[Pair],
    backend: str,
    variant: str,
    context: str,
    model_version: str = "",
    rare: set[str] | None = None,
) -> Score:
    total = EditCounts()
    exact = 0
    produced = 0
    unknown = 0
    latencies: list[float] = []
    field_hits: dict[str, int] = {f: 0 for f in FIELDS}
    field_total: dict[str, int] = {f: 0 for f in FIELDS}
    rare_total = rare_hits = 0
    rare = rare if rare is not None else set()

    for p in pairs:
        if p.latency_ms is not None:
            latencies.append(p.latency_ms)
        hyp = p.hypothesis
        if hyp is None or p.error:
            unknown += 1
            # An unread crop is a full deletion, not a free pass.
            total.deletions += len(p.reference)
        else:
            produced += 1
            counts = align(p.reference, hyp)
            total.substitutions += counts.substitutions
            total.insertions += counts.insertions
            total.deletions += counts.deletions
            total.matches += counts.matches
            if p.reference == hyp:
                exact += 1

        gold_fields = parse_entry(p.reference, own_label=p.own_label)
        hyp_fields = parse_entry(hyp, own_label=p.own_label)
        for f in FIELDS:
            g = gold_fields.field(f)
            if g is None:
                continue
            field_total[f] += 1
            if hyp_fields.field(f) == g:
                field_hits[f] += 1

        if rare:
            hyp_chars = Counter(hyp or "")
            for ch in p.reference:
                if ch in rare:
                    rare_total += 1
                    if hyp_chars[ch] > 0:
                        hyp_chars[ch] -= 1
                        rare_hits += 1

    ref_chars = max(total.total_reference, 1)
    return Score(
        backend=backend,
        variant=variant,
        context=context,
        model_version=model_version,
        samples=len(pairs),
        produced=produced,
        cer=total.errors / ref_chars,
        substitution_rate=total.substitutions / ref_chars,
        deletion_rate=total.deletions / ref_chars,
        insertion_rate=total.insertions / ref_chars,
        exact_entry=exact / len(pairs) if pairs else 0.0,
        field_exact={
            f: (field_hits[f] / field_total[f] if field_total[f] else float("nan"))
            for f in FIELDS
        },
        rare_char_accuracy=(rare_hits / rare_total) if rare_total else None,
        unknown_rate=unknown / len(pairs) if pairs else 0.0,
        latency_ms_median=_median(latencies),
    )
