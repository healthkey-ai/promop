from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_database_configuration_is_url_driven():
    """The Django database name must come from DATABASE_URL, never a fixed name."""
    settings = (ROOT / "ctomop" / "settings.py").read_text()
    database_block = settings.split("# Database", 1)[1].split("# Password validation", 1)[0]

    assert "DATABASE_URL" in database_block
    assert "dj_database_url.config" in database_block
    assert "ctomop_dev" not in database_block
    assert "ctomop" not in database_block


def test_operational_docs_do_not_require_ctomop_database_name():
    """Environment-specific database names must not become deployment requirements."""
    docs = [ROOT / "docs" / "vocabularies.md", ROOT / "docs" / "wearable-omop-mapping.md"]
    for path in docs:
        assert "ctomop_dev" not in path.read_text()
