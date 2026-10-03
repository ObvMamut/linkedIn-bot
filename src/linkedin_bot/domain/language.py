"""Tiny stopword-based language detector for posting text. Returns None unless confident."""

import re

STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "the and with you our will are have who that for looking person field work".split()
    ),
    "fr": frozenset(
        "nous vous avec des les une pour dans notre recherchons personne et sur est".split()
    ),
    "de": frozenset(
        "wir suchen eine mit und der die das im zu für sie ist bei auf oder erfahrung".split()
    ),
    "es": frozenset(
        "buscamos una con para los las del experiencia equipo herramientas el y en".split()
    ),
    "it": frozenset(
        "cerchiamo una con per che esperienza nel nelle settore competenze del gruppo".split()
    ),
    "pl": frozenset(
        "szukamy osoby z w oraz dla jest nasz doświadczeniem dziedzinie pracy zespole się".split()
    ),
}
MIN_HITS = 3


def detect_language(text: str) -> str | None:
    words = re.findall(r"[^\W\d_]+", text.lower())
    scores = sorted(
        ((sum(1 for w in words if w in stop), code) for code, stop in STOPWORDS.items()),
        reverse=True,
    )
    (best, code), (second, _) = scores[0], scores[1]
    return code if best >= MIN_HITS and best >= 1.5 * second else None
