"""Mask platform-minted secrets before they are recorded anywhere.

The platform hands every agent a set of live credentials — a Matrix access token, a realmtools
bearer token, a LiteLLM virtual key — and then gives that same agent a way to run code inside its
own container. An agent that runs `env`, or prints its config, or is simply asked to describe its
setup, produces output containing those values. That output goes into the Chronicle, which is
append-only, served over the API, and included in exports.

The platform MINTS all of them, so it can substitute them on the way out. That is the whole idea
here: redaction at the point of recording, using values we already hold, rather than hoping an
agent never prints them.

Deliberately narrow. It masks known literal secrets, not a guess at what a secret looks like: a
pattern-matcher would both miss real credentials and mangle innocent text, and neither failure is
acceptable in an append-only log.
"""

from __future__ import annotations

from collections.abc import Iterable

MASK = "<redacted>"

# Anything shorter is either not a credential or so short that masking it would corrupt ordinary
# text — "id", "no", a single character. Real tokens here are 32+ characters.
_MIN_SECRET_LEN = 8


class Redactor:
    """Masks a known set of secrets in any text passing through it.

    Build it once per realm from the credentials the platform minted, then call it on anything
    heading for the Chronicle. Empty, short, and duplicate values are dropped, so a caller can pass
    whatever it has without filtering first.
    """

    __slots__ = ("_secrets",)

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        # Longest first: when one secret contains another (a bearer token inside a header string),
        # masking the longer one first stops the shorter match from leaving a fragment behind.
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= _MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )

    def __bool__(self) -> bool:
        return bool(self._secrets)

    def __call__(self, text: str) -> str:
        return self.apply(text)

    def apply(self, text: str) -> str:
        """`text` with every known secret replaced by `MASK`."""
        if not text or not self._secrets:
            return text
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, MASK)
        return text


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """One-shot convenience for a caller that has no reason to keep a `Redactor` around."""
    return Redactor(secrets).apply(text)
