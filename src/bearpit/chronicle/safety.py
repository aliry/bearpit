"""What an agent may write into the chronicle without ending a realm.

Two characters are hazards, for different reasons, and neither is caught by anything today:

  * an **unpaired surrogate** stores perfectly well in a Postgres `jsonb` column and round-trips
    identically — and then cannot be encoded into any HTTP response, because Starlette renders
    with `ensure_ascii=False`. One of them anywhere in a realm's payloads and that realm's
    endpoints answer 500 from then on. The chronicle is append-only: there is no un-storing it.
  * a **NUL** is refused outright by a Postgres text column (`CharacterNotInRepertoire`), so it
    breaks the moment anything copies the value into a message body.

The API's own encoder now fails safe (`SafeJSONResponse`), so a value that slips past this guard
through some other path can no longer take a realm's console with it. This function is the other
half: it lets the ingress that *knows which agent is writing* answer that agent plainly, instead
of accepting something it can never take back.

Nothing here is about taste. Every rejection is a failure that is permanent once it lands.
"""

from __future__ import annotations

import re
from typing import Any

# Mirrors `toolcall.MAX_ARGS_CHARS`: the same kind of argument, from the same kind of caller.
MAX_VALUE_CHARS = 8000
# Deep enough for any declaration a scenario has shipped; shallow enough that walking it is free.
MAX_VALUE_DEPTH = 32

# Built at runtime: a source file must not itself contain a surrogate escape, or the
# module cannot be encoded by the tools that read it (pytest's rewriter found this).
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")


def unstorable(value: Any, *, _depth: int = 0) -> str | None:
    """Why `value` must not enter the chronicle, or None if it may.

    The message is written to be read by an agent: it says what is wrong, not what a column is
    called.
    """
    if _depth > MAX_VALUE_DEPTH:
        return f"is nested deeper than {MAX_VALUE_DEPTH} levels"
    if isinstance(value, str):
        if "\x00" in value:
            return "contains a NUL character, which cannot be stored"
        if _SURROGATE.search(value):
            return "contains an unpaired surrogate character, which cannot be sent back to you"
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if (why := unstorable(k, _depth=_depth + 1)) is not None:
                return f"has a key that {why}"
            if (why := unstorable(v, _depth=_depth + 1)) is not None:
                return why
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            if (why := unstorable(v, _depth=_depth + 1)) is not None:
                return why
    return None


def oversized(serialised_len: int) -> str | None:
    """Why a value is too large to accept, or None. Kept beside `unstorable` so an ingress has
    one place to look for every reason a write is refused."""
    if serialised_len > MAX_VALUE_CHARS:
        return f"is {serialised_len} characters; the limit is {MAX_VALUE_CHARS}"
    return None
