"""Catalog loading and startup validation.

Every validation rule has a failing test with the exact issue text, and the valid fixture (plus the boundary cases
here) is its passing test. Each failing case breaks one thing in a copy of tests/fixtures/minimal_catalog.yaml.
"""
from datetime import date
from pathlib import Path

import pytest
import yaml

from app.catalog import CatalogError, load_catalog, parse_catalog
from app.main import DEFAULT_CATALOG_PATH, create_app

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_catalog.yaml"
ALL_ROLES = ["analyst", "care_manager", "steward"]


def fixture() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def concept(data: dict, concept_id: str, version: str = "1.0.0") -> dict:
    return next(c for c in data["concepts"] if c["id"] == concept_id and c["version"] == version)


def issues_of(data) -> list[str]:
    with pytest.raises(CatalogError) as excinfo:
        parse_catalog(data)
    return [str(issue) for issue in excinfo.value.issues]


# --- Passing paths ----------------------------------------------------------------------------------------------


def test_seed_catalog_loads_with_expected_concepts():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    assert sorted(catalog.concepts) == [
        "active_member",
        "claimant_member",
        "covered_life",
        "currently_eligible_member",
        "eligible_for_follow_up",
        "engaged_member",
        "member_in_network",
        "registered_member",
        "reporting_month_member",
    ]
    assert sum(len(versions) for versions in catalog.concepts.values()) == 10
    assert [c.version for c in catalog.versions("reporting_month_member")] == ["1.0.0", "2.0.0"]


def test_seed_catalog_has_five_approved_member_concepts_plus_one_draft():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    member = sorted((c.id, c.status) for vs in catalog.concepts.values() for c in vs if c.term == "member")
    assert member == [
        ("active_member", "approved"),
        ("claimant_member", "approved"),
        ("currently_eligible_member", "approved"),
        ("engaged_member", "draft"),
        ("registered_member", "approved"),
        ("reporting_month_member", "approved"),
        ("reporting_month_member", "approved"),
    ]


def test_every_approved_seed_concept_has_rule_text_and_expression():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    approved = [c for vs in catalog.concepts.values() for c in vs if c.status == "approved"]
    assert len(approved) == 8
    for c in approved:
        assert c.rule is not None and c.rule.text and c.rule.expression is not None, c.id


def test_carried_over_definitions_are_verbatim():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    expected = {
        "active_member": (
            "Person with coverage effective on the requested date",
            "eligibility_enrollment_platform",
            "coverage_start_date <= requested_date AND (coverage_end_date IS NULL OR coverage_end_date >= requested_date)",
            ["eligibility_enrollment_platform", "crm", "claims_system"],
        ),
        "member_in_network": (
            "Member whose provider network status is in-network on the requested date",
            "provider_network_directory",
            "network_status == 'in_network' AND active_member == TRUE",
            ["provider_network_directory", "eligibility_enrollment_platform", "care_management_system"],
        ),
        "eligible_for_follow_up": (
            "Active member eligible for outreach based on plan status and care gap follow-up rules",
            "care_management_system",
            "active_member == TRUE AND follow_up_eligible == TRUE AND risk_flag != 'high'",
            ["care_management_system", "eligibility_enrollment_platform", "population_health_platform"],
        ),
    }
    for concept_id, (definition, source, rule_text, source_systems) in expected.items():
        (c,) = catalog.versions(concept_id)
        assert (c.definition, c.authoritative_source.system, c.rule.text, c.source_systems) == (
            definition,
            source,
            rule_text,
            source_systems,
        )


def test_seed_restricted_concept_roles():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    restricted = {cid for cid, vs in catalog.concepts.items() if any(c.allowed_roles != ALL_ROLES for c in vs)}
    assert restricted == {"eligible_for_follow_up"}
    assert catalog.versions("eligible_for_follow_up")[0].allowed_roles == ["care_manager", "steward"]


