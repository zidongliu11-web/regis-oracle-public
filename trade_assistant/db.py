from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    cost_price REAL NOT NULL CHECK(cost_price > 0),
    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    close_price REAL,
    close_reason TEXT
);
CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    level TEXT NOT NULL,
    reason TEXT NOT NULL,
    data_time TEXT NOT NULL,
    research_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notification_log (
    fingerprint TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    channel TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    initial_cash REAL NOT NULL CHECK(initial_cash > 0),
    cash REAL NOT NULL CHECK(cash >= 0),
    status TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    signal_price REAL NOT NULL CHECK(signal_price > 0),
    suggested_quantity INTEGER NOT NULL CHECK(suggested_quantity >= 0),
    reason TEXT NOT NULL,
    data_time TEXT NOT NULL,
    data_source TEXT NOT NULL,
    research_gate_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_signal_id INTEGER NOT NULL REFERENCES paper_signal_events(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    entry_price REAL NOT NULL CHECK(entry_price > 0),
    entry_fees REAL NOT NULL CHECK(entry_fees >= 0),
    entry_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    exit_fees REAL,
    exit_time TEXT,
    exit_reason TEXT
);
CREATE TRIGGER IF NOT EXISTS paper_signal_events_no_update
BEFORE UPDATE ON paper_signal_events
BEGIN
    SELECT RAISE(ABORT, 'paper signal events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS paper_signal_events_no_delete
BEFORE DELETE ON paper_signal_events
BEGIN
    SELECT RAISE(ABORT, 'paper signal events are append-only');
END;
"""


def connect(database: str) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database: str) -> None:
    with connect(database) as connection:
        connection.executescript(SCHEMA)
        defaults = {
            "commission_rate": "0.0003",
            "minimum_commission": "5.0",
            "fee_profile_status": "estimated_default",
        }
        for key, value in defaults.items():
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        connection.execute(
            "INSERT OR IGNORE INTO paper_account(id, initial_cash, cash, status) "
            "VALUES (1, 10000, 10000, 'waiting_for_research_gate')"
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(positions)").fetchall()
        }
        if "close_price" not in columns:
            connection.execute("ALTER TABLE positions ADD COLUMN close_price REAL")
        if "close_reason" not in columns:
            connection.execute("ALTER TABLE positions ADD COLUMN close_reason TEXT")
