"""Load and validate the concept catalog (catalog/concepts.yaml) at startup.

Validation collects every problem in one pass and raises a single CatalogError, so a steward fixing the file sees
the whole list at once. Each issue names its subject (concept id + version, or fact name) and field. The app refuses
to start on any issue: an invalid catalog must never serve definitions.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

import yaml
from pydantic import ValidationError

from .models import (
    AllExpr,
    AnyExpr,
    Comparison,
    Concept,
    ConceptRef,
    Expr,
    FactOperand,
    FactSpec,
    NotExpr,
    NULL_OPS,
    RefOperand,
    SNAKE_CASE,
)

# Which ops make sense for each fact type. Rejecting the rest at load means the evaluator never meets, say,
# `True < False` or a date inside an `in` list.
OPS_BY_TYPE = {
    "date": frozenset({"eq", "ne", "lt", "lte", "gt", "gte"}),
    "integer": frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in"}),
    "string": frozenset({"eq", "ne", "in"}),
    "boolean": frozenset({"eq", "ne"}),
}


@dataclass(frozen=True)
class Issue:
    subject: str  # e.g. "concept 'active_member' v1.0.0", "fact 'coverage_end_date'", "catalog"
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.subject}: {self.field}: {self.message}"


class CatalogError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        lines = "\n".join(f"  - {issue}" for issue in issues)
        super().__init__(f"catalog is invalid ({len(issues)} issue(s)):\n{lines}")


@dataclass(frozen=True)
class Catalog:
    facts: Mapping[str, FactSpec]
    concepts: Mapping[str, tuple[Concept, ...]]  # id -> versions sorted by effective_from

    def versions(self, concept_id: str) -> tuple[Concept, ...]:
        return self.concepts.get(concept_id, ())

    def effective(self, concept_id: str, on: date) -> Concept | None:
        """The non-draft version effective on `on` (bounds inclusive). Validation guarantees at most one."""
        for concept in self.versions(concept_id):
            if concept.status != "draft" and _covers(concept, on):
                return concept
        return None


def _covers(concept: Concept, on: date) -> bool:
    return concept.effective_from <= on and (concept.effective_to is None or on <= concept.effective_to)


def load_catalog(path: Path) -> Catalog:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError([Issue("catalog", "file", f"cannot read catalog file: {exc.strerror}")]) from None
    try:
        # safe_load only builds plain data (no arbitrary Python objects from YAML tags).
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError([Issue("catalog", "file", f"invalid YAML: {exc}")]) from None
    return parse_catalog(data)


def parse_catalog(data: Any) -> Catalog:
    issues: list[Issue] = []
    if not isinstance(data, dict) or set(data) != {"facts", "concepts"}:
        raise CatalogError([Issue("catalog", "top level", "must be a mapping with exactly the keys 'facts' and 'concepts'")])
    raw_facts, raw_concepts = data["facts"], data["concepts"]
    if not isinstance(raw_facts, dict):
        issues.append(Issue("catalog", "facts", "must be a mapping of fact name to fact spec"))
        raw_facts = {}
    if not isinstance(raw_concepts, list):
        issues.append(Issue("catalog", "concepts", "must be a list of concepts"))
        raw_concepts = []

    facts = _parse_facts(raw_facts, issues)
    concepts, raw_ids = _parse_concepts(raw_concepts, issues)
    issues.extend(_cross_checks(facts, set(raw_facts), concepts, raw_ids))
    if issues:
        raise CatalogError(issues)

    by_id: dict[str, list[Concept]] = defaultdict(list)
    for concept in concepts:
        by_id[concept.id].append(concept)
    frozen = {cid: tuple(sorted(vs, key=lambda c: c.effective_from)) for cid, vs in by_id.items()}
    return Catalog(facts=MappingProxyType(facts), concepts=MappingProxyType(frozen))


# --- Parsing ----------------------------------------------------------------------------------------------------


def _format_loc(loc: tuple[Any, ...]) -> str:
    # Pydantic puts the union tag in the path (e.g. "all.all.0"); dropping consecutive repeats keeps it readable.
    parts: list[str] = []
    for part in loc:
        text = str(part)
        if not parts or parts[-1] != text:
            parts.append(text)
    return ".".join(parts) or "(root)"


def _pydantic_issues(subject: str, exc: ValidationError) -> Iterator[Issue]:
    for error in exc.errors():
        yield Issue(subject, _format_loc(error["loc"]), error["msg"].removeprefix("Value error, "))


def _parse_facts(raw: dict[Any, Any], issues: list[Issue]) -> dict[str, FactSpec]:
    facts: dict[str, FactSpec] = {}
    for name, spec in raw.items():
        subject = f"fact '{name}'"
        if not isinstance(name, str) or not re.match(SNAKE_CASE, name):
            issues.append(Issue(subject, "name", "must be snake_case"))
            continue
        try:
            facts[name] = FactSpec.model_validate(spec)
        except ValidationError as exc:
            issues.extend(_pydantic_issues(subject, exc))
    for name, spec in facts.items():
        if spec.not_before is None:
            continue
        other = facts.get(spec.not_before)
        if other is None:
            issues.append(Issue(f"fact '{name}'", "not_before", f"unknown fact '{spec.not_before}'"))
        elif other.type != spec.type or spec.type not in ("date", "integer"):
            issues.append(Issue(f"fact '{name}'", "not_before", f"'{spec.not_before}' must be a fact of the same date or integer type"))
    return facts


def _concept_subject(raw: Any, index: int) -> str:
    if isinstance(raw, dict) and isinstance(raw.get("id"), str):
        return f"concept '{raw['id']}' v{raw.get('version', '?')}"
    return f"concepts[{index}]"


def _parse_concepts(raw_concepts: list[Any], issues: list[Issue]) -> tuple[list[Concept], set[str]]:
    concepts: list[Concept] = []
    # Ids of every entry, parsed or not: a reference to a concept that merely has a schema error is not also
    # reported as "unknown target", which would bury the real problem.
    raw_ids: set[str] = set()
    for index, raw in enumerate(raw_concepts):
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            raw_ids.add(raw["id"])
        try:
            concepts.append(Concept.model_validate(raw))
        except ValidationError as exc:
            issues.extend(_pydantic_issues(_concept_subject(raw, index), exc))
    return concepts, raw_ids


# --- Cross-object checks ----------------------------------------------------------------------------------------


def _subject(concept: Concept) -> str:
    return f"concept '{concept.id}' v{concept.version}"


def _walk(expr: Expr) -> Iterator[Expr]:
    yield expr
    if isinstance(expr, (AllExpr, AnyExpr)):
        for child in expr.all if isinstance(expr, AllExpr) else expr.any:
            yield from _walk(child)
    elif isinstance(expr, NotExpr):
        yield from _walk(expr.not_)


def concept_refs(expr: Expr) -> set[str]:
    return {node.concept for node in _walk(expr) if isinstance(node, ConceptRef)}


def _cross_checks(
    facts: dict[str, FactSpec], raw_fact_names: set[Any], concepts: list[Concept], raw_ids: set[str]
) -> list[Issue]:
    issues: list[Issue] = []
    known_ids = raw_ids
    known_terms = {concept.term for concept in concepts}
    seen_versions: set[tuple[str, str]] = set()

    for c in concepts:
        s = _subject(c)
        if (c.id, c.version) in seen_versions:
            issues.append(Issue(s, "version", "duplicate (id, version) pair"))
        seen_versions.add((c.id, c.version))

        if c.effective_to is not None and c.effective_from > c.effective_to:
            issues.append(Issue(s, "effective_to", f"{c.effective_to} is before effective_from {c.effective_from}"))

        if c.status == "approved":
            if c.rule is None:
                issues.append(Issue(s, "rule", "required for an approved concept"))
            if c.authoritative_source is None:
                issues.append(Issue(s, "authoritative_source", "required for an approved concept"))
        if c.status == "deprecated" and c.superseded_by is None:
            issues.append(Issue(s, "superseded_by", "required for a deprecated concept"))
        if c.superseded_by is not None and (c.superseded_by not in known_ids or c.superseded_by == c.id):
            issues.append(Issue(s, "superseded_by", f"'{c.superseded_by}' is not another concept id"))

        for i, rel in enumerate(c.relationships):
            if rel.target not in known_ids and rel.target not in known_terms:
                issues.append(Issue(s, f"relationships.{i}.target", f"'{rel.target}' is not a concept id or term"))

        refs = concept_refs(c.rule.expression) if c.rule else set()
        depends_on = {rel.target for rel in c.relationships if rel.type == "depends_on"}
        if refs != depends_on:
            issues.append(Issue(s, "relationships", f"depends_on targets {sorted(depends_on)} must equal the rule's concept refs {sorted(refs)}"))

        if c.rule is not None:
            issues.extend(_expression_issues(s, c.rule.expression, facts, raw_fact_names, known_ids))

    issues.extend(_overlap_issues(concepts))
    issues.extend(_cycle_issues(concepts))
    return issues


def _expression_issues(
    subject: str, expr: Expr, facts: dict[str, FactSpec], raw_fact_names: set[Any], known_ids: set[str]
) -> Iterator[Issue]:
    field = "rule.expression"
    for node in _walk(expr):
        if isinstance(node, ConceptRef) and node.concept not in known_ids:
            yield Issue(subject, field, f"unknown concept '{node.concept}'")
        if not isinstance(node, Comparison):
            continue
        spec = facts.get(node.fact)
        if spec is None:
            # A fact that exists but failed its own schema check is already reported; don't cascade.
            if node.fact not in raw_fact_names:
                yield Issue(subject, field, f"unknown fact '{node.fact}'")
            continue
        where = f"'{node.fact} {node.op}'"
        if node.op in NULL_OPS:
            if not spec.nullable:
                yield Issue(subject, field, f"{where}: fact is not nullable, so this test is constant")
            continue
        if node.op not in OPS_BY_TYPE[spec.type]:
            yield Issue(subject, field, f"{where}: op not allowed on a {spec.type} fact")
            continue
        value = node.value
        if isinstance(value, RefOperand):
            if spec.type != "date":
                yield Issue(subject, field, f"{where}: requested_date can only be compared with a date fact")
        elif isinstance(value, FactOperand):
            other = facts.get(value.fact)
            if other is None:
                if value.fact not in raw_fact_names:
                    yield Issue(subject, field, f"{where}: unknown fact '{value.fact}'")
            elif other.type != spec.type:
                yield Issue(subject, field, f"{where}: '{value.fact}' has type {other.type}, expected {spec.type}")
        else:
            for literal in value if isinstance(value, list) else [value]:
                if not _literal_matches(literal, spec.type):
                    yield Issue(subject, field, f"{where}: literal {literal!r} does not match fact type {spec.type}")


def _literal_matches(literal: Any, fact_type: str) -> bool:
    if fact_type == "boolean":
        return isinstance(literal, bool)
    if fact_type == "integer":
        return isinstance(literal, int) and not isinstance(literal, bool)
    if fact_type == "date":
        return isinstance(literal, date)
    return isinstance(literal, str)


def _overlap_issues(concepts: list[Concept]) -> Iterator[Issue]:
    # Brief §4a requires this for approved versions; it is applied to all non-draft versions (approved and
    # deprecated) because either can be selected by date, and two candidates would force a silent choice.
    by_id: dict[str, list[Concept]] = defaultdict(list)
    for c in concepts:
        if c.status != "draft":
            by_id[c.id].append(c)
    for versions in by_id.values():
        ordered = sorted(versions, key=lambda c: c.effective_from)
        for earlier, later in zip(ordered, ordered[1:]):
            if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
                yield Issue(_subject(later), "effective_from", f"overlaps version {earlier.version} of the same concept")


def _cycle_issues(concepts: list[Concept]) -> Iterator[Issue]:
    # Edges by id, across all versions: a cycle through any version could recurse forever at evaluation time.
    edges: dict[str, set[str]] = defaultdict(set)
    for c in concepts:
        edges[c.id] |= {r.target for r in c.relationships if r.type == "depends_on"}
        if c.rule is not None:
            edges[c.id] |= concept_refs(c.rule.expression)

    state: dict[str, str] = {}  # "visiting" | "done"
    path: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = "visiting"
        path.append(node)
        for target in sorted(edges.get(node, ())):
            if state.get(target) == "visiting":
                found.append(path[path.index(target):] + [target])
            elif target not in state:
                visit(target)
        path.pop()
        state[node] = "done"

    for node in sorted(edges):
        if node not in state:
            visit(node)
    for cycle in found:
        yield Issue(f"concept '{cycle[0]}'", "relationships", "dependency cycle: " + " -> ".join(cycle))