def test_minimal_fixture_is_valid_and_versions_are_selected_by_inclusive_dates():
    catalog = parse_catalog(fixture())
    assert catalog.effective("base", date(2023, 12, 31)) is None
    assert catalog.effective("base", date(2024, 12, 31)).version == "1.0.0"  # effective_to is inclusive
    assert catalog.effective("base", date(2025, 1, 1)).version == "2.0.0"  # adjacent versions do not overlap
    assert catalog.effective("idea", date(2026, 1, 1)) is None  # drafts never become effective
    assert catalog.effective("unknown", date(2026, 1, 1)) is None


def test_effective_from_equal_to_effective_to_is_valid():
    data = fixture()
    concept(data, "base")["effective_to"] = "2024-01-01"
    assert parse_catalog(data).effective("base", date(2024, 1, 1)).version == "1.0.0"


def test_draft_may_overlap_an_approved_version():
    data = fixture()
    draft = dict(concept(data, "base", "2.0.0"), version="3.0.0", status="draft", effective_from="2025-06-01")
    data["concepts"].append(draft)
    assert parse_catalog(data).effective("base", date(2025, 7, 1)).version == "2.0.0"


# --- File and top-level shape -----------------------------------------------------------------------------------


def test_missing_file_is_a_catalog_error(tmp_path):
    with pytest.raises(CatalogError) as excinfo:
        load_catalog(tmp_path / "absent.yaml")
    assert [str(i) for i in excinfo.value.issues] == ["catalog: file: cannot read catalog file: No such file or directory"]


