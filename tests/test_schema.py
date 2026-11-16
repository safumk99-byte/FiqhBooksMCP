from app.db import SCHEMA_SQL
from app.search import expand_search_query, normalize_arabic_text


def test_schema_has_core_entities():
    for table in ("books", "editions", "pages", "passages", "citations", "research_notes"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL


def test_schema_adds_normalized_arabic_search_vector():
    assert "fiqh_normalize_arabic" in SCHEMA_SQL
    assert "search_vector_ar" in SCHEMA_SQL
    assert "passages_search_ar_idx" in SCHEMA_SQL


def test_arabic_normalization_removes_tashkeel_and_folds_alif():
    assert normalize_arabic_text("إِقَامَةٌ") == "اقامة"


def test_arabic_normalization_folds_alif_maqsura():
    assert normalize_arabic_text("يُصَلِّي") == "يصلي"


def test_malayalam_qasr_expands_to_arabic_search_terms():
    expanded = expand_search_query("ഖസർ യാത്ര")
    assert "قصر سفر" in expanded


def test_malayalam_jam_expands_to_arabic_search_term():
    assert "جمع" in expand_search_query("ജംഅ്")


def test_arabic_fiqh_phrase_gets_related_search_keys():
    expanded = expand_search_query("مسافة القصر")
    assert "مرحلتان ثمانية واربعون ميلا" in expanded
