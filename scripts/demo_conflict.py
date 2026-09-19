"""Same word, different numbers: evaluate the five approved `member` definitions over one synthetic population.

Run: `python -m scripts.demo_conflict`. Uses the rule library directly on a seeded fixture (tests/fixtures), never the
API: the service must not accept or return populations. The counts differ because the definitions differ, which is
the problem the glossary exists to make explicit.
"""
from __future__ import annotations

from typing import Any

from app.catalog import load_catalog
from app.main import DEFAULT_CATALOG_PATH
from app.rules import evaluate
from tests.fixtures.synthetic_people import DEMO_DATE, generate

MEMBER_DEFINITIONS = [
    "registered_member", "active_member", "claimant_member", "reporting_month_member", "currently_eligible_member",
]


def membership(people: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """person id -> {definition id -> result} on DEMO_DATE. Every rule comes from the catalog; none is written here."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    return {p["id"]: {d: evaluate(catalog, d, DEMO_DATE, p["facts"]).result for d in MEMBER_DEFINITIONS} for p in people}


def counts(results: dict[str, dict[str, bool]]) -> dict[str, int]:
    return {d: sum(r[d] for r in results.values()) for d in MEMBER_DEFINITIONS}


def _not_covered(facts: dict[str, Any]) -> str:
    """Why active_member is false, read from the facts (not assumed from how the person was generated)."""
    if facts["coverage_start_date"] > DEMO_DATE.isoformat():
        return f"coverage only starts {facts['coverage_start_date']}"
    return f"coverage ended {facts['coverage_end_date']}"


def examples(people: list[dict[str, Any]], results: dict[str, dict[str, bool]]) -> list[tuple[str, str, str, str]]:
    """Up to three different people who are a member under one definition and not under active_member."""
    patterns = [
        ("reporting_month_member", lambda f: f"enrolled during the reporting month, but {_not_covered(f)}"),
        ("currently_eligible_member", lambda f: f"customer service still shows 'eligible', but {_not_covered(f)}"),
        ("registered_member", lambda f: f"registered {f['registration_date']}, but {_not_covered(f)}"),
    ]
    found, used = [], set()
    for member_of, reason in patterns:
        match = next((p for p in people if p["id"] not in used
                      and results[p["id"]][member_of] and not results[p["id"]]["active_member"]), None)
        if match is not None:
            used.add(match["id"])
            found.append((match["id"], member_of, "active_member", reason(match["facts"])))
    return found


def main() -> None:
    people = generate()
    results = membership(people)
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    print(f"'member' on {DEMO_DATE} across {len(people)} synthetic people (seeded, tests/fixtures/synthetic_people.py)\n")
    print(f"{'definition':28} {'system':18} {'count':>5}")
    for definition, count in counts(results).items():
        system = catalog.effective(definition, DEMO_DATE).context.system
        print(f"{definition:28} {system:18} {count:>5}")
    print("\nSame person, different answer:")
    for person_id, member_of, not_member_of, reason in examples(people, results):
        print(f"  {person_id}: {member_of} yes, {not_member_of} no: {reason}")


if __name__ == "__main__":
    main()
