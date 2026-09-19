"""Resolver decision table (PROGRESS.md D21), exercised directly on the pure function.

Rows: M = matching, non-draft concepts effective on as_of; N = M after context narrowing; V/R = N split by visibility.
The seed catalog has no term with mixed visibility, so a purpose-built catalog below covers every cell; the seed
covers the brief's named cases.
"""
from dataclasses import fields
from datetime import date

import pytest

from app.auth import Caller
from app.catalog import load_catalog, parse_catalog
from app.main import DEFAULT_CATALOG_PATH
from app.resolver import Resolution, resolve

SEED = load_catalog(DEFAULT_CATALOG_PATH)
D = date(2026, 9, 20)
ANALYST = Caller("a", "analyst")
MANAGER = Caller("m", "care_manager")
ALL = ["analyst", "care_manager", "steward"]
CM_ONLY = ["care_manager", "steward"]


def _c(cid, term, system, domain, roles, *, status="approved", superseded_by=None, frm="2024-01-01", to=None,
       version="1.0.0", aliases=(), definition=None):
    return {
        "id": cid, "term": term, "name": cid.replace("_", " ").title(), "context": {"system": system, "domain": domain},
        "definition": definition or f"Definition of {cid}", "aliases": list(aliases),
        "authoritative_source": {"system": "s", "dataset": "d"},
        "rule": {"text": "flag == TRUE", "expression": {"fact": "flag", "op": "eq", "value": True}},
        "owner": "team", "version": version, "status": status, "effective_from": frm, "effective_to": to,
        "superseded_by": superseded_by, "allowed_roles": roles,
    }


TABLE = parse_catalog({
    "facts": {"flag": {"type": "boolean"}},
    "concepts": [
        # term "widget": 2 visible (one deprecated), 3 restricted, plus a draft and a future concept
        _c("w_crm", "widget", "crm", "ops", ALL),
        _c("w_analytics", "widget", "analytics", "ops", ALL, status="deprecated", superseded_by="w_crm"),
        _c("w_claims", "widget", "claims", "ops", CM_ONLY, aliases=["claims widget"], definition="SECRET-DEF-claims-widget"),
        _c("w_network", "widget", "network", "field", CM_ONLY, definition="SECRET-DEF-network-widget"),
        _c("w_crm_secret", "widget", "crm", "private", CM_ONLY, aliases=["hush widget"], definition="SECRET-DEF-crm-widget"),
        _c("w_draft", "widget", "crm", "ops", ALL, status="draft"),
        _c("w_future", "widget", "enrollment", "ops", ALL, frm="2030-01-01"),
        # term "gadget": restricted only
        _c("g_only", "gadget", "crm", "ops", CM_ONLY, definition="SECRET-DEF-gadget"),
        # term "pair": one visible, one restricted
        _c("p_open", "pair", "crm", "ops", ALL),
        _c("p_closed", "pair", "claims", "ops", CM_ONLY, definition="SECRET-DEF-pair"),
        # term "solo": one visible
        _c("s_one", "solo", "enrollment", "eligibility", ALL),
        # term "twin": two visible in the same system -> clarifying question lists ids
        _c("t_a", "twin", "crm", "ops", ALL),
        _c("t_b", "twin", "crm", "sales", ALL),
        # term "sketch": draft only; term "later": not yet effective
        _c("sk_only", "sketch", "crm", "ops", ALL, status="draft"),
        _c("l_only", "later", "crm", "ops", ALL, frm="2030-01-01"),
        # term "timed": two versions
        _c("timed", "timed", "crm", "ops", ALL, to="2024-12-31"),
        _c("timed", "timed", "crm", "ops", ALL, frm="2025-01-01", version="2.0.0"),
    ],
})


def run(term, caller=ANALYST, system=None, domain=None, as_of=D, catalog=TABLE) -> Resolution:
    return resolve(catalog, term, as_of, caller, system, domain)


