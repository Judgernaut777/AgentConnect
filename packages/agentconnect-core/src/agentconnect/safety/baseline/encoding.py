"""Long encoded blobs.

An encoded blob is not dangerous. It is *opaque*, and opacity defeats every other
rule in this module: a base64 payload sails past the secret patterns and the
injection patterns alike, and is then decoded by whatever reads it.

So this rule never blocks and never redacts. It warns, and it labels, so a human
reading the artifact metadata knows there is something here that the other
scanners could not see into. Diffs, logs, and inline images are full of long
opaque runs; treating them as threats would make the layer useless.

The threshold is deliberately high. Below it, false positives swamp the signal.
"""

from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel

#: A base64 run long enough that nothing incidental produces it. 256 characters is
#: ~192 decoded bytes — far past a checksum, an id, or a short hash.
MIN_BLOB_CHARS = 256

BASE64_BLOB = re.compile(rf"[A-Za-z0-9+/]{{{MIN_BLOB_CHARS},}}={{0,2}}")
#: URL-safe alphabet, as used by JWTs and many token formats.
BASE64URL_BLOB = re.compile(rf"[A-Za-z0-9_\-]{{{MIN_BLOB_CHARS},}}")
HEX_BLOB = re.compile(rf"(?i)\b[0-9a-f]{{{MIN_BLOB_CHARS},}}\b")


def find(text: str) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[int, int]] = set()
    for rule_id, pattern, message in (
        ("encoding.base64_blob", BASE64_BLOB, "Long base64-like blob; contents not scanned."),
        ("encoding.base64url_blob", BASE64URL_BLOB,
         "Long base64url-like blob; contents not scanned."),
        ("encoding.hex_blob", HEX_BLOB, "Long hex blob; contents not scanned."),
    ):
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if span in seen:  # the two base64 alphabets overlap on most inputs
                continue
            seen.add(span)
            findings.append(Finding(
                rule_id=rule_id, category=Category.encoding, risk_level=RiskLevel.low,
                message=f"{message} ({match.end() - match.start()} chars)",
                start=match.start(), end=match.end(),
            ))
    return findings
