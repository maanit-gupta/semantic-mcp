"""Rule evaluator tests: boundaries, every op and combinator, missing vs null (incl. short-circuit), bad facts,
version selection and dependency handling. Structural expression errors and cycles are load-time checks and are
tested in test_catalog.py.
"""
from datetime import date, datetime, timedelta

import pytest

from app.catalog import load_catalog, parse_catalog
from app.main import DEFAULT_CATALOG_PATH
from app.rules import (
    ConceptNotEffective,
    ConceptNotFound,
    InsufficientContext,
    InvalidFacts,
    NoRule,
    evaluate,
    required_facts,
)
from tests.test_catalog import concept, fixture

SEED = load_catalog(DEFAULT_CATALOG_PATH)
D = date(2026, 9, 19)
DAY = timedelta(days=1)


def seed(concept_id: str, requested_date: date = D, **facts) -> bool:
    return evaluate(SEED, concept_id, requested_date, facts).result


def rule_error(exc_type, catalog, concept_id: str, requested_date: date, facts: dict):
    with pytest.raises(exc_type) as excinfo:
        evaluate(catalog, concept_id, requested_date, facts)
    return excinfo.value


# --- Single-expression catalog for op/combinator tests ---------------------------------------------------------

FACTS = {
    "d": {"type": "date", "nullable": True},
    "n": {"type": "integer", "nullable": True},
    "s": {"type": "string", "nullable": True},
    "b": {"type": "boolean"},
    "x": {"type": "string"},
}


def single(expression) -> object:
    return parse_catalog(
        {
            "facts": FACTS,
            "concepts": [
                {
                    "id": "c",
                    "term": "c",
                    "name": "C",
                    "context": {"system": "crm", "domain": "test"},
                    "definition": "test",
                    "authoritative_source": {"system": "s", "dataset": "d"},
                    "rule": {"text": "test", "expression": expression},
                    "owner": "team",
                    "version": "1.0.0",
                    "status": "approved",
                    "effective_from": date(2020, 1, 1),
                    "allowed_roles": ["analyst"],
                }
            ],
        }
    )


def run(expression, **facts) -> bool:
    return evaluate(single(expression), "c", D, facts).result


T = {"fact": "b", "op": "eq", "value": True}
F = {"fact": "b", "op": "eq", "value": False}


# --- active_member boundaries (brief §9) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (D, None, True),  # start == requested
        (D - 30 * DAY, D, True),  # end == requested
        (D - 30 * DAY, None, True),  # end null (open-ended)
        (D + DAY, None, False),  # start after requested
        (D - 30 * DAY, D - DAY, False),  # end before requested
        (D - DAY, D + DAY, True),
    ],
)
def test_active_member_boundaries(start, end, expected):
    assert seed("active_member", coverage_start_date=start.isoformat(),
                coverage_end_date=end.isoformat() if end else None) is expected


# --- Every op ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node", "facts", "expected"),
    [
        ({"fact": "n", "op": "eq", "value": 3}, {"n": 3}, True),
        ({"fact": "n", "op": "eq", "value": 3}, {"n": 4}, False),
        ({"fact": "n", "op": "ne", "value": 3}, {"n": 4}, True),
        ({"fact": "n", "op": "ne", "value": 3}, {"n": 3}, False),
        ({"fact": "n", "op": "lt", "value": 3}, {"n": 2}, True),
        ({"fact": "n", "op": "lt", "value": 3}, {"n": 3}, False),
        ({"fact": "n", "op": "lte", "value": 3}, {"n": 3}, True),
        ({"fact": "n", "op": "lte", "value": 3}, {"n": 4}, False),
        ({"fact": "n", "op": "gt", "value": 3}, {"n": 4}, True),
        ({"fact": "n", "op": "gt", "value": 3}, {"n": 3}, False),
        ({"fact": "n", "op": "gte", "value": 3}, {"n": 3}, True),
        ({"fact": "n", "op": "gte", "value": 3}, {"n": 2}, False),
        ({"fact": "s", "op": "in", "value": ["a", "b"]}, {"s": "b"}, True),
        ({"fact": "s", "op": "in", "value": ["a", "b"]}, {"s": "c"}, False),
        ({"fact": "n", "op": "in", "value": [1, 2]}, {"n": 2}, True),
        ({"fact": "d", "op": "is_null"}, {"d": None}, True),
        ({"fact": "d", "op": "is_null"}, {"d": "2026-01-01"}, False),
        ({"fact": "d", "op": "not_null"}, {"d": "2026-01-01"}, True),
        ({"fact": "d", "op": "not_null"}, {"d": None}, False),
        ({"fact": "b", "op": "eq", "value": True}, {"b": True}, True),
        ({"fact": "b", "op": "ne", "value": True}, {"b": True}, False),
        ({"fact": "x", "op": "eq", "value": "in_network"}, {"x": "in_network"}, True),
        ({"fact": "x", "op": "eq", "value": "in_network"}, {"x": "IN_NETWORK"}, False),  # exact, case-sensitive
        ({"fact": "d", "op": "eq", "value": date(2026, 9, 19)}, {"d": "2026-09-19"}, True),
        ({"fact": "d", "op": "lte", "value": {"ref": "requested_date"}}, {"d": "2026-09-19"}, True),
        ({"fact": "d", "op": "lt", "value": {"ref": "requested_date"}}, {"d": "2026-09-19"}, False),
        ({"fact": "d", "op": "gte", "value": {"ref": "requested_date"}}, {"d": "2026-09-19"}, True),
        ({"fact": "d", "op": "gt", "value": {"ref": "requested_date"}}, {"d": "2026-09-19"}, False),
    ],
)
def test_every_op(node, facts, expected):
    assert run(node, **facts) is expected


