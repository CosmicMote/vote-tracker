"""Prompt text and output schema for bill summaries. Bump llm.prompt_version when these change."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

SYSTEM_PROMPT = """\
You summarize U.S. legislation for a general reader.
Describe what the bill would do, using only the material provided.
Do not say whether the bill is good or bad, who supports or opposes it,
or what its effects will be beyond what the text states.
Do not use the bill's own promotional language; describe its mechanics.
If the material is truncated or unclear, say so in the caveat field.
Return only JSON matching the schema."""

BASIS_LABELS = {
    "crs": "Congressional Research Service summary",
    "bill_text": "Bill text",
}


class BillSummaryOut(BaseModel):
    plain_title: str = Field(description="At most 12 words, neutral")
    what_it_does: list[str] = Field(description="2 to 4 bullets, each at most 30 words")
    caveat: str | None = None

    @field_validator("plain_title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("plain_title is empty")
        if len(v.split()) > 12:
            raise ValueError(f"plain_title has {len(v.split())} words; at most 12 allowed")
        return v

    @field_validator("what_it_does")
    @classmethod
    def _bullets(cls, v: list[str]) -> list[str]:
        v = [b.strip() for b in v if b and b.strip()]
        if not 2 <= len(v) <= 4:
            raise ValueError(f"what_it_does has {len(v)} bullets; 2 to 4 required")
        for i, b in enumerate(v, 1):
            if len(b.split()) > 30:
                raise ValueError(f"bullet {i} has {len(b.split())} words; at most 30 allowed")
        return v

    @field_validator("caveat")
    @classmethod
    def _caveat(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


# Sent to providers. Ollama's constrained decoding enforces the array bounds; providers that do
# not support them strip them (see llm/anthropic.py). Word limits are enforced only by the
# pydantic model above.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "plain_title": {"type": "string", "description": "At most 12 words, neutral"},
        "what_it_does": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
            "description": "2 to 4 bullets, each at most 30 words",
        },
        "caveat": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["plain_title", "what_it_does", "caveat"],
    "additionalProperties": False,
}


def user_message(
    bill_number: str,
    title: str | None,
    short_title: str | None,
    policy_area: str | None,
    basis: str,
    basis_text: str,
) -> str:
    lines = [
        f"Bill number: {bill_number}",
        f"Official title: {title or '(none published)'}",
    ]
    if short_title:
        lines.append(f"Short title: {short_title}")
    lines.append(f"Policy area: {policy_area or '(not assigned)'}")
    lines.append(f"Material provided: {BASIS_LABELS[basis]}")
    lines.append("")
    lines.append(basis_text)
    lines.append("")
    lines.append(
        "Output: plain_title of at most 12 words; what_it_does with 2 to 4 bullets of at most "
        "30 words each; caveat as a string or null."
    )
    return "\n".join(lines)
