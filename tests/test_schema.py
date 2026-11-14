def test_schema_has_core_entities():
    from app.db import SCHEMA_SQL
    for table in ("books", "editions", "pages", "passages", "citations", "research_notes"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL
