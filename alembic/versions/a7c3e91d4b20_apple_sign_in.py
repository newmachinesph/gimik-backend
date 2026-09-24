"""sign in with apple: apple_sub, apple_refresh_token

Revision ID: a7c3e91d4b20
Revises: ae291ac74011
Create Date: 2026-09-25 02:00:00
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = 'a7c3e91d4b20'
down_revision = 'ae291ac74011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('apple_sub', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('apple_refresh_token', sa.String(length=1024), nullable=True))
    op.create_unique_constraint('uq_users_apple_sub', 'users', ['apple_sub'])


def downgrade() -> None:
    op.drop_constraint('uq_users_apple_sub', 'users', type_='unique')
    op.drop_column('users', 'apple_refresh_token')
    op.drop_column('users', 'apple_sub')
