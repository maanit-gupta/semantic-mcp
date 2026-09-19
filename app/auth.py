"""API keys mapped to roles: a demo stand-in for real identity (production would use OAuth, see docs/ASSUMPTIONS.md).

Keys are loaded once at startup from a YAML file outside version control; a bad file stops the app. The key value is
kept only as a SHA-256 digest and never appears in an error message. `is_visible` is the one access predicate for
concept content; every route and the resolver go through it.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import Concept, Role

# Restricted-visibility policy (brief §7). "existence": a caller without the role learns that a restricted concept
# exists (list entry, resolve restricted_count, 403) but never its content. "hidden" (behave as if the concept did
# not exist) is a production option that is not built; see PROGRESS.md.
RESTRICTED_VISIBILITY: Literal["existence"] = "existence"

API_KEY_HEADER = "X-API-Key"
MIN_KEY_LENGTH = 16


@dataclass(frozen=True)
class Caller:
    label: str
    role: Role


def is_visible(caller: Caller, concept: Concept) -> bool:
    """May this caller see the concept's content? The only place this decision is made."""
    return caller.role in concept.allowed_roles


class KeyConfigError(Exception):
    def __init__(self, path: Path, issues: list[str]):
        self.issues = issues
        lines = "\n".join(f"  - {issue}" for issue in issues)
        super().__init__(f"API key file {path} is invalid ({len(issues)} issue(s)):\n{lines}")


class _KeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    role: Role
    key: Annotated[str, Field(min_length=MIN_KEY_LENGTH, max_length=256)]


class _KeyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: Annotated[list[_KeyEntry], Field(min_length=1)]


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


class KeyStore:
    def __init__(self, entries: list[tuple[str, Role, str]]):
        self._entries = [(label, role, _digest(key)) for label, role, key in entries]

    def authenticate(self, presented: str | None) -> Caller | None:
        if not presented:
            return None
        # Digests make every comparison the same length, and bytes avoid compare_digest's TypeError on non-ASCII str.
        # No early exit: the loop takes the same time whichever key (if any) matches.
        digest = _digest(presented)
        match: Caller | None = None
        for label, role, stored in self._entries:
            if hmac.compare_digest(digest, stored):
                match = Caller(label, role)
        return match


def load_keys(path: Path) -> KeyStore:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyConfigError(path, [
            f"cannot read the file ({exc.strerror}); copy config/api_keys.example.yaml to config/api_keys.yaml "
            "or set SEMANTIC_API_KEYS_FILE"
        ]) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML's message quotes the offending line, which may hold a key: report the position only.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise KeyConfigError(path, [f"invalid YAML{where}"]) from None
    try:
        parsed = _KeyFile.model_validate(data)
    except ValidationError as exc:
        # loc + msg only: str(exc) would include the input value, i.e. possibly a key.
        issues = [f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()]
        raise KeyConfigError(path, issues) from None

    issues: list[str] = []
    seen_labels: set[str] = set()
    seen_keys: dict[str, str] = {}
    for index, entry in enumerate(parsed.keys):
        if entry.label in seen_labels:
            issues.append(f"keys.{index}.label: duplicate label '{entry.label}'")
        seen_labels.add(entry.label)
        if entry.key in seen_keys:
            issues.append(f"keys.{index}.key: same key as '{seen_keys[entry.key]}'")
        seen_keys.setdefault(entry.key, entry.label)
    if issues:
        raise KeyConfigError(path, issues)
    return KeyStore([(e.label, e.role, e.key) for e in parsed.keys])