def test_fact_operand_compares_two_facts():
    catalog = single({"fact": "n", "op": "lte", "value": {"fact": "n"}})
    assert evaluate(catalog, "c", D, {"n": 5}).result is True


# --- Null semantics (D8) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["eq", "ne", "lt", "lte", "gt", "gte"])
def test_comparison_with_null_is_false(op):
    assert run({"fact": "n", "op": op, "value": 3}, n=None) is False


def test_in_with_null_is_false():
    assert run({"fact": "s", "op": "in", "value": ["a"]}, s=None) is False


def test_not_over_null_comparison_is_true_documented_trap():
    # Known SQL-style trap (PROGRESS.md D8): the seed rules never negate a comparison on a nullable fact.
    assert run({"not": {"fact": "n", "op": "eq", "value": 3}}, n=None) is True


# --- Combinators ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ({"all": [T, T]}, True),
        ({"all": [T, F]}, False),
        ({"all": [F, T]}, False),
        ({"any": [F, F]}, False),
        ({"any": [F, T]}, True),
        ({"any": [T, F]}, True),
        ({"not": T}, False),
        ({"not": F}, True),
        ({"not": {"all": [T, {"any": [F, {"not": F}]}]}}, False),
    ],
)
def test_combinators(expression, expected):
    assert run(expression, b=True) is expected


# --- Missing vs null (the prototype's mistake 3) ----------------------------------------------------------------


def test_absent_end_date_is_missing():
    err = rule_error(InsufficientContext, SEED, "active_member", D, {"coverage_start_date": "2026-01-01"})
    assert err.missing_facts == ["coverage_end_date"]


def test_present_null_end_date_is_a_value():
    assert seed("active_member", coverage_start_date="2026-01-01", coverage_end_date=None) is True


def test_all_short_circuit_cannot_hide_missing_fact():
    # start is after requested_date, so a naive `all` would stop at the first clause and return False.
    err = rule_error(InsufficientContext, SEED, "active_member", D, {"coverage_start_date": "2027-01-01"})
    assert err.missing_facts == ["coverage_end_date"]


def test_any_short_circuit_cannot_hide_missing_fact():
    catalog = single({"any": [T, {"fact": "x", "op": "eq", "value": "y"}]})
    err = rule_error(InsufficientContext, catalog, "c", D, {"b": True})  # any(True, ...) would stop early
    assert err.missing_facts == ["x"]


def test_not_short_circuit_cannot_hide_missing_fact():
    catalog = single({"not": {"all": [F, {"fact": "x", "op": "eq", "value": "y"}]}})
    err = rule_error(InsufficientContext, catalog, "c", D, {"b": True})
    assert err.missing_facts == ["x"]


def test_dependency_facts_are_required_even_when_short_circuited():
    # network_status is out_of_network, so `all` would never reach {concept: active_member}.
    err = rule_error(InsufficientContext, SEED, "member_in_network", D, {"network_status": "out_of_network"})
    assert err.missing_facts == ["coverage_end_date", "coverage_start_date"]


def test_all_missing_facts_are_reported_sorted():
    err = rule_error(InsufficientContext, SEED, "eligible_for_follow_up", D, {})
    assert err.missing_facts == ["coverage_end_date", "coverage_start_date", "follow_up_eligible", "risk_flag"]


def test_null_on_a_non_nullable_fact_is_invalid_not_missing():
    err = rule_error(InvalidFacts, SEED, "active_member", D, {"coverage_start_date": None, "coverage_end_date": None})
    assert err.errors == {"coverage_start_date": "must not be null"}


def test_extra_facts_are_ignored():
    assert seed("claimant_member", claim_count_to_date=2, unrelated="anything", risk_flag=None) is True


