"""Pydantic models for the catalog: concepts, rule expressions and the fact dictionary.

These models check *structure* only (shapes, types, allowed keys). Checks that need more than one object
(references, overlaps, cycles, fact types used by an expression) live in catalog.py, so that every problem in the
file can be reported in one pass instead of stopping at the first failed model.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    Tag,
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
