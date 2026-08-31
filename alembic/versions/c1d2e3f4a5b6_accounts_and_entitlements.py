"""Token versioning, email verification, entitlement, and Google place caching.

Four things that arrived together because they touch the same two tables:

- `users.token_version` is what makes a stateless session endable. Signing out,
  changing a password and spending a reset link all move it forward, and every
  token minted before the bump stops decoding. It defaults to 0, which is what
  a token with no `tv` claim reports - so deploying this does not sign out
  everyone already carrying one.
- `users.email_verified` records a proven address. It gates nothing today.
- `users.tier` / `users.plus_until` carry the entitlement. `plus_until` is the
  truth; `tier` is the label beside it.
- `places.refreshed_at` bounds how long a cached Google display string may be
  served, and `lat`/`lon` become nullable because Autocomplete does not return
  coordinates and nothing in the product reads them.

Revision ID: c1d2e3f4a5b6
Revises: 98faea773eee
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c1d2e3f4a5b6'
down_revision = '98faea773eee'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so the columns are populated for rows that already exist;
    # the model default covers everything inserted afterwards.
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("tier", sa.String(length=16), nullable=False, server_default="free"),
    )
    op.add_column(
        "users",
        sa.Column("plus_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "places",
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Existing rows came from LocationIQ and cannot be refreshed against Google
    # by place ID. The column default stamps every row as fresh, which would
    # serve that old display text for the whole cache window - so the legacy
    # rows are explicitly backdated past it. They stay in the table, keeping
    # every plan that points at one intact, and simply stop being served: the
    # next search for each name costs one Google call and writes a Google row.
    op.execute(
        "UPDATE places SET refreshed_at = '1970-01-01 00:00:00+00' "
        "WHERE provider <> 'google'"
    )
    with op.batch_alter_table("places") as batch:
        batch.alter_column("lat", existing_type=sa.Float(), nullable=True)
        batch.alter_column("lon", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Rows written from Autocomplete have no coordinates, and the schema being
    # restored requires them. There is nothing to put back - the provider never
    # supplied them - so going backwards means dropping those rows. Plans that
    # referenced one lose the link and keep the typed place string, which is
    # what the screen displays anyway.
    op.execute("DELETE FROM places WHERE lat IS NULL OR lon IS NULL")
    with op.batch_alter_table("places") as batch:
        batch.alter_column("lon", existing_type=sa.Float(), nullable=False)
        batch.alter_column("lat", existing_type=sa.Float(), nullable=False)
    op.drop_column("places", "refreshed_at")
    op.drop_column("users", "plus_until")
    op.drop_column("users", "tier")
    op.drop_column("users", "token_version")
    op.drop_column("users", "email_verified")