def test_invalid_yaml_is_a_catalog_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("facts: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError) as excinfo:
        load_catalog(bad)
    assert len(excinfo.value.issues) == 1
    assert str(excinfo.value.issues[0]).startswith("catalog: file: invalid YAML: ")


def test_yaml_python_tags_are_rejected(tmp_path):
    # safe_load refuses to construct Python objects, so a catalog file cannot execute code.
    bad = tmp_path / "tagged.yaml"
    bad.write_text("facts: !!python/object/apply:os.system ['echo hi']\nconcepts: []\n", encoding="utf-8")
    with pytest.raises(CatalogError) as excinfo:
        load_catalog(bad)
    assert str(excinfo.value.issues[0]).startswith("catalog: file: invalid YAML: ")


@pytest.mark.parametrize("data", [[], None, "text", {"facts": {}}, {"facts": {}, "concepts": [], "extra": 1}])
def test_top_level_must_have_exactly_facts_and_concepts(data):
    assert issues_of(data) == ["catalog: top level: must be a mapping with exactly the keys 'facts' and 'concepts'"]


def test_wrong_top_level_value_types_are_all_reported():
    assert issues_of({"facts": [], "concepts": {}}) == [
        "catalog: facts: must be a mapping of fact name to fact spec",
        "catalog: concepts: must be a list of concepts",
    ]


# --- Fact dictionary --------------------------------------------------------------------------------------------


def test_fact_type_must_be_known():
    data = fixture()
    data["facts"]["count"] = {"type": "float"}
    assert issues_of(data) == ["fact 'count': type: Input should be 'date', 'integer', 'string' or 'boolean'"]


def test_fact_name_must_be_snake_case():
    data = fixture()
    data["facts"]["Bad-Name"] = {"type": "string"}
    assert issues_of(data) == ["fact 'Bad-Name': name: must be snake_case"]


def test_fact_not_before_must_name_a_known_fact():
    data = fixture()
    data["facts"]["end"]["not_before"] = "nope"
    assert issues_of(data) == ["fact 'end': not_before: unknown fact 'nope'"]


def test_fact_not_before_must_have_the_same_ordered_type():
    data = fixture()
    data["facts"]["end"]["not_before"] = "status"
    assert issues_of(data) == ["fact 'end': not_before: 'status' must be a fact of the same date or integer type"]


def test_malformed_fact_does_not_cascade_into_unknown_fact_errors():
    data = fixture()
    data["facts"]["status"] = {"type": "text"}  # `derived` uses status
    assert issues_of(data) == ["fact 'status': type: Input should be 'date', 'integer', 'string' or 'boolean'"]


# --- Concept schema ---------------------------------------------------------------------------------------------


def test_owner_is_required():
    data = fixture()
    del concept(data, "base")["owner"]
    assert issues_of(data) == ["concept 'base' v1.0.0: owner: Field required"]


def test_unknown_field_is_rejected():
    data = fixture()
    concept(data, "base")["efective_to"] = "2024-12-31"
    assert issues_of(data) == ["concept 'base' v1.0.0: efective_to: Extra inputs are not permitted"]


def test_context_system_must_be_known():
    data = fixture()
    concept(data, "legacy")["context"]["system"] = "finance"
    assert issues_of(data) == [
        "concept 'legacy' v1.0.0: context.system: Input should be 'crm', 'enrollment', 'claims', 'analytics', "
        "'customer_service', 'care_management' or 'network'"
    ]


def test_version_must_be_semver():
    data = fixture()
    concept(data, "legacy")["version"] = "1.0"
    assert issues_of(data) == [
        r"concept 'legacy' v1.0: version: String should match pattern '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$'"
    ]


def test_bad_effective_date_is_rejected():
    data = fixture()
    concept(data, "legacy")["effective_from"] = "2020-13-01"
    assert issues_of(data) == [
        "concept 'legacy' v1.0.0: effective_from: Input should be a valid date or datetime, month value is outside expected range of 1-12"
    ]


def test_non_mapping_concept_entry_is_rejected():
    data = fixture()
    data["concepts"].append("oops")
    assert issues_of(data) == ["concepts[5]: (root): Input should be a valid dictionary or instance of Concept"]


def test_schema_error_in_a_target_does_not_cascade():
    data = fixture()
    del concept(data, "base")["owner"]
    del concept(data, "base", "2.0.0")["owner"]  # `derived` and `legacy` still reference `base`
    assert issues_of(data) == [
        "concept 'base' v1.0.0: owner: Field required",
        "concept 'base' v2.0.0: owner: Field required",
    ]


# --- Cross-concept rules (brief §4a) ----------------------------------------------------------------------------


def test_id_version_pairs_are_unique():
    data = fixture()
    data["concepts"].append(dict(concept(data, "legacy")))
    assert issues_of(data) == [
        "concept 'legacy' v1.0.0: version: duplicate (id, version) pair",
        "concept 'legacy' v1.0.0: effective_from: overlaps version 1.0.0 of the same concept",
    ]


def test_effective_from_must_not_be_after_effective_to():
    data = fixture()
    concept(data, "base")["effective_to"] = "2023-12-31"
    assert issues_of(data) == ["concept 'base' v1.0.0: effective_to: 2023-12-31 is before effective_from 2024-01-01"]


def test_approved_requires_rule():
    data = fixture()
    del concept(data, "base")["rule"]
    assert issues_of(data) == ["concept 'base' v1.0.0: rule: required for an approved concept"]


def test_approved_requires_authoritative_source():
    data = fixture()
    del concept(data, "base")["authoritative_source"]
    assert issues_of(data) == ["concept 'base' v1.0.0: authoritative_source: required for an approved concept"]


def test_deprecated_requires_superseded_by():
    data = fixture()
    del concept(data, "legacy")["superseded_by"]
    assert issues_of(data) == ["concept 'legacy' v1.0.0: superseded_by: required for a deprecated concept"]


@pytest.mark.parametrize("target", ["nope", "legacy"])
def test_superseded_by_must_be_another_existing_concept(target):
    data = fixture()
    concept(data, "legacy")["superseded_by"] = target
    assert issues_of(data) == [f"concept 'legacy' v1.0.0: superseded_by: '{target}' is not another concept id"]


def test_relationship_target_must_be_a_concept_id_or_term():
    data = fixture()
    concept(data, "derived")["relationships"][1]["target"] = "nothing"
    assert issues_of(data) == ["concept 'derived' v1.0.0: relationships.1.target: 'nothing' is not a concept id or term"]


def test_depends_on_without_a_rule_reference_is_rejected():
    data = fixture()
    concept(data, "derived")["relationships"].append({"type": "depends_on", "target": "legacy", "description": "x"})
    assert issues_of(data) == [
        "concept 'derived' v1.0.0: relationships: depends_on targets ['base', 'legacy'] must equal the rule's concept refs ['base']"
    ]


def test_rule_reference_without_depends_on_is_rejected():
    data = fixture()
    del concept(data, "derived")["relationships"][0]
    assert issues_of(data) == [
        "concept 'derived' v1.0.0: relationships: depends_on targets [] must equal the rule's concept refs ['base']"
    ]


def test_approved_versions_must_not_overlap():
    data = fixture()
    concept(data, "base")["effective_to"] = "2025-01-01"  # one day into v2
    assert issues_of(data) == ["concept 'base' v2.0.0: effective_from: overlaps version 1.0.0 of the same concept"]


def test_open_ended_earlier_version_overlaps():
    data = fixture()
    concept(data, "base")["effective_to"] = None
    assert issues_of(data) == ["concept 'base' v2.0.0: effective_from: overlaps version 1.0.0 of the same concept"]


def test_deprecated_versions_must_not_overlap_either():
    data = fixture()
    data["concepts"].append(dict(concept(data, "legacy"), version="2.0.0", effective_from="2021-01-01"))
    assert issues_of(data) == ["concept 'legacy' v2.0.0: effective_from: overlaps version 1.0.0 of the same concept"]


def test_dependency_cycle_is_rejected():
    data = fixture()
    v2 = concept(data, "base", "2.0.0")
    v2["rule"]["expression"]["all"].append({"concept": "derived"})
    v2["relationships"] = [{"type": "depends_on", "target": "derived", "description": "cycle"}]
    assert issues_of(data) == ["concept 'base': relationships: dependency cycle: base -> derived -> base"]


def test_self_dependency_is_a_cycle():
    data = fixture()
    d = concept(data, "derived")
    d["rule"]["expression"]["all"].append({"concept": "derived"})
    d["relationships"].append({"type": "depends_on", "target": "derived", "description": "self"})
    assert issues_of(data) == ["concept 'derived': relationships: dependency cycle: derived -> derived"]


# --- Rule expressions: structure --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ({"foo": 1}, "rule.expression: expression node must be a mapping with one of: all, any, not, concept, fact"),
        ("start <= requested_date", "rule.expression: expression node must be a mapping with one of: all, any, not, concept, fact"),
        ({"all": []}, "rule.expression.all: List should have at least 1 item after validation, not 0"),
        ({"any": [{"fact": "start"}]}, "rule.expression.any.0.fact.op: Field required"),
        ({"fact": "start", "op": "is_null", "value": 1}, "rule.expression.fact: op 'is_null' takes no value"),
        ({"fact": "start", "op": "lte"}, "rule.expression.fact: op 'lte' requires a value"),
        ({"fact": "start", "op": "eq", "value": None}, "rule.expression.fact: op 'eq' cannot compare with null; use is_null or not_null"),
        ({"fact": "status", "op": "in", "value": "ok"}, "rule.expression.fact: op 'in' requires a non-empty list of literals"),
        ({"fact": "status", "op": "in", "value": []}, "rule.expression.fact.value.list: List should have at least 1 item after validation, not 0"),
        ({"fact": "start", "op": "lt", "value": [1]}, "rule.expression.fact: op 'lt' does not accept a list"),
        ({"fact": "start", "op": "lte", "value": {"ref": "today"}}, "rule.expression.fact.value.ref: Input should be 'requested_date'"),
        ({"fact": "start", "op": "lte", "value": {"x": 1}}, "rule.expression.fact.value: operand must be a literal, a list, {ref: requested_date} or {fact: <name>}"),
        ({"fact": "start", "op": "between", "value": 1}, "rule.expression.fact.op: Input should be 'eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'is_null', 'not_null' or 'in'"),
        ({"fact": "start", "op": "lte", "value": {"ref": "requested_date"}, "extra": 1}, "rule.expression.fact.extra: Extra inputs are not permitted"),
    ],
)
def test_malformed_expression_is_rejected(expression, expected):
    data = fixture()
    concept(data, "base")["rule"]["expression"] = expression
    assert issues_of(data) == [f"concept 'base' v1.0.0: {expected}"]


