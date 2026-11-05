"""Turning text into search terms. Shared by the keyword index (BM25) and the hashing embedder."""

from __future__ import annotations

import re

# Small English stop-word list: words so common they carry no meaning for search.
STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if in into is it
    its me my no not of on or our so than that the their them then there these they this to too us was we
    were what when where which who why will with you your after before again all also any because being
    both each few more most other some such only own same just should now""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Lower-case words and numbers, without stop words. 'No-show' stays one token: `no-show`.

    Very light normalisation on purpose: no stemming library. A trailing plural 's' is removed from
    longer words so 'appointments' matches 'appointment'.
    """
    tokens = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in STOPWORDS:
            continue
        if len(raw) > 4 and raw.endswith("s") and not raw.endswith("ss"):
            raw = raw[:-1]
        tokens.append(raw)
    return tokens
