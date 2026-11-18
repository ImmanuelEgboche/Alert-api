"""baseline schema

The schema as of v1.0.0 (create_all + scripts/migrations/001_hash_api_keys.sql).
Databases created before Alembic are stamped at this revision automatically
by src/migrate.py instead of running it.

Revision ID: 0001
Revises:
Create Date: 2026-11-18 20:30:43.571566
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('metrics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('metric_name', sa.String(length=100), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('labels', sa.Text(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_metric_timestamp', 'metrics', ['metric_name', 'timestamp'], unique=False)
    op.create_index(op.f('ix_metrics_id'), 'metrics', ['id'], unique=False)
    op.create_index(op.f('ix_metrics_metric_name'), 'metrics', ['metric_name'], unique=False)
    op.create_index(op.f('ix_metrics_timestamp'), 'metrics', ['timestamp'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('api_key_hash', sa.String(length=64), nullable=False),
    sa.Column('api_key_prefix', sa.String(length=12), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_api_key_hash'), 'users', ['api_key_hash'], unique=True)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('condition', sa.String(length=50), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=False),
    sa.Column('webhook_url', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('is_recurring', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_symbol_status', 'alerts', ['symbol', 'status'], unique=False)
    op.create_index('idx_user_status', 'alerts', ['user_id', 'status'], unique=False)
    op.create_index(op.f('ix_alerts_id'), 'alerts', ['id'], unique=False)
    op.create_index(op.f('ix_alerts_status'), 'alerts', ['status'], unique=False)
    op.create_index(op.f('ix_alerts_symbol'), 'alerts', ['symbol'], unique=False)
    op.create_index(op.f('ix_alerts_user_id'), 'alerts', ['user_id'], unique=False)
    op.create_table('webhook_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('http_status_code', sa.Integer(), nullable=True),
    sa.Column('response_time_ms', sa.Float(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_alert_timestamp', 'webhook_logs', ['alert_id', 'timestamp'], unique=False)
    op.create_index(op.f('ix_webhook_logs_alert_id'), 'webhook_logs', ['alert_id'], unique=False)
    op.create_index(op.f('ix_webhook_logs_id'), 'webhook_logs', ['id'], unique=False)
    op.create_index(op.f('ix_webhook_logs_status'), 'webhook_logs', ['status'], unique=False)
    op.create_index(op.f('ix_webhook_logs_timestamp'), 'webhook_logs', ['timestamp'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_webhook_logs_timestamp'), table_name='webhook_logs')
    op.drop_index(op.f('ix_webhook_logs_status'), table_name='webhook_logs')
    op.drop_index(op.f('ix_webhook_logs_id'), table_name='webhook_logs')
    op.drop_index(op.f('ix_webhook_logs_alert_id'), table_name='webhook_logs')
    op.drop_index('idx_alert_timestamp', table_name='webhook_logs')
    op.drop_table('webhook_logs')
    op.drop_index(op.f('ix_alerts_user_id'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_symbol'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_status'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_id'), table_name='alerts')
    op.drop_index('idx_user_status', table_name='alerts')
    op.drop_index('idx_symbol_status', table_name='alerts')
    op.drop_table('alerts')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_api_key_hash'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_metrics_timestamp'), table_name='metrics')
    op.drop_index(op.f('ix_metrics_metric_name'), table_name='metrics')
    op.drop_index(op.f('ix_metrics_id'), table_name='metrics')
    op.drop_index('idx_metric_timestamp', table_name='metrics')
    op.drop_table('metrics')
