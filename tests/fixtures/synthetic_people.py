"""Deterministic synthetic population for scripts/demo_conflict.py. ALL SYNTHETIC: ids are P001.., no names.

Seeded, so every run and every machine gets the same 50 people. Each person carries every fact the five `member`
rules need (so no evaluation can stop on a missing fact), and coverage_end_date is either null or not before
coverage_start_date. Never loaded by the API.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

DEFAULT_SEED = 20260920
MONTH_START, MONTH_END = date(2026, 9, 1), date(2026, 9, 30)
DEMO_DATE = date(2026, 9, 15)

# Coverage shapes relative to the September 2026 reporting month and the 15th (the demo date), with their weights.
KINDS = {
    "continuing": 24,  # covered through the demo date and the month
    "lapsed": 10,  # coverage ended before the month
    "ended_this_month": 5,  # ended between 1 and 14 September: in the month, not on the 15th
    "starts_later_this_month": 5,  # starts between 16 and 30 September
    "future": 6,  # coverage starts after September
}


def _day(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _person(rng: random.Random, number: int) -> dict[str, Any]:
    kind = rng.choices(list(KINDS), weights=list(KINDS.values()))[0]
    if kind == "continuing":
        start = _day(rng, date(2023, 1, 1), DEMO_DATE)
        end = None if rng.random() < 0.8 else _day(rng, date(2026, 10, 1), date(2027, 12, 31))
        status = "eligible" if rng.random() < 0.9 else "suspended"
    elif kind == "lapsed":
        start = _day(rng, date(2023, 1, 1), date(2026, 6, 30))
        end = min(start + timedelta(days=rng.randint(30, 900)), MONTH_START - timedelta(days=1))
        status = "eligible" if rng.random() < 0.3 else "ineligible"  # some customer-service snapshots are stale
    elif kind == "ended_this_month":
        start = _day(rng, date(2024, 1, 1), date(2026, 8, 31))
        end = _day(rng, MONTH_START, DEMO_DATE - timedelta(days=1))
        status = "eligible" if rng.random() < 0.4 else "ineligible"
    elif kind == "starts_later_this_month":
        start, end, status = _day(rng, DEMO_DATE + timedelta(days=1), MONTH_END), None, "pending"
    else:
        start, end, status = _day(rng, date(2026, 10, 1), date(2026, 12, 31)), None, "pending"

    registration = start - timedelta(days=rng.randint(0, 120))
    # Claims are common but not universal among people who have had coverage; nobody else has any.
    has_claims = kind in ("continuing", "lapsed", "ended_this_month") and rng.random() < 0.6
    return {
        "id": f"P{number:03d}",
        "facts": {
            "registration_date": registration.isoformat(),
            "coverage_start_date": start.isoformat(),
            "coverage_end_date": end.isoformat() if end else None,
            "claim_count_to_date": rng.randint(1, 9) if has_claims else 0,
            "reporting_month_start": MONTH_START.isoformat(),
            "reporting_month_end": MONTH_END.isoformat(),
            "eligibility_status": status,
            "network_status": rng.choice(["in_network", "out_of_network"]),
            "follow_up_eligible": rng.random() < 0.5,
            "risk_flag": rng.choice(["low", "medium", "high"]),
        },
    }


def generate(n: int = 50, seed: int = DEFAULT_SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [_person(rng, number) for number in range(1, n + 1)]
