"""per-user webhook secret

Webhooks were signed with the server's SECRET_KEY, which users can't have, so
they couldn't verify signatures. Each user now gets their own secret.

Existing users get a random secret here. They don't know it yet, so they must
call POST /api/v1/me/webhook-secret to rotate and receive one.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18 20:40:00
"""
import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("webhook_secret", sa.String(length=80), nullable=True))

    # One random secret per row, generated in Python (Postgres has no CSPRNG
    # without the pgcrypto extension)
    conn = op.get_bind()
    user_ids = conn.execute(sa.text("SELECT id FROM users WHERE webhook_secret IS NULL")).scalars()
    for user_id in user_ids.all():
        conn.execute(
            sa.text("UPDATE users SET webhook_secret = :secret WHERE id = :id"),
            {"secret": "whsec_" + secrets.token_hex(32), "id": user_id},
        )

    op.alter_column("users", "webhook_secret", nullable=False)


def downgrade() -> None:
    op.drop_column("users", "webhook_secret")