def ids(result: Resolution) -> list[str]:
    return [c.id for c in result.candidates]


def summary(result: Resolution) -> tuple:
    return (result.status, result.concept.id if result.concept else None, ids(result), result.restricted_count)


# --- Decision table: rows 1-8 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "caller", "system", "domain", "expected"),
    [
        # row 1: |M| = 0
        ("nothing at all", ANALYST, None, None, ("not_found", None, [], 0)),
        ("sketch", ANALYST, None, None, ("not_found", None, [], 0)),  # draft only
        ("later", ANALYST, None, None, ("not_found", None, [], 0)),  # not yet effective
        ("nothing at all", ANALYST, "crm", None, ("not_found", None, [], 0)),  # context irrelevant when nothing matches
        # row 2: V = 0, R >= 1 (no context / matched context)
        ("gadget", ANALYST, None, None, ("restricted", None, [], 1)),
        ("widget", ANALYST, "claims", None, ("restricted", None, [], 1)),
        # row 3: V = 1, R = 0
        ("solo", ANALYST, None, None, ("resolved", "s_one", [], 0)),
        ("widget", ANALYST, "analytics", None, ("resolved", "w_analytics", [], 0)),
        ("gadget", MANAGER, None, None, ("resolved", "g_only", [], 0)),
        # row 4: V = 1, R >= 1
        ("pair", ANALYST, None, None, ("resolved", "p_open", [], 1)),
        ("widget", ANALYST, "crm", None, ("resolved", "w_crm", [], 1)),
        # row 5: V >= 2, R = 0
        ("pair", MANAGER, None, None, ("ambiguous", None, ["p_closed", "p_open"], 0)),  # claims < crm
        ("widget", MANAGER, "crm", None, ("ambiguous", None, ["w_crm", "w_crm_secret"], 0)),
        # row 6: V >= 2, R >= 1
        ("widget", ANALYST, None, None, ("ambiguous", None, ["w_analytics", "w_crm"], 3)),
        ("widget", ANALYST, None, "ops", ("ambiguous", None, ["w_analytics", "w_crm"], 1)),
        # row 7: context matched none, V(M) >= 1 -> ambiguous even with one candidate
        ("widget", ANALYST, "finance", None, ("ambiguous", None, ["w_analytics", "w_crm"], 3)),
        ("solo", ANALYST, "crm", None, ("ambiguous", None, ["s_one"], 0)),
        ("pair", ANALYST, "enrollment", None, ("ambiguous", None, ["p_open"], 1)),
        # row 8: context matched none, V(M) = 0
        ("gadget", ANALYST, "finance", None, ("restricted", None, [], 1)),
        # system and domain together must both match
        ("widget", ANALYST, "crm", "ops", ("resolved", "w_crm", [], 0)),
        ("widget", MANAGER, "crm", "private", ("resolved", "w_crm_secret", [], 0)),
        ("widget", ANALYST, "crm", "field", ("ambiguous", None, ["w_analytics", "w_crm"], 3)),  # row 7
    ],
)
def test_decision_table(term, caller, system, domain, expected):
    assert summary(run(term, caller, system, domain)) == expected


# --- Warnings and questions per row -----------------------------------------------------------------------------


def test_row_2_restricted_warning_is_a_count_only():
    assert run("gadget").warnings == ("1 meaning(s) of 'gadget' exist that your role cannot access",)


def test_row_4_warns_that_another_meaning_exists():
    assert run("pair").warnings == ("1 other meaning(s) of 'pair' exist that your role cannot access",)


def test_row_5_question_lists_systems_in_candidate_order():
    assert run("pair", MANAGER).clarifying_question == "Which meaning of 'pair' do you need: claims or crm?"


def test_question_lists_ids_when_systems_repeat():
    result = run("twin")
    assert (ids(result), result.clarifying_question) == (["t_a", "t_b"], "Which meaning of 'twin' do you need: t_a or t_b?")


