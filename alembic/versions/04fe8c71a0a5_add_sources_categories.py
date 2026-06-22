"""add sources.categories

Adds the arXiv subject-category array to ``sources`` so ``select.py`` can apply
the Phase-0 filter (cs.AI ∧ cs.CR, OR a crypto keyword hit in the abstract).

The auto-generated draft also wanted to drop ``articles_embedding_hnsw`` because
that index is created via raw SQL in the initial migration and so is invisible to
the model metadata. We deliberately leave it in place.

Revision ID: 04fe8c71a0a5
Revises: 3e1fe4d03ee4
Create Date: 2026-06-19 23:12:41.896356

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '04fe8c71a0a5'
down_revision: Union[str, Sequence[str], None] = '3e1fe4d03ee4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sources', sa.Column('categories', sa.ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sources', 'categories')