@pytest.mark.parametrize(
    ("concept_id", "on", "expected"),
    [
        ("registered_member", D, {"registration_date"}),
        ("active_member", D, {"coverage_start_date", "coverage_end_date"}),
        ("claimant_member", D, {"claim_count_to_date"}),
        ("reporting_month_member", date(2025, 6, 1), {"coverage_start_date", "coverage_end_date", "reporting_month_start"}),
        ("reporting_month_member", D, {"coverage_start_date", "coverage_end_date", "reporting_month_start", "reporting_month_end"}),
        ("currently_eligible_member", D, {"eligibility_status"}),
        ("member_in_network", D, {"network_status", "coverage_start_date", "coverage_end_date"}),
        ("eligible_for_follow_up", D, {"follow_up_eligible", "risk_flag", "coverage_start_date", "coverage_end_date"}),
        ("covered_life", D, {"coverage_start_date"}),
    ],
)
def test_required_facts_are_derived_statically(concept_id, on, expected):
    assert required_facts(SEED, concept_id, on) == frozenset(expected)


# --- Invalid facts ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["2026/01/01", "20260101", "2026-1-1", "2026-02-30", "", "٢٠٢٦-٠١-٠١", "2026-01-01T00:00:00", 20260101,
     datetime(2026, 1, 1), True, ["2026-01-01"], {"y": 2026}],
)
def test_bad_date_is_invalid(bad):
    err = rule_error(InvalidFacts, SEED, "active_member", D, {"coverage_start_date": bad, "coverage_end_date": None})
    assert err.errors == {"coverage_start_date": "expected a date as YYYY-MM-DD"}


def test_date_object_is_accepted():
    assert seed("active_member", coverage_start_date=date(2026, 1, 1), coverage_end_date=None) is True


@pytest.mark.parametrize(
    ("concept_id", "fact", "bad", "message"),
    [
        ("claimant_member", "claim_count_to_date", "3", "expected an integer"),
        ("claimant_member", "claim_count_to_date", True, "expected an integer"),
        ("claimant_member", "claim_count_to_date", 3.0, "expected an integer"),
        ("currently_eligible_member", "eligibility_status", 5, "expected a string"),
        ("currently_eligible_member", "eligibility_status", ["eligible"], "expected a string"),
    ],
)
def test_wrong_fact_type_is_invalid(concept_id, fact, bad, message):
    err = rule_error(InvalidFacts, SEED, concept_id, D, {fact: bad})
    assert err.errors == {fact: message}


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_bad_boolean_is_invalid(bad):
    facts = {"coverage_start_date": "2026-01-01", "coverage_end_date": None, "follow_up_eligible": bad, "risk_flag": "low"}
    err = rule_error(InvalidFacts, SEED, "eligible_for_follow_up", D, facts)
    assert err.errors == {"follow_up_eligible": "must not be null" if bad is None else "expected a boolean"}


def test_all_invalid_facts_are_reported_together():
    facts = {"coverage_start_date": "bad", "coverage_end_date": None, "follow_up_eligible": "yes", "risk_flag": 7}
    err = rule_error(InvalidFacts, SEED, "eligible_for_follow_up", D, facts)
    assert err.errors == {
        "coverage_start_date": "expected a date as YYYY-MM-DD",
        "follow_up_eligible": "expected a boolean",
        "risk_flag": "expected a string",
    }


def test_coverage_end_before_start_is_invalid():
    err = rule_error(InvalidFacts, SEED, "active_member", D, {"coverage_start_date": "2026-05-01", "coverage_end_date": "2026-04-30"})
    assert err.errors == {"coverage_end_date": "must not be before coverage_start_date"}


def test_coverage_end_equal_to_start_is_valid():
    assert seed("active_member", coverage_start_date=D.isoformat(), coverage_end_date=D.isoformat()) is True


def test_reporting_month_end_before_start_is_invalid():
    facts = {"coverage_start_date": "2026-01-01", "coverage_end_date": None,
             "reporting_month_start": "2026-06-30", "reporting_month_end": "2026-06-01"}
    err = rule_error(InvalidFacts, SEED, "reporting_month_member", D, facts)
    assert err.errors == {"reporting_month_end": "must not be before reporting_month_start"}


# --- The seed rules ---------------------------------------------------------------------------------------------


def test_registered_member():
    assert seed("registered_member", registration_date=D.isoformat()) is True
    assert seed("registered_member", registration_date=(D + DAY).isoformat()) is False


def test_claimant_member():
    assert seed("claimant_member", claim_count_to_date=1) is True
    assert seed("claimant_member", claim_count_to_date=0) is False


