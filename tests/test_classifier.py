"""Smoke tests for the classifier – run with `python -m tests.test_classifier`."""

from __future__ import annotations

from handshake_agent.classifier import classify
from handshake_agent.labels import HEALTH, MEDIA, SOCIAL, TECH


CASES = [
    (
        "Software Engineering Intern",
        "We are looking for a backend developer with Python, AWS, and SQL experience to help scale our SaaS product.",
        "Computer Science",
        TECH,
    ),
    (
        "Management Consulting Analyst",
        "Entry-level analyst role. Strategy consulting for Fortune 500 clients. Strong Excel and finance skills preferred.",
        "Economics, Business",
        TECH,
    ),
    (
        "Public Health Research Assistant",
        "Assist epidemiology team on clinical trial data. Biology or public health background preferred.",
        "Biology, Public Health",
        HEALTH,
    ),
    (
        "Lab Technician – Biotech Startup",
        "Wet lab work, cell culture, pharmaceutical pipeline support.",
        "",
        HEALTH,
    ),
    (
        "Policy Intern at UN Women",
        "Support advocacy and policy research on gender equality for the United Nations.",
        "Political Science, SRPP",
        SOCIAL,
    ),
    (
        "Human Rights Legal Fellow",
        "NGO work on refugee law and human rights litigation.",
        "Legal Studies",
        SOCIAL,
    ),
    (
        "Graphic Designer",
        "Design social media content, branding and marketing assets. Portfolio required.",
        "Visual Arts, Interactive Media",
        MEDIA,
    ),
    (
        "Content Writer / Journalist",
        "Write editorial pieces, interview artists, and create multimedia storytelling content.",
        "Writing, Communications",
        MEDIA,
    ),
    (
        "Museum Curatorial Assistant",
        "Support exhibitions at a contemporary art museum.",
        "Art History",
        MEDIA,
    ),
    (
        "Data Scientist – Healthcare AI",
        "Build ML models on patient data. Python, pandas, clinical data experience.",
        "Computer Science, Data Science",
        TECH,
    ),
]


def main() -> int:
    failures = 0
    for title, desc, majors, expected in CASES:
        res = classify(title=title, description=desc, majors=majors)
        ok = res.label == expected
        status = "OK " if ok else "FAIL"
        print(f"[{status}] {title!r:50s} → {res.label}  (expected {expected})")
        if not ok:
            failures += 1
            print(f"        score={res.score} runner_up={res.runner_up}={res.runner_up_score}")
            for r in res.reasons:
                print(f"        · {r}")
    print()
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