def test_row_6_warnings():
    assert run("widget").warnings == (
        "3 other meaning(s) of 'widget' exist that your role cannot access",
        "'w_analytics' is deprecated; use 'w_crm' instead",
    )


def test_row_7_warning_and_single_candidate_question():
    result = run("solo", system="crm")
    assert result.warnings == ("context system 'crm' matched none of the 1 meaning(s) of 'solo'; showing every meaning you can access",)
    assert result.clarifying_question == (
        "No meaning of 'solo' matches system 'crm'. The only meaning available to you is 'S One' (enrollment): "
        "Definition of s_one. Is that the meaning you need?"
    )


def test_row_7_with_restricted_names_the_only_available_meaning():
    result = run("pair", system="enrollment")
    assert result.clarifying_question.startswith("No meaning of 'pair' matches system 'enrollment'. The only meaning available")
    assert result.warnings == (
        "context system 'enrollment' matched none of the 2 meaning(s) of 'pair'; showing every meaning you can access",
        "1 other meaning(s) of 'pair' exist that your role cannot access",
    )


def test_row_8_warnings():
    assert run("gadget", system="finance").warnings == (
        "context system 'finance' matched none of the 1 meaning(s) of 'gadget'; showing every meaning you can access",
        "1 meaning(s) of 'gadget' exist that your role cannot access",
    )


def test_context_domain_and_system_named_in_warning():
    assert run("widget", system="crm", domain="field").warnings[0] == (
        "context system 'crm' and domain 'field' matched none of the 5 meaning(s) of 'widget'; showing every meaning you can access"
    )


def test_resolved_and_restricted_have_no_question_and_no_candidates():
    for result in (run("solo"), run("gadget")):
        assert (result.clarifying_question, result.candidates, result.suggestions) == (None, (), ())


# --- Status and as_of filters -----------------------------------------------------------------------------------


def test_draft_never_becomes_a_candidate():
    assert "w_draft" not in ids(run("widget", MANAGER))
    assert summary(run("sketch", MANAGER)) == ("not_found", None, [], 0)


def test_deprecated_resolves_with_warning():
    result = run("covered life", catalog=SEED)
    assert (result.status, result.concept.id, result.concept.superseded_by, result.warnings) == (
        "resolved", "covered_life", "active_member", ("'covered_life' is deprecated; use 'active_member' instead",)
    )


@pytest.mark.parametrize(("as_of", "status", "version"), [
    (date(2023, 12, 31), "not_found", None),
    (date(2024, 1, 1), "resolved", "1.0.0"),
    (date(2024, 12, 31), "resolved", "1.0.0"),
    (date(2025, 1, 1), "resolved", "2.0.0"),
])
def test_as_of_selects_the_version(as_of, status, version):
    result = run("timed", as_of=as_of)
    assert (result.status, result.concept.version if result.concept else None) == (status, version)


def test_future_concept_joins_once_effective():
    assert ids(run("widget", MANAGER, as_of=date(2030, 1, 1))) == [
        "w_analytics", "w_claims", "w_crm", "w_crm_secret", "w_future", "w_network"
    ]


# --- The brief's named cases on the seed catalog ----------------------------------------------------------------


def test_member_without_context_is_ambiguous_with_exactly_five():
    result = run("member", catalog=SEED)
    assert (result.status, ids(result), result.restricted_count) == (
        "ambiguous",
        ["reporting_month_member", "claimant_member", "registered_member", "currently_eligible_member", "active_member"],
        0,
    )
    assert result.clarifying_question == "Which meaning of 'member' do you need: analytics, claims, crm, customer_service or enrollment?"


@pytest.mark.parametrize(("system", "expected"), [
    ("enrollment", "active_member"), ("crm", "registered_member"), ("claims", "claimant_member"),
    ("analytics", "reporting_month_member"), ("customer_service", "currently_eligible_member"),
    ("Customer-Service", "currently_eligible_member"), (" ENROLLMENT ", "active_member"),
])
def test_member_narrowed_by_system(system, expected):
    assert summary(run("member", system=system, catalog=SEED)) == ("resolved", expected, [], 0)


