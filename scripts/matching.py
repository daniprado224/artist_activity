import unicodedata


def normalize_for_matching(name: str) -> str:
    # lowercase, fold accents, treat "&" as "and" -- catches things like
    # "Beyoncé" vs "Beyonce" and "Bob Marley & The Wailers" vs "...and..."
    lowered = name.strip().lower().replace("&", " and ")
    folded = unicodedata.normalize("NFKD", lowered)
    without_accents = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(without_accents.split())
