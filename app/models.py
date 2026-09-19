"""Pydantic models for the catalog: concepts, rule expressions and the fact dictionary.

These models check *structure* only (shapes, types, allowed keys). Checks that need more than one object
(references, overlaps, cycles, fact types used by an expression) live in catalog.py, so that every problem in the
file can be reported in one pass instead of stopping at the first failed model.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Iterator, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    AfterValidator,
    Tag,
    WithJsonSchema,
    model_validator,
)

SNAKE_CASE = r"^[a-z][a-z0-9_]*$"
SEMVER = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"

SnakeId = Annotated[str, Field(pattern=SNAKE_CASE)]
NonEmpty = Annotated[str, Field(min_length=1)]

System = Literal["crm", "enrollment", "claims", "analytics", "customer_service", "care_management", "network"]
Role = Literal["analyst", "care_manager", "steward"]
Status = Literal["draft", "approved", "deprecated"]
FactType = Literal["date", "integer", "string", "boolean"]
Op = Literal["eq", "ne", "lt", "lte", "gt", "gte", "is_null", "not_null", "in"]

NULL_OPS = frozenset({"is_null", "not_null"})


class _Strict(BaseModel):
    # Unknown keys are errors: a typo such as `efective_to` must fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactSpec(_Strict):
    type: FactType
    nullable: bool = False
    # Name of a fact this one may not precede (coverage end before start is invalid input, not a rule outcome).
    not_before: SnakeId | None = None


# --- Rule expressions -------------------------------------------------------------------------------------------

Scalar = Union[StrictBool, StrictInt, StrictStr, date]


class RefOperand(_Strict):
    ref: Literal["requested_date"]


class FactOperand(_Strict):
    fact: SnakeId


def _operand_tag(value: Any) -> str | None:
    if isinstance(value, (RefOperand, FactOperand)):
        return "ref" if isinstance(value, RefOperand) else "fact"
    if isinstance(value, dict):
        return "ref" if "ref" in value else "fact" if "fact" in value else None
    return "list" if isinstance(value, list) else "scalar"


Operand = Annotated[
    Union[
        Annotated[RefOperand, Tag("ref")],
        Annotated[FactOperand, Tag("fact")],
        Annotated[list[Scalar], Field(min_length=1), Tag("list")],
        Annotated[Scalar, Tag("scalar")],
    ],
    Discriminator(
        _operand_tag,
        custom_error_type="invalid_operand",
        custom_error_message="operand must be a literal, a list, {ref: requested_date} or {fact: <name>}",
    ),
]


class Comparison(_Strict):
    fact: SnakeId
    op: Op
    value: Operand | None = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> Comparison:
        # "value absent" and "value: null" are different mistakes, so check fields_set rather than `is None`.
        has_value = "value" in self.model_fields_set
        if self.op in NULL_OPS:
            if has_value:
                raise ValueError(f"op '{self.op}' takes no value")
            return self
        if not has_value:
            raise ValueError(f"op '{self.op}' requires a value")
        if self.value is None:
            raise ValueError(f"op '{self.op}' cannot compare with null; use is_null or not_null")
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("op 'in' requires a non-empty list of literals")
        if self.op != "in" and isinstance(self.value, list):
            raise ValueError(f"op '{self.op}' does not accept a list")
        return self


class ConceptRef(_Strict):
    # Evaluates another concept's rule; this is how `depends_on` is executed instead of copied.
    concept: SnakeId


class AllExpr(_Strict):
    all: list[Expr] = Field(min_length=1)


class AnyExpr(_Strict):
    any: list[Expr] = Field(min_length=1)


class NotExpr(_Strict):
    not_: Expr = Field(alias="not")


_NODE_KEYS = ("all", "any", "not", "concept", "fact")
_NODE_TYPES = {AllExpr: "all", AnyExpr: "any", NotExpr: "not", ConceptRef: "concept", Comparison: "fact"}


def _expr_tag(value: Any) -> str | None:
    if isinstance(value, dict):
        # The first recognised key picks the node type; any other key is then rejected by extra="forbid".
        return next((key for key in _NODE_KEYS if key in value), None)
    return _NODE_TYPES.get(type(value))


Expr = Annotated[
    Union[
        Annotated[AllExpr, Tag("all")],
        Annotated[AnyExpr, Tag("any")],
        Annotated[NotExpr, Tag("not")],
        Annotated[ConceptRef, Tag("concept")],
        Annotated[Comparison, Tag("fact")],
    ],
    Discriminator(
        _expr_tag,
        custom_error_type="invalid_expression",
        custom_error_message="expression node must be a mapping with one of: all, any, not, concept, fact",
    ),
]

AllExpr.model_rebuild()
AnyExpr.model_rebuild()
NotExpr.model_rebuild()


def walk(expr: Expr) -> Iterator[Expr]:
    """Every node of an expression tree, depth first. Static: nothing is evaluated."""
    yield expr
    if isinstance(expr, (AllExpr, AnyExpr)):
        for child in expr.all if isinstance(expr, AllExpr) else expr.any:
            yield from walk(child)
    elif isinstance(expr, NotExpr):
        yield from walk(expr.not_)


def concept_refs(expr: Expr) -> set[str]:
    return {node.concept for node in walk(expr) if isinstance(node, ConceptRef)}


# --- Concepts ---------------------------------------------------------------------------------------------------


class Context(_Strict):
    system: System
    domain: NonEmpty


class AuthoritativeSource(_Strict):
    system: NonEmpty
    dataset: NonEmpty


class Rule(_Strict):
    # `text` is written by hand for people; only `expression` is executed. Keeping them in sync is stewardship.
    text: NonEmpty
    expression: Expr


class Relationship(_Strict):
    type: Literal["depends_on", "alternative_meaning_of", "supersedes"]
    target: NonEmpty  # a concept id or a term
    description: NonEmpty


class Concept(_Strict):
    id: SnakeId
    term: NonEmpty
    name: NonEmpty
    context: Context
    definition: NonEmpty
    aliases: list[NonEmpty] = []
    authoritative_source: AuthoritativeSource | None = None
    source_systems: list[NonEmpty] = []
    rule: Rule | None = None
    relationships: list[Relationship] = []
    owner: NonEmpty
    version: Annotated[str, Field(pattern=SEMVER)]
    status: Status
    effective_from: date
    effective_to: date | None = None
    superseded_by: SnakeId | None = None
    allowed_roles: list[Role] = Field(min_length=1)


def normalize_term(text: str) -> str:
    """Case-insensitive, `_`/`-` read as spaces, whitespace collapsed: "Active__Member " == "active member"."""
    return " ".join(text.casefold().replace("_", " ").replace("-", " ").split())


# --- Dates from callers ------------------------------------------------------------------------------------------

# [0-9], not \d: \d also matches non-ASCII digits. fromisoformat alone would accept "20260101" on 3.11.
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_iso_date(value: Any) -> date:
    """The one date rule for caller input (facts, as_of, requested_date). Raises ValueError, never TypeError."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass  # e.g. 2026-02-30: right shape, not a real date
    raise ValueError("expected a date as YYYY-MM-DD")


