"""Exercise the new revision against a disposable SQLite schema, never .env DB."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_upgrade_and_downgrade():
    path = Path(__file__).parents[1] / "alembic/versions/ae291ac74011_place_learning.py"
    spec = importlib.util.spec_from_file_location("place_learning_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        old = sa.MetaData()
        for name in ["users", "places", "somedays"]:
            sa.Table(name, old, sa.Column("id", sa.Uuid(), primary_key=True))
        old.create_all(connection)
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()
        inspector = sa.inspect(connection)
        assert "catalog_places" in inspector.get_table_names()
        assert "place_assessments" in inspector.get_table_names()
        assert "place_search_events" in inspector.get_table_names()
        assert "coordinates_refreshed_at" in {c["name"] for c in inspector.get_columns("places")}
        revision.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == {"users", "places", "somedays"}
    engine.dispose()
