"""phase3_article_columns

Adds the Phase 3 columns to ``articles``:
* corrections/retraction workflow: ``correction_count``, ``correction_notes``,
  ``retraction_reason``, ``retracted_at``
* programmatic SEO clusters: ``noindex``, ``canonical_url``

Revision ID: a1b2c3d4e5f6
Revises: 156d4b138296
Create Date: 2026-06-20 03:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '156d4b138296'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'articles',
        sa.Column(
            'correction_count', sa.Integer(),
            server_default=sa.text('0'), nullable=False,
        ),
    )
    op.add_column('articles', sa.Column('correction_notes', sa.ARRAY(sa.Text()), nullable=True))
    op.add_column('articles', sa.Column('retraction_reason', sa.Text(), nullable=True))
    op.add_column('articles', sa.Column('retracted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'articles',
        sa.Column(
            'noindex', sa.Boolean(),
            server_default=sa.text('FALSE'), nullable=False,
        ),
    )
    op.add_column('articles', sa.Column('canonical_url', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('articles', 'canonical_url')
    op.drop_column('articles', 'noindex')
    op.drop_column('articles', 'retracted_at')
    op.drop_column('articles', 'retraction_reason')
    op.drop_column('articles', 'correction_notes')
    op.drop_column('articles', 'correction_count')