def test_expression_node_with_two_node_keys_is_rejected():
    data = fixture()
    concept(data, "base")["rule"]["expression"] = {"all": [{"concept": "base"}], "not": {"concept": "base"}}
    assert issues_of(data) == ["concept 'base' v1.0.0: rule.expression.all.not: Extra inputs are not permitted"]


# --- Rule expressions: types against the fact dictionary --------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ({"fact": "nope", "op": "eq", "value": 1}, "unknown fact 'nope'"),
        ({"fact": "status", "op": "lt", "value": "a"}, "'status lt': op not allowed on a string fact"),
        ({"fact": "flag", "op": "in", "value": [True]}, "'flag in': op not allowed on a boolean fact"),
        ({"fact": "start", "op": "in", "value": [date(2024, 1, 1)]}, "'start in': op not allowed on a date fact"),
        ({"fact": "count", "op": "gte", "value": "1"}, "'count gte': literal '1' does not match fact type integer"),
        ({"fact": "count", "op": "eq", "value": True}, "'count eq': literal True does not match fact type integer"),
        ({"fact": "flag", "op": "eq", "value": 1}, "'flag eq': literal 1 does not match fact type boolean"),
        ({"fact": "status", "op": "in", "value": ["ok", 2]}, "'status in': literal 2 does not match fact type string"),
        ({"fact": "start", "op": "eq", "value": "2024-01-01"}, "'start eq': literal '2024-01-01' does not match fact type date"),
        ({"fact": "count", "op": "lte", "value": {"ref": "requested_date"}}, "'count lte': requested_date can only be compared with a date fact"),
        ({"fact": "start", "op": "lte", "value": {"fact": "count"}}, "'start lte': 'count' has type integer, expected date"),
        ({"fact": "start", "op": "lte", "value": {"fact": "nope"}}, "'start lte': unknown fact 'nope'"),
        ({"fact": "start", "op": "is_null"}, "'start is_null': fact is not nullable, so this test is constant"),
    ],
)
def test_expression_must_match_fact_dictionary(expression, expected):
    data = fixture()
    concept(data, "base")["rule"]["expression"] = expression
    assert issues_of(data) == [f"concept 'base' v1.0.0: rule.expression: {expected}"]


