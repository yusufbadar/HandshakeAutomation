"""Rule-based classifier for the four NYUAD Career Pathway labels.

Given a job posting's title, description, and (optionally) required/preferred
majors, pick exactly ONE label out of the four Career Pathway labels.

Strategy (mirrors the guide in the PDF):

1. If we know required/preferred majors, those are the strongest signal – each
   major maps to one pathway. The pathway with the most matching majors wins.
2. Otherwise, score each pathway by how many of its keywords appear in the
   combined title + description text, weighting title matches higher.
3. Break ties with a fixed priority order (majors > keywords, and
   Tech/Business > Health > Social > Media as a fallback).
4. Return both the chosen label and a confidence score + reasoning so the
   operator can double-check low-confidence calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .labels import ALL_LABELS, PATHWAYS, Pathway


TITLE_WEIGHT = 3
MAJOR_WEIGHT = 5
DESCRIPTION_WEIGHT = 1


PRIORITY_ORDER = {
    "NYUAD Technology, Business, and Innovation": 0,
    "NYUAD Health, Science, and Engineering": 1,
    "NYUAD Social Impact and Global Affairs": 2,
    "NYUAD Media, Arts, and Communications": 3,
}


@dataclass
class Classification:
    label: str
    score: int
    reasons: list[str]
    runner_up: str | None
    runner_up_score: int

    @property
    def confident(self) -> bool:
        if self.score <= 0:
            return False
        if self.runner_up is None:
            return True
        return self.score >= self.runner_up_score + 2


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _count_matches(text: str, needles: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for needle in needles:
        n = needle.lower().strip()
        if not n:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            hits.append(n)
    return hits


def _score_pathway(
    pathway: Pathway,
    title: str,
    description: str,
    majors_text: str,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0

    major_hits = _count_matches(majors_text, pathway.majors) if majors_text else []
    if major_hits:
        score += MAJOR_WEIGHT * len(major_hits)
        reasons.append(f"majors: {', '.join(major_hits)}")

    title_hits = _count_matches(title, pathway.keywords)
    if title_hits:
        score += TITLE_WEIGHT * len(title_hits)
        reasons.append(f"title: {', '.join(title_hits)}")

    desc_hits = _count_matches(description, pathway.keywords)
    if desc_hits:
        score += DESCRIPTION_WEIGHT * len(desc_hits)
        shown = desc_hits[:6]
        suffix = "..." if len(desc_hits) > len(shown) else ""
        reasons.append(f"description: {', '.join(shown)}{suffix}")

    desc_major_hits = _count_matches(description, pathway.majors) if description else []
    if desc_major_hits:
        score += 1 * len(desc_major_hits)
        reasons.append(f"desc-majors: {', '.join(desc_major_hits)}")

    return score, reasons


def classify(
    title: str,
    description: str = "",
    majors: str | list[str] | None = None,
) -> Classification:
    """Pick the single best Career Pathway label for a job posting."""
    title_n = _normalise(title)
    description_n = _normalise(description)
    if isinstance(majors, list):
        majors_n = _normalise(", ".join(majors))
    else:
        majors_n = _normalise(majors or "")

    scored: list[tuple[int, Pathway, list[str]]] = []
    for pathway in PATHWAYS:
        s, reasons = _score_pathway(pathway, title_n, description_n, majors_n)
        scored.append((s, pathway, reasons))

    scored.sort(
        key=lambda item: (-item[0], PRIORITY_ORDER.get(item[1].label, 99))
    )

    top_score, top_pathway, top_reasons = scored[0]
    runner_up_score, runner_up_pathway, _ = scored[1]

    if top_score == 0:
        top_pathway = next(p for p in PATHWAYS if p.label == ALL_LABELS[3])
        top_reasons = ["no keyword hits – falling back to Technology, Business & Innovation"]

    return Classification(
        label=top_pathway.label,
        score=top_score,
        reasons=top_reasons,
        runner_up=runner_up_pathway.label if runner_up_score > 0 else None,
        runner_up_score=runner_up_score,
    )