def test_member_with_unknown_system_is_ambiguous_with_warning():
    result = run("member", system="finance", catalog=SEED)
    assert (result.status, len(result.candidates), result.warnings) == (
        "ambiguous", 5, ("context system 'finance' matched none of the 5 meaning(s) of 'member'; showing every meaning you can access",)
    )


@pytest.mark.parametrize("term", ["Active  Member", "ACTIVE_MEMBER", "active-member", "  active member  ", "enrolled member", "Enrolled-Member"])
def test_normalisation_matches_id_name_and_alias(term):
    assert summary(run(term, catalog=SEED)) == ("resolved", "active_member", [], 0)


@pytest.mark.parametrize(("term", "expected"), [("registrant", "registered_member"), ("claimant_member", "claimant_member"), ("Reporting month member", "reporting_month_member")])
def test_match_on_alias_id_and_name(term, expected):
    assert run(term, catalog=SEED).concept.id == expected


def test_eligible_for_follow_up_restricted_for_analyst_resolved_for_care_manager():
    assert summary(run("eligible for follow up", catalog=SEED)) == ("restricted", None, [], 1)
    assert summary(run("eligible for follow up", MANAGER, catalog=SEED)) == ("resolved", "eligible_for_follow_up", [], 0)


def test_misspelling_suggests_member():
    result = run("membr", catalog=SEED)
    assert (result.status, result.suggestions) == ("not_found", ("member",))


def test_draft_term_is_not_found_and_not_suggested():
    result = run("engaged member", catalog=SEED)
    assert result.status == "not_found"
    assert "engaged member" not in result.suggestions


@pytest.mark.parametrize(("as_of", "version"), [(date(2025, 12, 31), "1.0.0"), (date(2026, 1, 1), "2.0.0")])
def test_reporting_month_member_version_by_as_of(as_of, version):
    assert run("reporting month member", as_of=as_of, catalog=SEED).concept.version == version


def test_member_before_any_version_is_not_found():
    assert run("member", as_of=date(2023, 6, 1), catalog=SEED).status == "not_found"


def test_substrings_do_not_match():
    # "member" must not pull in member_in_network, and "member in network" must not match "member".
    assert "member_in_network" not in ids(run("member", catalog=SEED))
    assert run("member in network", catalog=SEED).concept.id == "member_in_network"


# --- Leak prevention --------------------------------------------------------------------------------------------


def test_resolution_has_no_field_that_can_hold_restricted_content():
    assert [f.name for f in fields(Resolution)] == [
        "status", "term", "as_of", "concept", "candidates", "clarifying_question", "suggestions", "restricted_count", "warnings"
    ]


@pytest.mark.parametrize(
    ("term", "system", "domain"),
    [("widget", None, None), ("widget", "claims", None), ("widget", "finance", None), ("gadget", None, None),
     ("pair", None, None), ("pair", "enrollment", None), ("widgt", None, None), ("hush", None, None), ("claims widg", None, None)],
)
def test_analyst_result_never_contains_restricted_content(term, system, domain):
    text = repr(run(term, ANALYST, system, domain))
    for secret in ("w_claims", "w_network", "w_crm_secret", "g_only", "p_closed", "claims widget", "hush widget", "SECRET-DEF"):
        assert secret not in text


def test_suggestions_come_only_from_visible_concepts():
    # The analyst is pointed at the visible "widget", never at the restricted alias "hush widget".
    assert run("hush widgt").suggestions == ("widget",)
    assert run("hush widgt", MANAGER).suggestions == ("hush widget", "widget", "claims widget")


def test_resolver_is_deterministic():
    assert run("widget") == run("widget")
    assert [run("member", catalog=SEED) for _ in range(3)] == [run("member", catalog=SEED)] * 3
