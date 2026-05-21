from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

logger = logging.getLogger(__name__)

SCHEMAS = ['bronze_ibis', 'silver_ibis', 'gold_ibis', 'ibis', 'store_ibis', 'sms']


def create_db_engine(config) -> Engine:
    """Create a SQLAlchemy engine from the 'db' config block.
    Uses URL.create() to safely handle special characters in the password.
    """
    db = config.get('db')
    with open(db['password_secret_file']) as f:
        password = f.read().strip()
    url = URL.create(
        drivername='postgresql+psycopg2',
        username=db['user'],
        password=password,
        host=db['host'],
        port=db['port'],
        database=db['name'],
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": "-c statement_timeout=300000 -c lock_timeout=30000"
        },
    )


def init_schemas(engine: Engine) -> None:
    """Create all medallion schemas if they do not already exist."""
    with engine.connect() as conn:
        for schema in SCHEMAS:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
            logger.debug('Schema ready: %s', schema)
        conn.commit()
    logger.info('Initialised schemas: %s', SCHEMAS)


def run_migrations(engine: Engine) -> None:
    """Run all SQL migration files in sql/migrations/ in filename order (idempotent)."""
    migrations_dir = Path(__file__).parent.parent / 'sql' / 'migrations'
    sql_files = sorted(migrations_dir.glob('*.sql'))
    with engine.begin() as conn:
        for path in sql_files:
            conn.execute(text(path.read_text()))
            logger.debug('Applied migration: %s', path.name)
    if sql_files:
        logger.info('Ran %d migration(s).', len(sql_files))


def init_sms_tables(engine: Engine) -> None:
    """Create SMS tables from sql/sms/init_sms_schema.sql (idempotent — IF NOT EXISTS)."""
    sql_path = Path(__file__).parent.parent / 'sql' / 'sms' / 'init_sms_schema.sql'
    with engine.begin() as conn:
        conn.execute(text(sql_path.read_text()))
    logger.debug('SMS tables ready.')
