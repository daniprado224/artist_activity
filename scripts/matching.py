"""Shared name-normalization for exact-match comparisons.

Used by both ingestion scripts (to prefer an exact match among API search
results over the API's own relevance ranking) and resolve_entities.py (to
check for an exact match before falling back to fuzzy matching). Kept as
one shared function so all three exact-match checks agree on what "exact"
means, rather than three independently-drifting implementations.
"""
import unicodedata


def normalize_for_matching(name: str) -> str:
    """Lowercase, fold accents to their base letter, and normalize "&" to "and".

    This is intentionally shallow -- it does not understand abbreviations,
    stage-name aliases (e.g. "Kanye West" vs "Ye"), or word order. It only
    catches cosmetic string differences: accented characters (e.g. "Beyoncé"
    vs "Beyonce") and "&" vs "and" (e.g. "Bob Marley & The Wailers" vs
    "Bob Marley and the Wailers"). See README known-limitations.
    """
    lowered = name.strip().lower().replace("&", " and ")
    folded = unicodedata.normalize("NFKD", lowered)
    without_accents = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(without_accents.split())
