"""Arabic-aware query normalization and conservative multilingual expansion."""

import unicodedata

_ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ؤ": "و", "ئ": "ي",
})

# Search-key mappings only; these do not assert legal equivalence or issue rulings.
_QUERY_EXPANSIONS = {
    "ഖസർ": "قصر سفر",
    "ഖസ്റ്": "قصر سفر",
    "യാത്ര": "سفر",
    "സഫർ": "سفر",
    "ജംഅ്": "جمع",
    "ജം": "جمع",
    "നാല് ദിവസം": "أربعة أيام إقامة",
    "നാലു ദിവസം": "أربعة أيام إقامة",
    "ഇഖാമത്ത്": "إقامة",
    "വതൻ": "وطن",
    "qasr": "قصر سفر",
    "qasar": "قصر الصلاة السفر",
    "solat qasar": "قصر الصلاة السفر",
    "qasar solat": "قصر الصلاة السفر",
    "jamak solat musafir": "جمع الصلاة السفر",
    "solat jamak": "جمع الصلاة",
    "musafir": "مسافر سفر",
    "jam": "جمع",
    "travel": "سفر",
    "watan": "وطن",
    "residence": "إقامة",
    "four days": "أربعة أيام",
    "مسافة القصر": "مرحلتان ثمانية وأربعون ميلا",
    "شروط القصر": "السفر الطويل المباح نية القصر",
    "نية الإقامة": "إقامة أربعة أيام بلياليها",
    "الجمع في السفر": "جمع التقديم جمع التأخير",
}


def normalize_arabic_text(value: str) -> str:
    """Fold common Arabic letter variants and remove tashkeel/tatweel for matching."""
    value = unicodedata.normalize("NFKC", value or "")
    value = "".join(
        char for char in value
        if char != "ـ" and unicodedata.category(char) != "Mn"
    )
    return value.translate(_ARABIC_FOLD)


def expand_search_query(query: str) -> list[str]:
    """Return normalized original text plus explicit multilingual search alternatives."""
    original = normalize_arabic_text(query).strip()
    if not original:
        return []

    candidates = [original]
    lowered = original.casefold()
    for phrase, expansion in _QUERY_EXPANSIONS.items():
        if normalize_arabic_text(phrase).casefold() in lowered:
            candidates.append(normalize_arabic_text(expansion))

    return list(dict.fromkeys(item for item in candidates if item))


def build_fallback_search(tokens: list[str]) -> tuple[str, int]:
    """Build an OR-prefix tsquery and its minimum distinct-term coverage threshold."""
    unique_tokens = list(dict.fromkeys(token for token in tokens if token))
    if not unique_tokens:
        return "", 0
    tsquery = " | ".join(f"{token}:*" for token in unique_tokens)
    minimum_matches = 2 if len(unique_tokens) >= 3 else 1
    return tsquery, minimum_matches
