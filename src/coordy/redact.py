"""Replace real names with codes (P01, ORG01, ...) and back again, locally."""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("person", "org", "project", "term")
CODE_RE = re.compile(r"^[A-Z]{1,6}\d{1,4}$")
ARABIC_RE = re.compile(r"[؀-ۿ]")
# Arabic attaches these to the next word: و (and), ف (so), ب (with), ل (to), ك (like), ال (the).
ARABIC_PREFIX = r"(?:[وف]?(?:[بلك]|ال)?)"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# International (+971..., 0020...) or local mobile numbers (010..., 050...).
PHONE_RE = re.compile(r"(?<!\w)(?:(?:\+|00)\d[\d\s\-]{7,15}\d|0\d{9,11})(?!\w)")


class AliasError(ValueError):
    pass


@dataclass
class Alias:
    kind: str
    name: str
    code: str
    role: str = ""
    variants: list[str] = field(default_factory=list)

    @property
    def spellings(self) -> list[str]:
        return [s for s in [self.name, *self.variants] if s.strip()]


def load_aliases(path: Path) -> list[Alias]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    aliases: list[Alias] = []
    seen: dict[str, str] = {}
    for kind in KINDS:
        for entry in data.get(kind, []):
            alias = Alias(
                kind=kind,
                name=str(entry.get("name", "")).strip(),
                code=str(entry.get("code", "")).strip(),
                role=str(entry.get("role", "")).strip(),
                variants=[str(v).strip() for v in entry.get("variants", [])],
            )
            if not alias.name or not CODE_RE.match(alias.code):
                raise AliasError(f"{path.name}: [[{kind}]] needs a name and a code like P01 (got {alias.code!r})")
            if alias.code in seen and seen[alias.code] != alias.name:
                raise AliasError(f"{path.name}: code {alias.code} is used twice")
            seen[alias.code] = alias.name
            aliases.append(alias)
    return aliases


def _spelling_pattern(spelling: str) -> str:
    body = r"\s+".join(re.escape(part) for part in spelling.split())
    if ARABIC_RE.search(spelling):
        return rf"(?<!\w)(?P<pre>{ARABIC_PREFIX}){body}(?!\w)"
    return rf"(?<!\w)(?P<pre>){body}(?!\w)"


class Redactor:
    def __init__(self, aliases: list[Alias], *, emails: bool = True, phones: bool = True):
        self.aliases = aliases
        self.emails = emails
        self.phones = phones
        # Longest spellings first so "Omar Elsayed" wins over "Omar".
        pairs = sorted(
            ((s, a.code) for a in aliases for s in a.spellings),
            key=lambda p: len(p[0]),
            reverse=True,
        )
        self._patterns = [(re.compile(_spelling_pattern(s), re.IGNORECASE), code) for s, code in pairs]
        self._codes = {a.code: a for a in aliases}
        self._code_re = (
            re.compile(r"(?<![\w])(" + "|".join(map(re.escape, sorted(self._codes, key=len, reverse=True))) + r")(?![\w])")
            if self._codes
            else None
        )

    @classmethod
    def from_settings(cls, settings) -> "Redactor":
        return cls(
            load_aliases(settings.paths.aliases),
            emails=settings.privacy.redact_emails,
            phones=settings.privacy.redact_phones,
        )

    @property
    def codes(self) -> set[str]:
        return set(self._codes)

    def redact(self, text: str, hits: Counter | None = None) -> str:
        if not text:
            return text
        for pattern, code in self._patterns:
            def repl(m: re.Match, code: str = code) -> str:
                if hits is not None:
                    hits[code] += 1
                return m.group("pre") + code

            text = pattern.sub(repl, text)
        if self.emails:
            text = EMAIL_RE.sub("[EMAIL]", text)
        if self.phones:
            text = PHONE_RE.sub("[PHONE]", text)
        return text

    def unredact(self, text: str) -> str:
        """Put real names back. Only for files that stay on this computer."""
        if not text or self._code_re is None:
            return text
        return self._code_re.sub(lambda m: self._codes[m.group(1)].name, text)

    def guard(self, value):
        """Redact every string inside a JSON-like value (defence in depth for MCP output)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.guard(v) for v in value]
        if isinstance(value, dict):
            return {k: self.guard(v) for k, v in value.items()}
        return value

    def public_codes(self) -> list[dict]:
        """Codes and roles only - safe to share with Claude."""
        return [{"code": a.code, "kind": a.kind, "role": a.role} for a in self.aliases]