# Pydantic's lax `date` would read an integer as a Unix timestamp; this accepts only the ISO string form.
IsoDate = Annotated[date, PlainValidator(parse_iso_date), WithJsonSchema({"type": "string", "format": "date"})]


# --- API request bodies ------------------------------------------------------------------------------------------


class _Request(BaseModel):
    # Unknown fields are a 422, so a body cannot smuggle in e.g. {"role": "steward"}.
    model_config = ConfigDict(extra="forbid")


def _has_words(value: str) -> str:
    # "   " or "__" would normalise to nothing and silently match nothing; reject it instead.
    if not normalize_term(value):
        raise ValueError("must contain at least one character other than spaces, '_' or '-'")
    return value


class ResolveContext(_Request):
    # Free text on purpose: an unknown system such as "finance" must reach the resolver (context-matched-nothing
    # warning), not be rejected as a 422.
    system: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_has_words)] | None = None
    domain: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_has_words)] | None = None


class ResolveRequest(_Request):
    term: Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_has_words)]
    context: ResolveContext | None = None
    as_of: IsoDate | None = None  # None → today's UTC date


class EvaluateRequest(_Request):
    requested_date: IsoDate
    # Values are type-checked by the evaluator against the fact dictionary; only the count is bounded here.
    facts: Annotated[dict[str, Any], Field(max_length=100)]