def test_unknown_concept_reference_is_rejected():
    data = fixture()
    concept(data, "base")["rule"]["expression"] = {"concept": "nope"}
    assert issues_of(data) == [
        "concept 'base' v1.0.0: relationships: depends_on targets [] must equal the rule's concept refs ['nope']",
        "concept 'base' v1.0.0: rule.expression: unknown concept 'nope'",
    ]


# --- One pass ---------------------------------------------------------------------------------------------------


def test_all_errors_are_reported_in_one_pass():
    data = fixture()
    data["facts"]["end"]["not_before"] = "nope"
    del concept(data, "legacy")["owner"]
    concept(data, "base")["effective_to"] = "2023-12-31"
    concept(data, "derived")["relationships"][1]["target"] = "nothing"
    with pytest.raises(CatalogError) as excinfo:
        parse_catalog(data)
    assert str(excinfo.value) == (
        "catalog is invalid (4 issue(s)):\n"
        "  - fact 'end': not_before: unknown fact 'nope'\n"
        "  - concept 'legacy' v1.0.0: owner: Field required\n"
        "  - concept 'base' v1.0.0: effective_to: 2023-12-31 is before effective_from 2024-01-01\n"
        "  - concept 'derived' v1.0.0: relationships.1.target: 'nothing' is not a concept id or term"
    )


def test_every_schema_error_within_one_concept_is_reported():
    data = fixture()
    c = concept(data, "legacy")
    del c["owner"]
    c["status"] = "retired"
    assert issues_of(data) == [
        "concept 'legacy' v1.0.0: owner: Field required",
        "concept 'legacy' v1.0.0: status: Input should be 'draft', 'approved' or 'deprecated'",
    ]


# --- Startup ----------------------------------------------------------------------------------------------------


def test_app_refuses_to_start_with_invalid_catalog(tmp_path):
    data = fixture()
    del concept(data, "legacy")["superseded_by"]
    bad = tmp_path / "concepts.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CatalogError) as excinfo:
        create_app(bad)
    assert [str(i) for i in excinfo.value.issues] == ["concept 'legacy' v1.0.0: superseded_by: required for a deprecated concept"]


def test_app_starts_with_seed_catalog():
    app = create_app()
    assert sorted(app.state.catalog.concepts)[0] == "active_member"
