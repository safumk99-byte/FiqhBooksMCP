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
        normalized_phrase = normalize_arabic_text(phrase).casefold()
        # Match complete words/phrases only; e.g. "jam" must not match
        # inside "jamak", and short aliases must not trigger accidentally.
        if re.search(
            rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)",
            lowered,
            flags=re.UNICODE,
        ):
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
    "و", "ف", "ثم", "او", "أو", "بل", "قد",
    "من", "في", "عن", "على", "الى", "إلى", "هو", "هي", "هم", "هن",
    "به", "بها", "له", "لها", "هذا", "هذه", "ذلك", "تلك", "كان",
    "كانت", "يكون", "تكون", "ان", "أن", "إن", "اذا", "إذا", "حيث",
    "مثل", "مثلا", "مثلاً", "نحو", "لنحو", "عند", "مع", "كل",
    "وهو", "وهي",
}

# Legal polarity and ruling terms must not be discarded as ordinary stopwords.
# A query containing a negation is constrained to passages containing that
# polarity token, so "لا يجوز" cannot silently become a search for "يجوز".
_ARABIC_POLARITY_TOKENS = {
    "لا", "لم", "لن", "ليس", "ليست", "ليسوا", "غير", "عدم",
    "ولا", "فلا", "ولم", "ولن", "فلم", "فلن", "لايجوز",
}


def _arabic_token_variants(token: str) -> list[str]:
    """Return conservative spelling variants for one Arabic search token."""
    variants = [token]
    base = token
    if token in {"ولا", "فلا", "ولم", "ولن", "فلم", "فلن"}:
        base = token[1:]
        variants.append(base)
    elif token.startswith("لا") and len(token) > 4:
        variants.append(token[2:])
    if len(base) > 4 and base[0] in {"و", "ف"}:
        base = base[1:]
        variants.append(base)
    if len(base) > 4 and base.startswith("ال"):
        variants.append(base[2:])

    # Resolve common attached prepositions only when they precede the
    # definite article, avoiding blind removal from ordinary root letters.
    for form in list(variants):
        if len(form) > 5 and form.startswith(("بال", "كال")):
            variants.append(form[1:])
        elif len(form) > 4 and form.startswith("لل"):
            variants.append(form[2:])
    for form in list(variants):
        if len(form) > 4 and form.startswith("ال"):
            variants.append(form[2:])

    # Conservative Arabic sound-plural normalization. Keep the original and
    # article-stripped forms, then add stems for common masculine/feminine
    # plural endings. Each token still counts as one coverage group.
    for form in list(variants):
        for suffix in ("ون", "ين", "ات"):
            if form.endswith(suffix) and len(form) > len(suffix) + 3:
                stem = form[:-len(suffix)]
                variants.append(stem)
                if len(stem) > 4 and stem.startswith("ال"):
                    variants.append(stem[2:])

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
    polarity_groups = set()
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
            if token in _ARABIC_POLARITY_TOKENS:
                polarity_groups.add(group)

    if not groups:
        return "", [], 0

    # Preserve ruling polarity: negation groups are mandatory, while the
    # remaining content terms form the normal broad-recall OR group.
    optional_groups = [group for group in groups if group not in polarity_groups]
    query_groups = [f"({group})" for group in sorted(polarity_groups)]
    if optional_groups:
        query_groups.append(f"({' | '.join(optional_groups)})")
    tsquery = " & ".join(query_groups)

    if len(groups) < 3:
        minimum_matches = 1
    else:
        minimum_matches = max(2, (len(groups) + 1) // 2)
    if polarity_groups and len(groups) >= 2:
        minimum_matches = max(2, minimum_matches)
    return tsquery, groups, minimum_matches
