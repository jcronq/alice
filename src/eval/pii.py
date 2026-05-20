"""Conservative PII redaction for the speaking-quality eval.

The design (``2026-05-15-speaking-quality-eval-design.md`` Privacy
section) calls for "Mitigation A: redact phone numbers, addresses,
full names of non-Jason individuals" before sending text to non-
Anthropic providers. We narrow that to three regex-driven rules:

- E.164-ish and US phone numbers.
- Email addresses.
- ``/home/<user>/...`` paths — collapsed to ``/home/<REDACTED>/...``
  so the path *shape* survives for context but the user identifier
  doesn't.

Anything fancier (NER over names, address parsing) is out of scope —
the design explicitly says basic regex is fine. Names like "Jason"
or "Katie" are intentionally preserved; they're already public in
the agent persona and stripping them would gut the conversation's
meaning.

Functions are pure and stateless. ``redact`` returns a new string;
the input is not mutated.
"""

from __future__ import annotations

import re

__all__ = ["PHONE_PATTERN", "EMAIL_PATTERN", "HOME_PATH_PATTERN", "redact"]


# Allows ``+14357091512``, ``435-709-1512``, ``(435) 709-1512``,
# ``435.709.1512`` and ``4357091512``. The leading ``\+?1?`` is optional
# country code; the ``\(?\)?`` lets parenthesised area codes through.
PHONE_PATTERN = re.compile(
    r"\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
)

# Standard-ish email. RFC-5322 is a rabbit hole; this covers the
# overwhelming majority of real-world addresses.
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

# ``/home/<user>/<rest>`` — preserve the leading segment so downstream
# context like "the path under /home/.../alice-mind/..." stays
# parseable. We only capture the user segment for replacement; the
# tail is passed through verbatim.
HOME_PATH_PATTERN = re.compile(r"/home/([^/\s]+)(/[^\s]*)?")


def redact(text: str) -> str:
    """Return ``text`` with phone numbers, emails, and ``/home/<user>``
    paths replaced by stable placeholders.

    Conservative by design: does not touch names, addresses, IPs, or
    URLs. Pure function; idempotent up to the placeholder text.
    """
    if not text:
        return text

    redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)

    def _home_sub(match: re.Match[str]) -> str:
        tail = match.group(2) or ""
        return f"/home/[REDACTED_USER]{tail}"

    redacted = HOME_PATH_PATTERN.sub(_home_sub, redacted)
    return redacted
