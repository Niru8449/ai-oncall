import logging
import os

from psycopg.conninfo import make_conninfo

QUEUE = "jobs"
SCHEMA_LOCK = 4242
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS orders ("
    " id BIGSERIAL PRIMARY KEY, sku TEXT NOT NULL, qty INTEGER NOT NULL,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS jobs_done ("
    " order_id BIGINT NOT NULL, done_at TIMESTAMPTZ NOT NULL DEFAULT now())",
)


def setup_logging(name):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(name)


def env_float(name, default):
    return float(os.environ.get(name) or default)


def env_int(name, default):
    return int(os.environ.get(name) or default)


def db_conninfo():
    url = os.environ["DATABASE_URL"]
    host = os.environ.get("DB_HOST")
    return make_conninfo(url, host=host) if host else make_conninfo(url)
