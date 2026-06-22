"""add_distributions

Adds the ``distributions`` table backing the distribution pipeline: one row per
repackaged article payload per channel (X thread / Telegram post). Generation and
posting are decoupled (status: generated -> posted/failed).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'distributions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('article_id', sa.BigInteger(), nullable=True),
        sa.Column('channel', sa.Text(), nullable=False),
        sa.Column('variant', sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('rendered_text', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), server_default=sa.text("'generated'"), nullable=False),
        sa.Column('external_url', sa.Text(), nullable=True),
        sa.Column('impressions', sa.Integer(), nullable=True),
        sa.Column('link_clicks', sa.Integer(), nullable=True),
        sa.Column('in_tokens', sa.Integer(), nullable=True),
        sa.Column('out_tokens', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_distributions_article_id', 'distributions', ['article_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_distributions_article_id', table_name='distributions')
    op.drop_table('distributions')
