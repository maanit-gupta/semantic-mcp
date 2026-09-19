"""Deterministic rule evaluator: executes the declarative `rule.expression` from the catalog. No eval/exec.

The order is the point: (1) derive the required facts statically from the expression tree, following {concept:}
dependencies; (2) refuse if any is absent; (3) type-check them; (4) only then evaluate. Because evaluation starts
only when every required fact is present and valid, short-circuiting in all/any/not can never hide a missing fact.
Absent key = missing; a key present with null = a value. This is a pure library; HTTP mapping belongs to the API.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping

from .catalog import Catalog
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
    RefOperand,
    parse_iso_date,
    walk,
)


class RuleError(Exception):
    """Base class: every evaluator failure is one of the typed errors below, never a boolean."""


class ConceptNotFound(RuleError):
    def __init__(self, concept_id: str):
        self.concept_id = concept_id
        super().__init__(f"no concept '{concept_id}' (unknown or draft only)")


class ConceptNotEffective(RuleError):
    def __init__(self, concept_id: str, on: date):
        self.concept_id, self.on = concept_id, on
        super().__init__(f"concept '{concept_id}' has no version effective on {on.isoformat()}")


class NoRule(RuleError):
    def __init__(self, concept_id: str, version: str):
        self.concept_id, self.version = concept_id, version
        super().__init__(f"concept '{concept_id}' v{version} has no rule to evaluate")


class InsufficientContext(RuleError):
    def __init__(self, missing_facts: list[str]):
        self.missing_facts = missing_facts
        super().__init__(f"missing facts: {', '.join(missing_facts)}")


class InvalidFacts(RuleError):
    def __init__(self, errors: dict[str, str]):
        self.errors = dict(sorted(errors.items()))
        super().__init__("; ".join(f"{name}: {msg}" for name, msg in self.errors.items()))


@dataclass(frozen=True)
class Evaluation:
    concept: Concept  # the version whose rule was executed (effective on requested_date)
    result: bool


# The only place comparison semantics live. Types are guaranteed compatible by load-time checks plus _coerce.
_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "in": lambda left, right: left in right,
}

def rule_version(catalog: Catalog, concept_id: str, on: date) -> Concept:
    """The version of `concept_id` whose rule applies on `on`, or a typed error explaining why there is none."""
    if not any(v.status != "draft" for v in catalog.versions(concept_id)):
        raise ConceptNotFound(concept_id)
    concept = catalog.effective(concept_id, on)
    if concept is None:
        raise ConceptNotEffective(concept_id, on)
    if concept.rule is None:
        raise NoRule(concept_id, concept.version)
    return concept


def required_facts(catalog: Catalog, concept_id: str, on: date) -> frozenset[str]:
    """Every fact the rule can read on `on`, found by walking the tree (never by evaluating it)."""
    names: set[str] = set()
    for node in walk(rule_version(catalog, concept_id, on).rule.expression):
        if isinstance(node, ConceptRef):
            names |= required_facts(catalog, node.concept, on)  # acyclic: checked at load
        elif isinstance(node, Comparison):
            names.add(node.fact)
            if isinstance(node.value, FactOperand):
                names.add(node.value.fact)
    return frozenset(names)


def evaluate(catalog: Catalog, concept_id: str, requested_date: date, facts: Mapping[str, Any]) -> Evaluation:
    concept = rule_version(catalog, concept_id, requested_date)
    required = required_facts(catalog, concept_id, requested_date)

    missing = sorted(name for name in required if name not in facts)  # key test: a present null is not missing
    if missing:
        raise InsufficientContext(missing)

    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in sorted(required):
        try:
            values[name] = _coerce(catalog.facts[name], facts[name])
        except ValueError as exc:
            errors[name] = str(exc)
    for name, value in values.items():
        partner = catalog.facts[name].not_before
        earlier = values.get(partner) if partner else None
        if value is not None and earlier is not None and value < earlier:
            errors[name] = f"must not be before {partner}"
    if errors:
        raise InvalidFacts(errors)

    return Evaluation(concept=concept, result=_eval(catalog, concept.rule.expression, requested_date, values))


def _coerce(spec: FactSpec, raw: Any) -> Any:
    if raw is None:
        if spec.nullable:
            return None
        raise ValueError("must not be null")
    if spec.type == "date":
        return parse_iso_date(raw)
    if spec.type == "integer":
        if isinstance(raw, int) and not isinstance(raw, bool):  # bool is an int subclass in Python
            return raw
        raise ValueError("expected an integer")
    if spec.type == "boolean":
        if isinstance(raw, bool):
            return raw
        raise ValueError("expected a boolean")
    if isinstance(raw, str):
        return raw
    raise ValueError("expected a string")


def _eval(catalog: Catalog, expr: Expr, on: date, values: Mapping[str, Any]) -> bool:
    if isinstance(expr, AllExpr):
        return all(_eval(catalog, child, on, values) for child in expr.all)
    if isinstance(expr, AnyExpr):
        return any(_eval(catalog, child, on, values) for child in expr.any)
    if isinstance(expr, NotExpr):
        return not _eval(catalog, expr.not_, on, values)
    if isinstance(expr, ConceptRef):
        # Dependencies are evaluated with the same date and facts; their version is the one effective on `on`.
        return _eval(catalog, rule_version(catalog, expr.concept, on).rule.expression, on, values)

    left = values[expr.fact]
    if expr.op == "is_null":
        return left is None
    if expr.op == "not_null":
        return left is not None
    value = expr.value
    if isinstance(value, RefOperand):
        right = on
    elif isinstance(value, FactOperand):
        right = values[value.fact]
    else:
        right = value
    if left is None or right is None:
        return False  # SQL WHERE semantics: only is_null / not_null observe null
    return _COMPARE[expr.op](left, right)