def test_currently_eligible_member():
    assert seed("currently_eligible_member", eligibility_status="eligible") is True
    assert seed("currently_eligible_member", eligibility_status="ineligible") is False


ACTIVE = {"coverage_start_date": "2026-01-01", "coverage_end_date": None}
LAPSED = {"coverage_start_date": "2025-01-01", "coverage_end_date": "2025-12-31"}


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({**ACTIVE, "network_status": "in_network"}, True),
        ({**ACTIVE, "network_status": "out_of_network"}, False),
        ({**LAPSED, "network_status": "in_network"}, False),  # dependency on active_member is executed
    ],
)
def test_member_in_network(facts, expected):
    assert seed("member_in_network", **facts) is expected


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({**ACTIVE, "follow_up_eligible": True, "risk_flag": "low"}, True),
        ({**ACTIVE, "follow_up_eligible": True, "risk_flag": "high"}, False),
        ({**ACTIVE, "follow_up_eligible": False, "risk_flag": "low"}, False),
        ({**LAPSED, "follow_up_eligible": True, "risk_flag": "low"}, False),
    ],
)
def test_eligible_for_follow_up(facts, expected):
    assert seed("eligible_for_follow_up", **facts) is expected


# --- Versions, drafts, deprecation, dependencies ----------------------------------------------------------------

JUNE_2025 = {"coverage_start_date": "2025-06-10", "coverage_end_date": None,
             "reporting_month_start": "2025-06-01", "reporting_month_end": "2025-06-30"}
JUNE_2026 = {"coverage_start_date": "2026-06-10", "coverage_end_date": None,
             "reporting_month_start": "2026-06-01", "reporting_month_end": "2026-06-30"}


def test_reporting_month_member_v1_counts_only_first_day_enrollment():
    result = evaluate(SEED, "reporting_month_member", date(2025, 6, 30), JUNE_2025)
    assert (result.concept.version, result.result) == ("1.0.0", False)


def test_reporting_month_member_v2_counts_any_portion():
    result = evaluate(SEED, "reporting_month_member", date(2026, 6, 30), JUNE_2026)
    assert (result.concept.version, result.result) == ("2.0.0", True)


@pytest.mark.parametrize(("on", "version"), [(date(2025, 12, 31), "1.0.0"), (date(2026, 1, 1), "2.0.0")])
def test_version_switch_boundary(on, version):
    assert evaluate(SEED, "reporting_month_member", on, JUNE_2026).concept.version == version


def test_v2_needs_a_fact_v1_did_not():
    facts = {k: v for k, v in JUNE_2026.items() if k != "reporting_month_end"}
    assert evaluate(SEED, "reporting_month_member", date(2025, 12, 31), facts).concept.version == "1.0.0"
    err = rule_error(InsufficientContext, SEED, "reporting_month_member", date(2026, 1, 1), facts)
    assert err.missing_facts == ["reporting_month_end"]


def test_unknown_concept_is_not_found():
    err = rule_error(ConceptNotFound, SEED, "no_such_concept", D, {})
    assert err.concept_id == "no_such_concept"


def test_draft_is_not_found():
    err = rule_error(ConceptNotFound, SEED, "engaged_member", D, {})
    assert err.concept_id == "engaged_member"


def test_concept_before_its_effective_date_is_not_effective():
    err = rule_error(ConceptNotEffective, SEED, "active_member", date(2023, 12, 31), ACTIVE)
    assert (err.concept_id, err.on) == ("active_member", date(2023, 12, 31))


def test_dependency_not_effective_is_reported_for_the_dependency():
    data = fixture()
    concept(data, "base")["effective_from"] = "2024-06-01"  # `derived` is effective from 2024-01-01
    err = rule_error(ConceptNotEffective, parse_catalog(data), "derived", date(2024, 3, 1), {"start": "2024-01-01", "status": "ok"})
    assert (err.concept_id, err.on) == ("base", date(2024, 3, 1))


def test_deprecated_concept_still_evaluates():
    result = evaluate(SEED, "covered_life", D, {"coverage_start_date": "2020-01-01"})
    assert (result.concept.status, result.concept.superseded_by, result.result) == ("deprecated", "active_member", True)


def test_non_draft_concept_without_rule_raises_no_rule():
    err = rule_error(NoRule, parse_catalog(fixture()), "legacy", date(2024, 1, 1), {})
    assert (err.concept_id, err.version) == ("legacy", "1.0.0")


def test_evaluation_reports_the_target_version_not_the_dependency():
    result = evaluate(SEED, "member_in_network", D, {**ACTIVE, "network_status": "in_network"})
    assert (result.concept.id, result.concept.version, result.result) == ("member_in_network", "1.0.0", True)

