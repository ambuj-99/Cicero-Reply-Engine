"""
redact.py -- STAGE 5b. The only text that ever reaches a model provider
passes through here first.

THE CENTRAL TRADE-OFF, stated plainly:

We do NOT redact names and company names. Pseudonymising them would destroy
the tone quality that is the entire point of this system -- "Dear PERSON_1,
regarding COMPANY_2" cannot be made to sound like a thoughtful colleague, and
the model needs the real name to pick the right register for a founder versus
a broker.

We DO redact everything that is both sensitive and useless for tone: phone
numbers, bank details, GSTIN and PAN numbers, physical addresses, and exact
monetary figures. None of these improve a reply. All of them are painful in a
breach.

Redaction is REVERSIBLE within one request. Placeholders go out, the model
writes prose containing placeholders, and rehydrate() puts the real values
back locally. The provider never sees the original string.
"""
import re
from typing import Dict, Tuple

# Ordered: longest and most specific patterns first, so a GSTIN is not
# partially eaten by the generic number rule.
PATTERNS = [
    # Indian tax and identity numbers
    ("GSTIN",   re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}\b")),
    ("PAN",     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("CIN",     re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")),

    # Financial account details
    ("IFSC",    re.compile(r"\b[A-Z]{4}0[A-Z\d]{6}\b")),
    ("ACCOUNT", re.compile(r"\b(?:a/c|acc(?:ount)?\.?\s*(?:no\.?|number)?)\s*:?\s*\d{9,18}\b", re.I)),
    ("CARD",    re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")),

    # Contact details
    # Indian mobiles are written a dozen ways: +91 98765 43210, 098765-43210,
    # 9876543210. Allow internal spaces and hyphens, then require 10 digits total.
    ("PHONE",   re.compile(r"(?:\+?91[\s\-.]?)?(?:0)?[6-9](?:[\s\-.]?\d){9}\b")),
    ("PHONE",   re.compile(r"\+\d{1,3}(?:[\s\-.]?\d){6,12}\b")),
    ("URLPATH", re.compile(r"https?://[^\s<>\"]{4,}")),

    # Money. Exact figures are the single most sensitive item in an M&A
    # thread and the model has zero legitimate use for them.
    ("MONEY",   re.compile(r"(?:[₹$£€]|Rs\.?|INR|USD)\s?[\d,]+(?:\.\d+)?(?:\s?(?:cr|crore|lakh|lakhs|k|m|mn|million|bn|billion))?", re.I)),
    ("MONEY",   re.compile(r"\b\d+(?:\.\d+)?\s?(?:cr|crore|lakh|lakhs)\b", re.I)),

    # Postal address lines -- crude but catches the common signature block form
    ("ADDRESS", re.compile(r"\b\d{1,4}[/-]?[A-Z]?,?\s+[\w\s\.]{3,40},\s*[\w\s]{3,25}\s*[-–]?\s*\d{6}\b")),
    ("PINCODE", re.compile(r"\b\d{6}\b")),
]


class Redactor:
    """
    One instance per message. Holds the mapping for that message only,
    then goes out of scope. Nothing is persisted.
    """

    def __init__(self):
        self.map: Dict[str, str] = {}     # placeholder -> original
        self._counts: Dict[str, int] = {}

    def _token(self, kind: str) -> str:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return f"[{kind}_{self._counts[kind]}]"

    def redact(self, text: str) -> str:
        """Replace sensitive spans with typed placeholders."""
        if not text:
            return text
        out = text
        for kind, pat in PATTERNS:
            def sub(m):
                original = m.group(0)
                # reuse the same token if we have already seen this exact value
                for tok, val in self.map.items():
                    if val == original:
                        return tok
                tok = self._token(kind)
                self.map[tok] = original
                return tok
            out = pat.sub(sub, out)
        return out

    def rehydrate(self, text: str) -> str:
        """Put the real values back. Runs locally, after the model returns."""
        if not text:
            return text
        out = text
        for tok, val in self.map.items():
            out = out.replace(tok, val)
        return out

    def leaked(self, text: str) -> bool:
        """
        Belt and braces: did any placeholder survive into the final draft?
        A reply containing '[PHONE_1]' is worse than no reply at all, and
        the validator treats this as a hard failure.
        """
        return bool(re.search(r"\[(?:GSTIN|PAN|AADHAAR|CIN|IFSC|ACCOUNT|CARD|"
                              r"PHONE|URLPATH|MONEY|ADDRESS|PINCODE)_\d+\]", text or ""))

    def summary(self) -> Dict[str, int]:
        """What was redacted, by type. Logged; the values never are."""
        out: Dict[str, int] = {}
        for tok in self.map:
            kind = tok.strip("[]").rsplit("_", 1)[0]
            out[kind] = out.get(kind, 0) + 1
        return out
