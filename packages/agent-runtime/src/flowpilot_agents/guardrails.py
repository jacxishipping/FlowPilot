"""Pre-LLM PII scrubbing, prompt-injection detection, and JSON output schema guardrails."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\w)")
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_API_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("system_override", re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior)\s+instructions")),
    ("prompt_exfil", re.compile(r"(?i)(reveal|print|dump)\s+(your\s+)?(system\s+prompt|secrets)")),
    ("role_hijack", re.compile(r"(?i)you\s+are\s+now\s+(in\s+)?(developer|admin|DAN)\s+mode")),
    ("tool_bypass", re.compile(r"(?i)bypass\s+(all\s+)?(approval|policy|security)\s+checks")),
)


def _luhn_valid(digits: str) -> bool:
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


class PiiScrubResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scrubbed_text: str
    detected_types: list[str] = Field(default_factory=list)
    redaction_count: int = 0


class PromptInjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    flagged: bool
    matched_rules: list[str] = Field(default_factory=list)


def scrub_pii(text: str) -> PiiScrubResult:
    """Detect and mask emails, SSNs, Luhn-valid credit cards, phone numbers, and API keys."""
    detected: set[str] = set()
    count = 0
    out = text

    def _sub(pattern: re.Pattern[str], label: str, replacement: str, target: str) -> str:
        nonlocal count
        matches = list(pattern.finditer(target))
        if matches:
            detected.add(label)
            count += len(matches)
            return pattern.sub(replacement, target)
        return target

    out = _sub(_API_KEY_RE, "api_key", "[REDACTED_API_KEY]", out)
    out = _sub(_SSN_RE, "ssn", "[REDACTED_SSN]", out)
    out = _sub(_EMAIL_RE, "email", "[REDACTED_EMAIL]", out)
    out = _sub(_PHONE_RE, "phone", "[REDACTED_PHONE]", out)

    def _card_repl(match: re.Match[str]) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            detected.add("credit_card")
            count += 1
            return "[REDACTED_CARD]"
        return match.group(0)

    out = _CARD_CANDIDATE_RE.sub(_card_repl, out)
    return PiiScrubResult(
        scrubbed_text=out,
        detected_types=sorted(detected),
        redaction_count=count,
    )


def detect_prompt_injection(text: str) -> PromptInjectionResult:
    """Detect common prompt-injection and policy-bypass attack patterns."""
    matched = [name for name, pat in _INJECTION_PATTERNS if pat.search(text)]
    return PromptInjectionResult(flagged=len(matched) > 0, matched_rules=matched)


def validate_json_output_schema(raw_text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Parse LLM JSON response and validate required keys & basic JSON Schema types."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError as exc:
        raise ValueError(f"Model output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Model JSON output must be a JSON object.")

    for req_key in schema.get("required", []):
        if req_key not in parsed:
            raise ValueError(f"Model JSON output missing required key '{req_key}'.")

    type_map: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for prop, prop_spec in schema.get("properties", {}).items():
        if prop in parsed and isinstance(prop_spec, dict) and "type" in prop_spec:
            expected_t = type_map.get(str(prop_spec["type"]))
            if expected_t is not None and not isinstance(parsed[prop], expected_t):
                raise ValueError(
                    f"Key '{prop}' expected type '{prop_spec['type']}', "
                    f"got {type(parsed[prop]).__name__}."
                )
    return parsed
