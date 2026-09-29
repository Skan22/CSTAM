"""Test database: an ephemeral Postgres cluster, migrated once and cloned per test.

Set IPO_TEST_DATABASE_URL (a superuser DSN for the `postgres` database) to use an existing
server, as CI does; otherwise a private cluster is started from the local Postgres binaries.
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ipo.db.migrate import upgrade

_PG_BIN_CANDIDATES = ["/usr/bin", "/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/17/bin"]


def _pg_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in _PG_BIN_CANDIDATES:
        if (Path(d) / name).exists():
            return str(Path(d) / name)
    raise RuntimeError(f"{name} not found; set IPO_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def server_dsn() -> Iterator[str]:
    external = os.environ.get("IPO_TEST_DATABASE_URL")
    if external:
        yield external
        return
    root = Path(tempfile.mkdtemp(prefix="ipo-pg-"))
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    subprocess.run(
        [_pg_bin("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        check=True, capture_output=True,
    )
    opts = f"-c listen_addresses='' -c unix_socket_directories={sock} -c fsync=off " \
           f"-c synchronous_commit=off -c full_page_writes=off -c max_connections=200"
    subprocess.run(
        [_pg_bin("pg_ctl"), "-D", str(data), "-o", opts, "-w", "-l", str(root / "log"), "start"],
        check=True, capture_output=True,
    )
    try:
        yield f"postgresql://postgres@/postgres?host={sock}"
    finally:
        subprocess.run([_pg_bin("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def _with_db(dsn: str, dbname: str) -> str:
    base, _, query = dsn.partition("?")
    head, _, _ = base.rpartition("/")
    return f"{head}/{dbname}" + (f"?{query}" if query else "")


@pytest.fixture(scope="session")
def template_db(server_dsn: str) -> str:
    name = "ipo_template"
    with psycopg.connect(server_dsn, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    upgrade(_with_db(server_dsn, name))
    return name


@pytest.fixture
def dsn(server_dsn: str, template_db: str) -> Iterator[str]:
    name = f"ipo_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server_dsn, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    try:
        yield _with_db(server_dsn, name)
    finally:
        with psycopg.connect(server_dsn, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c
