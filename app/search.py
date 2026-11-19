"""Arabic-aware query normalization and conservative multilingual expansion."""

import re
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
    "jamak taqdim": "جمع التقديم",
    "jamak takhir": "جمع التأخير",
    "jamak ta'khir": "جمع التأخير",
    "jamak taakhir": "جمع التأخير",
    "jamak": "جمع",
    "taqdim": "التقديم",
    "takhir": "التأخير",
    "ta'khir": "التأخير",
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


def normalize_search_result_value(value):
    """Convert database binary text values to safe Unicode before MCP JSON serialization."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {key: normalize_search_result_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_search_result_value(item) for item in value]
    return value


# High-frequency Arabic particles that add little value to broad fallback matching.
# Keep content-bearing forms such as "فيها" because they can be part of a useful phrase.
_ARABIC_SEARCH_STOPWORDS = {
    "و", "ف", "ثم", "او", "أو", "بل", "قد", "لا", "لم", "لن", "ما",
    "من", "في", "عن", "على", "الى", "إلى", "هو", "هي", "هم", "هن",
    "به", "بها", "له", "لها", "هذا", "هذه", "ذلك", "تلك", "كان",
    "كانت", "يكون", "تكون", "ان", "أن", "إن", "اذا", "إذا", "حيث",
    "مثل", "مثلا", "مثلاً", "نحو", "لنحو", "عند", "مع", "كل", "غير",
    "يجوز", "لايجوز", "ولا", "وهو", "وهي",
}


def _arabic_token_variants(token: str) -> list[str]:
    """Return conservative spelling variants for one Arabic search token."""
    variants = [token]
    base = token
    if len(base) > 4 and base[0] in {"و", "ف"}:
        base = base[1:]
        variants.append(base)
    if len(base) > 4 and base.startswith("ال"):
        variants.append(base[2:])
    return list(dict.fromkeys(item for item in variants if len(item) > 1))


def build_arabic_fallback_search(query: str) -> tuple[str, list[str], int]:
    """Build an OR query from meaningful Arabic term groups and return coverage terms.

    Each coverage term is a small OR-group of safe prefix variants. Coverage is
    counted once per original query token, so a token's spelling variants do not
    inflate relevance.
    """
    normalized = normalize_arabic_text(query)
    raw_tokens = re.findall(r"[\w]+", normalized, flags=re.UNICODE)
    groups = []
    seen = set()
    for token in raw_tokens:
        if token in _ARABIC_SEARCH_STOPWORDS or len(token) < 2:
            continue
        variants = _arabic_token_variants(token)
        if not variants:
            continue
        group = " | ".join(f"{variant}:*" for variant in variants)
        if group not in seen:
            seen.add(group)
            groups.append(group)

    if not groups:
        return "", [], 0

    tsquery = " | ".join(f"({group})" for group in groups)
    if len(groups) < 3:
        minimum_matches = 1
    else:
        minimum_matches = max(2, (len(groups) + 1) // 2)
    return tsquery, groups, minimum_matches
