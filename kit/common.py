"""Shared bits: settings from .env, a tiny HTTP helper, and the SQLite store.

.env only holds harmless settings. Keys that can change anything (Whop, GoDaddy,
full Resend) are typed in during setup and never saved."""

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "kit.db"


def load_env(path=ROOT / ".env"):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


ENV = load_env()


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status


def http(method, url, headers=None, json_body=None, form=None, timeout=30):
    """One HTTP call. Sends JSON or form data, returns parsed JSON (or {})."""
    data = None
    headers = dict(headers or {})
    # Resend sits behind Cloudflare, which blocks the default urllib User-Agent.
    headers.setdefault("User-Agent", "digital-product-kit/1.0")
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode(errors="replace"), url) from None
    return json.loads(raw) if raw.strip() else {}


# Whop versions its API by date. Pinning one keeps the replies (and the webhook
# payloads) in the shape this code was written against.
WHOP_API_VERSION = "2026-09-29"
WHOP_HOSTS = {
    "live": ("https://api.whop.com/api/v1", "https://whop.com"),
    "sandbox": ("https://sandbox-api.whop.com/api/v1", "https://sandbox.whop.com"),
}


def whop(key, mode, method, path, body=None, query=None):
    """One call to Whop's API. mode is "live" or "sandbox" (Whop's test copy)."""
    url = WHOP_HOSTS[mode][0] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return http(method, url, json_body=body, headers={
        "Authorization": f"Bearer {key}",
        "Api-Version-Date": WHOP_API_VERSION,
    })


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    slug          TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    domain        TEXT NOT NULL,
    seller_name   TEXT NOT NULL,
    price_id      TEXT NOT NULL,             -- the Whop variant (plan_...) buyers pay for
    files         TEXT NOT NULL,             -- JSON list of file names
    expiry_days   INTEGER NOT NULL DEFAULT 365,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    token          TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL,
    slug           TEXT NOT NULL,
    session_id     TEXT NOT NULL UNIQUE,      -- the Whop payment id (pay_...)
    downloads      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (           -- every Whop variant the kit made
    plan_id  TEXT PRIMARY KEY,
    slug     TEXT NOT NULL,
    mode     TEXT NOT NULL                   -- "sandbox" or "live"
);
"""

# Columns added after the first release. Existing rows get the value that matches
# how they were made: every older product was live, every older order was emailed.
MIGRATIONS = {
    "products": [("mode", "TEXT NOT NULL DEFAULT 'live'")],
    "orders": [("emailed", "INTEGER NOT NULL DEFAULT 1")],
}


def db():
    # Orders (buyer emails), webhook secrets and send-only email keys live here: owner-only.
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
    return conn


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
