from __future__ import annotations

from contextlib import contextmanager

import psycopg

from digital_twin.infrastructure.config import get_settings


# Get the database URL from the application settings
def get_database_url() -> str:
    return get_settings().database_url

# This context manager provides a connection to the database, optionally with a custom row factory 
# for query results. It ensures that the connection is properly closed after use, 
# even if an error occurs. Clients can use this context manager to execute database operations 
# without worrying about connection management.           
@contextmanager
def get_connection(row_factory=None):
    with psycopg.connect(get_database_url(), row_factory=row_factory) as conn:
        yield conn

