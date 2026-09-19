"""The eval runner and the demo script: exact outcomes, a failing eval exits non-zero, and the demo fixture is
deterministic, complete and never reachable through the API.
"""
import subprocess
import sys
from pathlib import Path

import yaml

from app.catalog import load_catalog
from app.main import DEFAULT_CATALOG_PATH
from app.rules import required_facts
from scripts.demo_conflict import MEMBER_DEFINITIONS, counts, examples, membership
from tests.fixtures.synthetic_people import DEMO_DATE, generate

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "ambiguous_questions.yaml"


def run_module(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, timeout=180)


# --- Eval -------------------------------------------------------------------------------------------------------


def test_eval_cases_cover_the_brief():
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    assert 10 <= len(cases) <= 15
    assert len({c["id"] for c in cases}) == len(cases)
    required = {"id", "question", "term", "role", "as_of", "expected_status", "expected_concepts", "rationale"}
    assert all(required <= set(c) for c in cases)
    assert [c["id"] for c in cases if c.get("mcp")] == ["1-member-no-context", "2-member-enrollment", "7a-follow-up-as-analyst"]


def test_eval_passes_and_runs_core_cases_through_mcp():
    result = run_module("evals.run")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == "16/16 checks passed (13 cases; 3 also via MCP)"
    assert sum(1 for line in lines if line.split()[1:2] == ["mcp"] and line.endswith("PASS")) == 3


def test_eval_exits_non_zero_on_a_failing_case(tmp_path):
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    wrong = dict(cases[1], expected_concepts=["registered_member"])  # "member" + enrollment is active_member
    bad = tmp_path / "cases.yaml"
    bad.write_text(yaml.safe_dump({"cases": [wrong]}), encoding="utf-8")
    result = run_module("evals.run", "--cases", str(bad))
    assert result.returncode == 1
    assert "FAIL: concepts ['active_member'] != ['registered_member']" in result.stdout
    assert result.stdout.splitlines()[-1] == "0/2 checks passed (1 cases; 1 also via MCP)"


# --- Demo fixture -----------------------------------------------------------------------------------------------


def test_fixture_is_deterministic_and_seeded():
    assert generate() == generate()
    assert generate(seed=1) != generate()
    assert [p["id"] for p in generate()] == [f"P{i:03d}" for i in range(1, 51)]


def test_every_person_carries_every_fact_and_valid_coverage():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    needed = set().union(*(required_facts(catalog, d, DEMO_DATE) for d in MEMBER_DEFINITIONS))
    for person in generate():
        facts = person["facts"]
        assert set(facts) == set(catalog.facts)  # every fact in the dictionary, so no rule can hit a missing fact
        assert needed <= set(facts)
        end = facts["coverage_end_date"]
        assert end is None or end >= facts["coverage_start_date"]


# --- Demo output ------------------------------------------------------------------------------------------------


def test_demo_counts_differ_per_definition():
    result_counts = counts(membership(generate()))
    assert result_counts == {
        "registered_member": 47, "active_member": 24, "claimant_member": 22,
        "reporting_month_member": 32, "currently_eligible_member": 23,
    }
    assert len(set(result_counts.values())) == 5


def test_demo_examples_are_explained_from_the_facts():
    people = generate()
    assert examples(people, membership(people)) == [
        ("P002", "reporting_month_member", "active_member", "enrolled during the reporting month, but coverage ended 2026-09-08"),
        ("P033", "currently_eligible_member", "active_member", "customer service still shows 'eligible', but coverage ended 2026-09-05"),
        ("P001", "registered_member", "active_member", "registered 2024-04-14, but coverage ended 2025-01-03"),
    ]


def test_demo_script_runs():
    result = run_module("scripts.demo_conflict")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[3:8] == [
        "registered_member            crm                   47",
        "active_member                enrollment            24",
        "claimant_member              claims                22",
        "reporting_month_member       analytics             32",
        "currently_eligible_member    customer_service      23",
    ]
    assert len([line for line in lines if line.startswith("  P")]) == 3


def test_population_is_never_reachable_through_the_api():
    # No app module refers to the fixture or the demo, so no route can serve either.
    for path in (ROOT / "app").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "synthetic_people" not in text and "demo_conflict" not in text and "tests." not in text, path.name


def test_every_eval_expectation_is_checked():
    # One wrong expectation at a time: each must produce exactly its own failure message.
    from evals.run import failures

    observed = {"status": "resolved", "concepts": ["reporting_month_member"], "version": "2.0.0",
                "suggestions": ["member"], "restricted_count": 1, "warnings": ["'x' is deprecated; use 'y' instead"]}
    base = {"expected_status": "resolved", "expected_concepts": ["reporting_month_member"]}
    assert failures(base, observed) == []
    cases = [
        ({"expected_status": "ambiguous"}, "status 'resolved' != 'ambiguous'"),
        ({"expected_concepts": ["active_member"]}, "concepts ['reporting_month_member'] != ['active_member']"),
        ({"expected_version": "1.0.0"}, "version '2.0.0' != '1.0.0'"),
        ({"expected_suggestions": []}, "suggestions ['member'] != []"),
        ({"expected_restricted_count": 0}, "restricted_count 1 != 0"),
        ({"expected_warning": "matched none"}, "no warning containing 'matched none'"),
    ]
    for change, message in cases:
        assert failures({**base, **change}, observed) == [message]
