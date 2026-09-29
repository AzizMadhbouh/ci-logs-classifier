"""create_initial_schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-09-29

"""
from alembic import op
import sqlalchemy as sa

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'builds',
        sa.Column('build_id', sa.Integer, primary_key=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('error_count', sa.Integer, default=0),
        sa.Column('warning_count', sa.Integer, default=0),
        sa.Column('trend', sa.Text, default=''),
        sa.Column('result', sa.String(20)),
        sa.Column('category', sa.String(50)),
        sa.Column('llm_severity', sa.String(20)),
        sa.Column('severity_source', sa.String(20)),
        sa.Column('severity_reason', sa.Text),
    )
    op.create_index('ix_builds_timestamp', 'builds', ['timestamp'])
    op.create_index('ix_builds_category', 'builds', ['category'])

    op.create_table(
        'build_issues',
        sa.Column('issue_id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('build_id', sa.Integer, sa.ForeignKey('builds.build_id', ondelete='CASCADE'), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('severity', sa.String(20)),
        sa.Column('category', sa.String(50)),
        sa.Column('line', sa.Text),
        sa.Column('is_error', sa.Boolean, nullable=False),
    )
    op.create_index('ix_build_issues_build_id', 'build_issues', ['build_id'])
    op.create_index('ix_build_issues_severity', 'build_issues', ['severity'])

def downgrade():
    op.drop_index('ix_build_issues_severity', table_name='build_issues')
    op.drop_index('ix_build_issues_build_id', table_name='build_issues')
    op.drop_table('build_issues')
    op.drop_index('ix_builds_category', table_name='builds')
    op.drop_index('ix_builds_timestamp', table_name='builds')
    op.drop_table('builds')