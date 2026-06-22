"""Multi-vertical safety: add vertical column to articles + budget_day, update reserve_budget.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-21

- articles: add vertical TEXT NOT NULL DEFAULT 'aixcrypto'
- budget_day: add vertical TEXT NOT NULL DEFAULT 'aixcrypto' as part of composite PK  
- reserve_budget: update SQL function to accept p_vertical parameter
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add vertical column to articles (non-breaking: has DEFAULT)
    op.add_column(
        "articles",
        sa.Column(
            "vertical",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'aixcrypto'"),
        ),
    )

    # 2. Add vertical column to budget_day (must happen before PK change)
    op.add_column(
        "budget_day",
        sa.Column(
            "vertical",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'aixcrypto'"),
        ),
    )

    # 3. Drop old PK, add new composite PK (day, vertical)
    op.drop_constraint("budget_day_pkey", "budget_day", type_="primary")
    op.create_primary_key("budget_day_pkey", "budget_day", ["day", "vertical"])

    # 4. Update reserve_budget SQL function to accept + scope by vertical
    op.execute("DROP FUNCTION IF EXISTS reserve_budget(DATE, NUMERIC)")
    op.execute("""
        CREATE OR REPLACE FUNCTION reserve_budget(
            p_day DATE, p_vertical TEXT, p_amount NUMERIC
        )
        RETURNS BOOLEAN LANGUAGE plpgsql AS $$
        DECLARE ok BOOLEAN;
        BEGIN
          UPDATE budget_day SET reserved_usd = reserved_usd + p_amount
           WHERE day = p_day AND vertical = p_vertical
             AND reserved_usd + p_amount <= ceiling_usd
          RETURNING TRUE INTO ok;
          RETURN COALESCE(ok, FALSE);
        END $$;
    """)


def downgrade() -> None:
    # 1. Revert reserve_budget to vertical-free form
    op.execute("DROP FUNCTION IF EXISTS reserve_budget(DATE, TEXT, NUMERIC)")
    op.execute("""
        CREATE OR REPLACE FUNCTION reserve_budget(p_day DATE, p_amount NUMERIC)
        RETURNS BOOLEAN LANGUAGE plpgsql AS $$
        DECLARE ok BOOLEAN;
        BEGIN
          UPDATE budget_day SET reserved_usd = reserved_usd + p_amount
           WHERE day = p_day AND reserved_usd + p_amount <= ceiling_usd
          RETURNING TRUE INTO ok;
          RETURN COALESCE(ok, FALSE);
        END $$;
    """)

    # 2. Drop composite PK, restore single-column PK
    op.drop_constraint("budget_day_pkey", "budget_day", type_="primary")
    op.create_primary_key("budget_day_pkey", "budget_day", ["day"])

    # 3. Drop vertical column from budget_day
    op.drop_column("budget_day", "vertical")

    # 4. Drop vertical column from articles
    op.drop_column("articles", "vertical")
