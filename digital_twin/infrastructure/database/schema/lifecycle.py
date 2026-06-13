from __future__ import annotations

import threading
import time

import psycopg

from digital_twin.infrastructure.database.connection import get_connection
from digital_twin.infrastructure.database.schema.constants import (
    DATABASE_INITIALIZATION_LOCK_ID,
    DEFAULT_POT_COUNT,
)
from digital_twin.infrastructure.database.schema.ddl import (
    _schema_is_current,
    create_schema,
)
from digital_twin.infrastructure.database.schema.seeding import (
    seed_pots,
    seed_reference_data,
    sync_generated_pot_flow_rates,
)

_initialize_lock = threading.Lock()
_database_initialized = False


def wait_for_database(max_attempts: int = 20, delay_seconds: float = 1.0) -> None:
    last_error = None
    for _ in range(max_attempts):
        try:
            with get_connection() as conn:
                conn.execute("SELECT 1")
                return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError("Database did not become available") from last_error


def initialize_database(pot_count: int = DEFAULT_POT_COUNT) -> None:
    global _database_initialized
    if _database_initialized:
        return

    with _initialize_lock:
        if _database_initialized:
            return

        wait_for_database()
        with get_connection() as conn:
            if _schema_is_current(conn):
                _database_initialized = True
                return
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (DATABASE_INITIALIZATION_LOCK_ID,))
            if not _schema_is_current(conn):
                create_schema(conn)
                seed_reference_data(conn)
                seed_pots(conn, target_count=pot_count)
                sync_generated_pot_flow_rates(conn, target_count=pot_count)
                conn.commit()
            _database_initialized = True
