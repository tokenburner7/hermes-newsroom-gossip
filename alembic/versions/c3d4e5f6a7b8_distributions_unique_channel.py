"""distributions unique (article_id, channel) for active rows

Backs the idempotency guard in ``newsroom.distribute.repackage`` with a real DB
constraint so two concurrent distributors (the Temporal ``distribute_activity`` and
the shell-cron ``newsroom distribute``) cannot both insert a payload for the same
``(article, channel)``. Partial so re-tries after a ``failed``/``partial`` row are
still allowed.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Pre-cleanup: deduplicate existing (article_id, channel) rows before the
    # unique index is created. Keeps the newest row per group (highest id).
    op.execute(sa.text("""
        DELETE FROM distributions d
        USING (
            SELECT article_id, channel, MAX(id) as keep_id
            FROM distributions
            WHERE status IN ('generated', 'posted')
            GROUP BY article_id, channel
            HAVING COUNT(*) > 1
        ) dupes
        WHERE d.article_id = dupes.article_id
          AND d.channel = dupes.channel
          AND d.id != dupes.keep_id
          AND d.status IN ('generated', 'posted')
    """))
    op.create_index(
        'uq_distributions_article_channel_active',
        'distributions',
        ['article_id', 'channel'],
        unique=True,
        postgresql_where=sa.text("status IN ('generated', 'posted')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_distributions_article_channel_active', table_name='distributions'
    )
