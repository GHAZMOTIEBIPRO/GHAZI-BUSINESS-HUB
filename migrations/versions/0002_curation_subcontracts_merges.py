"""curation: subcontract opportunities (D), CR-only merges, grouped change history

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('subcontract_opportunities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('trade', sa.String(length=60), nullable=False),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('region', sa.String(length=100), nullable=True),
    sa.Column('main_contractor_name', sa.String(length=300), nullable=False),
    sa.Column('main_contractor_company_id', sa.Integer(), nullable=True),
    sa.Column('related_tender_id', sa.Integer(), nullable=True),
    sa.Column('evidence_kind', sa.String(length=30), nullable=False),
    sa.Column('evidence_url', sa.String(length=500), nullable=False),
    sa.Column('evidence_note', sa.Text(), nullable=False),
    sa.Column('published_date', sa.Date(), nullable=True),
    sa.Column('response_deadline', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('review_status', sa.String(length=30), nullable=False),
    sa.Column('visibility', sa.String(length=20), nullable=False),
    sa.Column('publish_allowed', sa.Boolean(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=False),
    sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('review_note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['main_contractor_company_id'], ['companies.id'], name=op.f('fk_subcontract_opportunities_main_contractor_company_id_companies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['related_tender_id'], ['procurement_tenders.id'],
                            name=op.f('fk_subcontract_opportunities_related_tender_id_procurement_tenders'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subcontract_opportunities'))
    )
    op.create_index(op.f('ix_subcontract_opportunities_main_contractor_company_id'), 'subcontract_opportunities', ['main_contractor_company_id'], unique=False)
    op.create_index('ix_subcontract_review', 'subcontract_opportunities', ['review_status', 'status'], unique=False)
    # batch mode: works on PostgreSQL and on SQLite (which cannot ALTER a table to add a foreign key)
    with op.batch_alter_table('change_history') as batch:
        batch.add_column(sa.Column('event_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_change_history_event_id'), ['event_id'], unique=False)
        batch.create_foreign_key(batch.f('fk_change_history_event_id_verification_events'), 'verification_events',
                                 ['event_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('companies') as batch:
        batch.add_column(sa.Column('merged_into_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_companies_merged_into_id'), ['merged_into_id'], unique=False)
        batch.create_foreign_key(batch.f('fk_companies_merged_into_id_companies'), 'companies',
                                 ['merged_into_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('companies') as batch:
        batch.drop_constraint(batch.f('fk_companies_merged_into_id_companies'), type_='foreignkey')
        batch.drop_index(batch.f('ix_companies_merged_into_id'))
        batch.drop_column('merged_into_id')
    with op.batch_alter_table('change_history') as batch:
        batch.drop_constraint(batch.f('fk_change_history_event_id_verification_events'), type_='foreignkey')
        batch.drop_index(batch.f('ix_change_history_event_id'))
        batch.drop_column('event_id')
    op.drop_index('ix_subcontract_review', table_name='subcontract_opportunities')
    op.drop_index(op.f('ix_subcontract_opportunities_main_contractor_company_id'), table_name='subcontract_opportunities')
    op.drop_table('subcontract_opportunities')
