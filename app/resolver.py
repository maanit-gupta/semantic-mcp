"""Deterministic term resolver: term + optional context + as_of + caller → approved meaning, or candidates.

A pure function (no HTTP, no clock, no I/O) so it is testable directly and reusable by the eval runner. It never
chooses between several candidates, and restricted concepts are carried only as a count: `Resolution` has no field
that could hold one. See PROGRESS.md D21 for the decision table.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .auth import Caller, is_visible
from .catalog import Catalog, term_keys
from .models import Concept, normalize_term

Status = Literal["resolved", "ambiguous", "not_found", "restricted"]


@dataclass(frozen=True)
class Resolution:
    status: Status
    term: str  # normalised
    as_of: date
    concept: Concept | None = None  # set only when status == "resolved"
    candidates: tuple[Concept, ...] = ()  # visible concepts only, ordered by (context.system, id)
    clarifying_question: str | None = None
    suggestions: tuple[str, ...] = ()
    restricted_count: int = 0
    warnings: tuple[str, ...] = ()


def _order(concept: Concept) -> tuple[str, str]:
    return (concept.context.system, concept.id)


def _effective_concepts(catalog: Catalog, as_of: date) -> list[Concept]:
    # One non-draft version per id, the one effective on as_of; everything else is invisible to resolution.
    found = (catalog.effective(concept_id, as_of) for concept_id in catalog.concepts)
    return sorted((c for c in found if c is not None), key=_order)


def _join(options: list[str]) -> str:
    return options[0] if len(options) == 1 else ", ".join(options[:-1]) + " or " + options[-1]


def _context_text(system: str | None, domain: str | None) -> str:
    parts = [f"system '{system}'" if system is not None else "", f"domain '{domain}'" if domain is not None else ""]
    return " and ".join(p for p in parts if p)


def resolve(
    catalog: Catalog,
    term: str,
    as_of: date,
    caller: Caller,
    system: str | None = None,
    domain: str | None = None,
) -> Resolution:
    wanted = normalize_term(term)
    want_system = normalize_term(system) if system is not None else None
    want_domain = normalize_term(domain) if domain is not None else None
    effective = _effective_concepts(catalog, as_of)
    matched = [c for c in effective if wanted in term_keys(c)]

    if not matched:
        # Suggestions come only from what this caller can see, so they cannot reveal a restricted term.
        vocabulary = sorted({key for c in effective if is_visible(caller, c) for key in term_keys(c)})
        suggestions = tuple(difflib.get_close_matches(wanted, vocabulary, n=3, cutoff=0.6))
        return Resolution("not_found", wanted, as_of, suggestions=suggestions)

    warnings: list[str] = []
    pool = matched
    contradicted = False
    if want_system is not None or want_domain is not None:
        narrowed = [
            c for c in matched
            if (want_system is None or normalize_term(c.context.system) == want_system)
            and (want_domain is None or normalize_term(c.context.domain) == want_domain)
        ]
        if narrowed:
            pool = narrowed
        else:
            # Never fall back silently: keep every meaning, say so, and refuse to resolve.
            contradicted = True
            warnings.append(
                f"context {_context_text(system, domain)} matched none of the {len(matched)} meaning(s) of "
                f"'{wanted}'; showing every meaning you can access"
            )

    visible = [c for c in pool if is_visible(caller, c)]
    restricted_count = len(pool) - len(visible)
    if restricted_count:
        other = "other " if visible else ""
        warnings.append(f"{restricted_count} {other}meaning(s) of '{wanted}' exist that your role cannot access")
    warnings.extend(
        f"'{c.id}' is deprecated; use '{c.superseded_by}' instead" for c in visible if c.status == "deprecated"
    )

    if not visible:
        return Resolution("restricted", wanted, as_of, restricted_count=restricted_count, warnings=tuple(warnings))
    if len(visible) == 1 and not contradicted:
        return Resolution("resolved", wanted, as_of, concept=visible[0],
                          restricted_count=restricted_count, warnings=tuple(warnings))

    if len(visible) == 1:
        (only,) = visible
        question = (
            f"No meaning of '{wanted}' matches {_context_text(system, domain)}. The only meaning available to you is "
            f"'{only.name}' ({only.context.system}): {only.definition}. Is that the meaning you need?"
        )
    else:
        systems = [c.context.system for c in visible]
        options = systems if len(set(systems)) == len(systems) else [c.id for c in visible]
        question = f"Which meaning of '{wanted}' do you need: {_join(options)}?"
    return Resolution("ambiguous", wanted, as_of, candidates=tuple(visible), clarifying_question=question,
                      restricted_count=restricted_count, warnings=tuple(warnings))
