"""Replace matched spans with a marker that says what was removed.

Two properties matter more than they look:

1. **Spans are merged before replacement.** `sk-ant-…` matches both the Anthropic
   and the generic OpenAI rule. Replacing them independently, in any order,
   corrupts every offset after the first substitution and can splice a marker into
   the middle of the second match — leaving half the credential in the text.
2. **The marker names the category and rule, never the value.** A redaction that
   printed what it removed would be a leak with a receipt.
"""

from __future__ import annotations

from .models import Finding

MARKER = "[REDACTED:{category}:{rule_id}]"


def merge_spans(findings: list[Finding]) -> list[tuple[int, int, Finding]]:
    """Non-overlapping, left-to-right. The first finding of an overlapping run
    names the merged span, so the marker reports the most specific rule that fired
    at that position."""
    ordered = sorted(findings, key=lambda f: (f.start, -f.end))
    merged: list[tuple[int, int, Finding]] = []
    for finding in ordered:
        if finding.end <= finding.start:
            continue
        if merged and finding.start < merged[-1][1]:
            start, end, first = merged[-1]
            merged[-1] = (start, max(end, finding.end), first)
        else:
            merged.append((finding.start, finding.end, finding))
    return merged


def redact(text: str, findings: list[Finding]) -> str:
    """Rebuild `text` with every finding's span replaced by its marker."""
    if not findings:
        return text
    out: list[str] = []
    cursor = 0
    for start, end, finding in merge_spans(findings):
        if start < cursor:  # already consumed by an earlier merged span
            continue
        out.append(text[cursor:start])
        out.append(MARKER.format(category=finding.category.value, rule_id=finding.rule_id))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)
