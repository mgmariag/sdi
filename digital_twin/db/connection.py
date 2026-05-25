from __future__ import annotations

from contextlib import contextmanager

import psycopg

from digital_twin.core.config import get_settings


def get_database_url() -> str:
    return get_settings().database_url


@contextmanager
def get_connection(row_factory=None):
    with psycopg.connect(get_database_url(), row_factory=row_factory) as conn:
        yield conn

