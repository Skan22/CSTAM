"""Local users. Passwords are argon2id hashes; nothing else about them is stored."""

from dataclasses import dataclass

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ROLES = ("viewer", "operator", "admin")  # each role includes those before it
TEAM = "team"  # not on the ladder: one team's own account, which no staff route admits
GATEWAY = "gateway"  # not on the ladder: a gateway agent, bound to its gateway, three routes only
ALL_ROLES = (*ROLES, TEAM, GATEWAY)


@dataclass(frozen=True)
class Identity:
    role: str
    team_id: str | None
    gateway: str | None = None

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
# Verified against when the account doesn't exist, so a login takes the same time either way.
_DUMMY = _hasher.hash("ipo-dummy-password")


def create_user(conn: psycopg.Connection, email: str, password: str, role: str,
                gateway: str | None = None) -> None:
    """A staff account, or (role `gateway`) a gateway agent's, which names its gateway."""
    if role not in (*ROLES, GATEWAY):
        raise ValueError(f"unknown role {role!r}")
    if (role == GATEWAY) != (gateway is not None):
        raise ValueError("a gateway account names its gateway, and only a gateway account does")
    conn.execute(
        "INSERT INTO users (email, role, password_hash, gateway) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role,"
        " password_hash = EXCLUDED.password_hash, gateway = EXCLUDED.gateway"
        " WHERE users.team_id IS NULL",
        (email, role, _hasher.hash(password), gateway))


class EmailTaken(Exception):
    pass


def create_team_user(conn: psycopg.Connection, team_id: str, email: str, password: str) -> None:
    """Never replaces an existing account, so it cannot turn a staff login into a team one."""
    cur = conn.execute(
        "INSERT INTO users (email, role, password_hash, team_id) VALUES (%s, 'team', %s, %s)"
        " ON CONFLICT (email) DO NOTHING", (email, _hasher.hash(password), team_id))
    if cur.rowcount == 0:
        raise EmailTaken(email)


def authenticate(conn: psycopg.Connection, email: str, password: str) -> Identity | None:
    """Who the user is if the password is right, else None."""
    row = conn.execute("SELECT role, password_hash, team_id::text, gateway FROM users"
                       " WHERE email = %s",
                       (email,)).fetchone()
    stored = row[1] if row else _DUMMY
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return None
    if row is None:
        return None
    if _hasher.check_needs_rehash(stored):
        conn.execute("UPDATE users SET password_hash = %s WHERE email = %s",
                     (_hasher.hash(password), email))
    return Identity(str(row[0]), row[2], row[3])
