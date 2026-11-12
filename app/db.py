from collections.abc import Generator

import psycopg

from app.config import get_settings


def get_db() -> Generator[psycopg.Connection, None, None]:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        yield conn
