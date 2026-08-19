"""E.164 phone normalization for conversational evidence.

Kept intentionally narrow — this pipeline is US-first (Spotless Homes,
Callio, LeadBridge all serve US customers today). We prefer a small,
zero-dependency helper to importing `phonenumbers` (adds ~2MB of country
metadata for one country's worth of use).

Contract:

- `normalize_e164(value, default_country='US')` returns a full E.164
  string like '+18135551234' on success, or `None` on failure.
- We never invent a country code. If the input is 10 digits and
  `default_country='US'`, we prepend '+1'. Any other length + no leading
  '+' is treated as unparseable — no silent country-code guessing.
- Extension suffixes (`x123`, `ext. 45`) are stripped; the base number is
  what identifies the customer.

To upgrade to full international parsing later, swap the implementation
of `normalize_e164()` — every caller in this codebase uses this one
function.
"""

from __future__ import annotations

import re
from typing import Optional

_DIGIT_RE = re.compile(r'\d')
_EXT_RE = re.compile(r'(?i)(?:x|ext\.?|extension)\s*\d+\s*$')


def normalize_e164(value: Optional[str], *, default_country: str = 'US') -> Optional[str]:
    """Return a full E.164 string ('+18135551234') or None on failure.

    Accepts common US formats:
      '(813) 555-1234'  → '+18135551234'
      '813-555-1234'    → '+18135551234'
      '813.555.1234'    → '+18135551234'
      '8135551234'      → '+18135551234'
      '18135551234'     → '+18135551234'
      '+18135551234'    → '+18135551234'
      '813 555 1234 x99'→ '+18135551234'  (extension stripped)

    Returns None for:
      None, '', whitespace, letters-only, obviously invalid lengths.

    Non-US input (e.g. '+442071838750') is returned unchanged if it
    already looks like a valid E.164 string; otherwise None. This
    module does not attempt international guesswork.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Strip common extension notations before pulling digits.
    text = _EXT_RE.sub('', text).strip()

    # Preserve a leading '+' so we can distinguish already-international
    # numbers from ambiguous 10-digit inputs.
    has_plus = text.startswith('+')
    digits = ''.join(_DIGIT_RE.findall(text))

    if not digits:
        return None

    if has_plus:
        # Already-international: minimum ITU E.164 length is 7 digits after
        # the country code (some countries), max is 15. Reject clearly
        # bogus lengths but don't apply US-specific rules.
        if 7 <= len(digits) <= 15:
            return f'+{digits}'
        return None

    if default_country != 'US':
        # We only implement US normalization today. If another default is
        # requested, refuse rather than guess.
        return None

    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'

    # Anything else (7-digit local, 12+ digits without +, letters-only,
    # too-short) is unparseable under US-only rules.
    return None


def digits_only(value: Optional[str]) -> str:
    """Return just the digits from a phone string. Useful for comparing
    incoming Quo/LB/SF phones that may differ in formatting but not digits.
    Returns empty string for None / empty input.
    """
    if not value:
        return ''
    return ''.join(_DIGIT_RE.findall(str(value)))
