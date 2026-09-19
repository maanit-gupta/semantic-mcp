"""FastAPI app: routes stay thin and delegate to catalog, rules and resolver, which are testable without HTTP.

One middleware authenticates (deny by default, before routing) and writes the audit line after the response, so no
request can skip either. The catalog and key file are validated when the app is built: bad config stops the process.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam

from . import rules
from .audit import AuditLog, clip, outcome_for_status
from .auth import API_KEY_HEADER, Caller, is_visible, load_keys
from .catalog import Catalog, load_catalog, term_keys
from .errors import ApiError, AsciiJSONResponse, error_response, install_error_handlers
from .models import Concept, EvaluateRequest, IsoDate, ResolveRequest, normalize_term
from .resolver import resolve

ROOT = Path(__file__).resolve().parent.parent  # paths are resolved from this file, not the working directory
DEFAULT_CATALOG_PATH = ROOT / "catalog" / "concepts.yaml"
DEFAULT_KEYS_PATH = ROOT / "config" / "api_keys.yaml"
DEFAULT_AUDIT_PATH = ROOT / "audit" / "audit.jsonl"

# Everything else needs a key. Docs are public for the demo (documented in README/ASSUMPTIONS).
PUBLIC_PATHS = frozenset({"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

ConceptId = Annotated[str, PathParam(min_length=1, max_length=100)]
AsOf = Annotated[IsoDate | None, Query(description="YYYY-MM-DD; defaults to today's UTC date")]


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def deprecation_warnings(concept: Concept) -> list[str]:
    if concept.status != "deprecated":
        return []
    return [f"'{concept.id}' is deprecated; use '{concept.superseded_by}' instead"]


def concept_view(concept: Concept) -> dict[str, Any]:
    """Full definition. Only ever called on a concept the caller may see (checked by is_visible first)."""
    data = concept.model_dump(mode="json", by_alias=True, exclude={"rule"})
    data["rule"] = concept.rule.model_dump(mode="json", by_alias=True, exclude_unset=True) if concept.rule else None
    data["warnings"] = deprecation_warnings(concept)
    return data


def candidate_view(concept: Concept) -> dict[str, Any]:
    return {
        "id": concept.id, "name": concept.name, "term": concept.term, "context": concept.context.model_dump(),
        "definition": concept.definition, "version": concept.version, "status": concept.status,
    }


# Resolve outcomes map onto the audit vocabulary; a restricted answer is a denial even though it is HTTP 200.
RESOLVE_OUTCOMES = {"resolved": "ok", "ambiguous": "ambiguous", "not_found": "not_found", "restricted": "denied"}


def create_app(
    catalog_path: Path = DEFAULT_CATALOG_PATH, keys_path: Path | None = None, audit_path: Path | None = None
) -> FastAPI:
    catalog: Catalog = load_catalog(catalog_path)  # raises CatalogError listing every issue
    keys = load_keys(keys_path or _env_path("SEMANTIC_API_KEYS_FILE", DEFAULT_KEYS_PATH))  # raises KeyConfigError
    audit = AuditLog(audit_path or _env_path("SEMANTIC_AUDIT_LOG", DEFAULT_AUDIT_PATH))

    app = FastAPI(
        title="Ryan-MCP semantic glossary",
        version="0.1.0",
        description=f"Read-only glossary of approved business meanings. Send an `{API_KEY_HEADER}` header.",
        default_response_class=AsciiJSONResponse,
    )
    app.state.catalog = catalog
    app.state.audit = audit
    install_error_handlers(app)

    @app.middleware("http")
    async def authenticate_and_audit(request: Request, call_next):
        state = request.state
        state.caller, state.audit_params, state.audit_outcome, state.error_code = None, {}, None, None
        if request.url.path in PUBLIC_PATHS:
            response = await call_next(request)
        else:
            # Exactly one key header: with several, a proxy and this app could disagree about which one counts.
            presented = request.headers.getlist(API_KEY_HEADER)
            state.caller = keys.authenticate(presented[0]) if len(presented) == 1 else None
            if state.caller is None:
                # Same body for a missing key, a wrong key, an unknown path or an invalid body: nothing is learned.
                state.error_code = "unauthenticated"
                response = error_response(401, "unauthenticated", f"missing or invalid {API_KEY_HEADER} header")
            else:
                try:
                    response = await call_next(request)
                except Exception:  # anything a route did not handle: still enveloped, still audited
                    state.error_code = "internal_error"
                    response = error_response(500, "internal_error", "internal error")

        caller: Caller | None = state.caller
        route = request.scope.get("route")
        audit.write(
            {
                "key_label": caller.label if caller else None,
                "role": caller.role if caller else None,
                # X-Via is untrusted metadata: mapped to a fixed value, never copied, never used for authorization.
                "via": "mcp" if request.headers.get("x-via") == "mcp" else "api",
                "method": request.method,
                "endpoint": route.path if route is not None else clip(request.url.path),
                "params": clip(state.audit_params),
                "status_code": response.status_code,
                "error_code": state.error_code,
                "outcome": state.audit_outcome or outcome_for_status(response.status_code),
            }
        )
        return response

    def visible_concept(caller: Caller, concept_id: str, on: date) -> Concept:
        """Shared 404 → 404 → 403 order for GET, relationships and evaluate."""
        if not any(v.status != "draft" for v in catalog.versions(concept_id)):
            raise ApiError(404, "not_found", f"no concept '{concept_id}'", {"concept_id": concept_id})
        concept = catalog.effective(concept_id, on)
        if concept is None:
            raise ApiError(
                404, "not_effective", f"concept '{concept_id}' has no version effective on {on.isoformat()}",
                {"concept_id": concept_id, "as_of": on.isoformat()},
            )
        if not is_visible(caller, concept):
            raise ApiError(
                403, "forbidden", f"your role cannot access '{concept_id}'",
                {"concept_id": concept_id, "required_role": list(concept.allowed_roles)},
            )
        return concept

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}  # no catalog content, so public

    @app.get("/semantic/concepts")
    def list_concepts(
        request: Request,
        term: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        status: Literal["approved", "deprecated"] | None = None,
        system: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    ) -> dict[str, Any]:
        request.state.audit_params = {k: v for k, v in {"term": term, "status": status, "system": system}.items() if v}
        caller: Caller = request.state.caller
        wanted_term = normalize_term(term) if term is not None else None
        wanted_system = system.strip().casefold() if system is not None else None
        entries: list[dict[str, Any]] = []
        for concept_id in sorted(catalog.concepts):
            restricted_listed = False
            for c in catalog.versions(concept_id):
                if c.status == "draft":  # drafts never appear through the API
                    continue
                if wanted_term is not None and wanted_term not in term_keys(c):
                    continue
                if (status and c.status != status) or (wanted_system and c.context.system != wanted_system):
                    continue
                if is_visible(caller, c):
                    entries.append(
                        {
                            "id": c.id, "name": c.name, "term": c.term, "context": c.context.model_dump(),
                            "definition": c.definition, "version": c.version, "status": c.status,
                            "effective_from": c.effective_from.isoformat(),
                            "effective_to": c.effective_to.isoformat() if c.effective_to else None,
                            "access": "full",
                        }
                    )
                elif not restricted_listed:  # existence only: no definition, no rule, no version detail
                    entries.append({"id": c.id, "name": c.name, "term": c.term, "access": "restricted"})
                    restricted_listed = True
        return {"count": len(entries), "concepts": entries}

    @app.get("/semantic/concepts/{concept_id}")
    def get_concept(request: Request, concept_id: ConceptId, as_of: AsOf = None) -> dict[str, Any]:
        on = as_of or today_utc()
        request.state.audit_params = {"concept_id": concept_id, "as_of": on.isoformat()}
        concept = visible_concept(request.state.caller, concept_id, on)
        return {"as_of": on.isoformat(), **concept_view(concept)}

    @app.get("/semantic/concepts/{concept_id}/relationships")
    def get_relationships(request: Request, concept_id: ConceptId, as_of: AsOf = None) -> dict[str, Any]:
        on = as_of or today_utc()
        request.state.audit_params = {"concept_id": concept_id, "as_of": on.isoformat()}
        caller: Caller = request.state.caller
        concept = visible_concept(caller, concept_id, on)
        incoming: list[dict[str, Any]] = []
        for other_id in sorted(catalog.concepts):
            other = catalog.effective(other_id, on)
            if other is None or other_id == concept_id:
                continue
            for rel in other.relationships:
                if rel.target != concept_id:
                    continue
                if is_visible(caller, other):
                    incoming.append({"source": other_id, "type": rel.type, "description": rel.description})
                else:  # the neighbour's existence is visible, its description is content
                    incoming.append({"source": other_id, "type": rel.type, "access": "restricted"})
        return {
            "concept_id": concept_id,
            "version": concept.version,
            "as_of": on.isoformat(),
            "outgoing": [rel.model_dump() for rel in concept.relationships],
            "incoming": incoming,
        }

    @app.post("/semantic/concepts/{concept_id}/evaluate")
    def evaluate_concept(request: Request, concept_id: ConceptId, body: EvaluateRequest) -> dict[str, Any]:
        on = body.requested_date
        # Fact names only, never values: the audit log must not hold person-level data.
        request.state.audit_params = {"concept_id": concept_id, "requested_date": on.isoformat(), "fact_names": sorted(body.facts)}
        # Access is checked on the target before the facts are looked at, so a caller without the role cannot
        # learn a restricted rule's required facts from a 422. Dependencies are evaluated internally.
        visible_concept(request.state.caller, concept_id, on)
        try:
            evaluation = rules.evaluate(catalog, concept_id, on, body.facts)
        except rules.InsufficientContext as exc:
            raise ApiError(422, "insufficient_context", str(exc), {"missing_facts": exc.missing_facts}) from None
        except rules.InvalidFacts as exc:
            raise ApiError(422, "invalid_facts", "one or more facts are invalid", {"errors": exc.errors}) from None
        except (rules.NoRule, rules.ConceptNotEffective, rules.ConceptNotFound) as exc:
            raise ApiError(422, "not_evaluable", str(exc), {"concept_id": concept_id}) from None
        concept = evaluation.concept  # the version effective on requested_date, i.e. the one that was executed
        return {
            "concept_id": concept_id,
            "result": evaluation.result,
            "rule_text": concept.rule.text if concept.rule else None,
            "source": concept.authoritative_source.model_dump() if concept.authoritative_source else None,
            "version": concept.version,
            "status": concept.status,
            "requested_date": on.isoformat(),
            "warnings": deprecation_warnings(concept),
        }

    @app.post("/semantic/resolve")
    def resolve_term(request: Request, body: ResolveRequest) -> dict[str, Any]:
        on = body.as_of or today_utc()
        context = body.context.model_dump(exclude_none=True) if body.context else {}
        request.state.audit_params = {"term": body.term, "context": context, "as_of": on.isoformat()}
        result = resolve(catalog, body.term, on, request.state.caller, context.get("system"), context.get("domain"))
        request.state.audit_outcome = RESOLVE_OUTCOMES[result.status]
        # The four outcomes are answers, not errors (brief §6), so all are HTTP 200.
        return {
            "status": result.status,
            "term": result.term,
            "as_of": on.isoformat(),
            "concept": concept_view(result.concept) if result.concept else None,
            "candidates": [candidate_view(c) for c in result.candidates],
            "clarifying_question": result.clarifying_question,
            "suggestions": list(result.suggestions),
            "restricted_count": result.restricted_count,
            "warnings": list(result.warnings),
        }

    @app.get("/semantic/audit")
    def read_audit(request: Request, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> dict[str, Any]:
        request.state.audit_params = {"limit": limit}
        if request.state.caller.role != "steward":
            raise ApiError(403, "forbidden", "the audit log is available to stewards only", {"required_role": ["steward"]})
        entries = audit.tail(limit)
        return {"count": len(entries), "entries": entries}

    return app


app = create_app()
