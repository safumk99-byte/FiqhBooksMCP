from app.main import _turath_id_from_description


def test_parses_turath_book_id_label():
    assert _turath_id_from_description("Turath Book ID: 9059") == 9059


def test_parses_turath_book_id_with_equals():
    assert _turath_id_from_description("Turath book_id=21721") == 21721


def test_parses_case_insensitively():
    assert _turath_id_from_description("turath BOOK id: 6140") == 6140


def test_does_not_infer_catalogue_only_records():
    assert _turath_id_from_description("Turath catalogue page_id=153; no verified full text") is None


def test_empty_description_has_no_source_id():
    assert _turath_id_from_description("") is None
