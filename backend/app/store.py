"""Audit store — SQLite, stdlib only.

Design choices tied to the spec's non-functional requirements (§5):

* **Idempotent ingest** — the primary key is ``file_hash`` (sha256 of the raw
  bytes); re-uploading the same log is a no-op. ``(SerialNumber, file_hash)`` is
  additionally unique.
* **Latest run wins** — retests share a serial. The row with the newest ``mtime``
  (id as tiebreak) is the *active* run; older ones are marked ``superseded`` and
  excluded from dashboard counts, but kept for the audit trail.
* **Audit trail** — we persist the raw log path plus every parsed field, so any
  flagged serial traces back to its file (Ford/8D requirement).
* **Scale** — only raw parsed fields are stored; the physics/disposition is
  recomputed on read from live config, so a QE retune of the limits takes effect
  with no re-ingest. Reads stream row-by-row (``yield``), never loading all rows.

Swap SQLite for Postgres by reimplementing this module's small surface; nothing
else imports sqlite3.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

from .calc import RawFields, Unit, compute
from . import config

_RAW_COLS = [
    "Estatus_Tester", "Consumo_mA", "Offset_High_V", "V_20A_High_V",
    "Tiempo_ms_High", "Offset_Low_V", "V_20A_Low_V", "Tiempo_ms_Low",
    "parse_error",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS units (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash    TEXT NOT NULL UNIQUE,
    serial       TEXT NOT NULL,
    archivo      TEXT NOT NULL,
    categoria    TEXT NOT NULL,
    raw_path     TEXT,
    mtime        REAL NOT NULL,
    superseded   INTEGER NOT NULL DEFAULT 0,
    {", ".join(c + " TEXT" for c in _RAW_COLS)}
);
CREATE INDEX IF NOT EXISTS ix_units_serial ON units(serial);
CREATE INDEX IF NOT EXISTS ix_units_active ON units(superseded);
CREATE UNIQUE INDEX IF NOT EXISTS ux_serial_hash ON units(serial, file_hash);
"""


class Store:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------------------------------------------ ingest
    def add(self, raw: RawFields, file_hash: str, raw_path: Optional[str] = None,
            mtime: Optional[float] = None) -> str:
        """Insert one parsed run. Returns 'added' or 'skipped' (duplicate hash)."""
        mtime = mtime if mtime is not None else time.time()
        with self._conn() as c:
            exists = c.execute("SELECT 1 FROM units WHERE file_hash=?",
                               (file_hash,)).fetchone()
            if exists:
                return "skipped"
            cols = ["file_hash", "serial", "archivo", "categoria", "raw_path", "mtime"] + _RAW_COLS
            vals = [file_hash, raw.SerialNumber, raw.Archivo, raw.Categoria, raw_path, mtime]
            for col in _RAW_COLS:
                v = getattr(raw, col)
                vals.append(None if v is None else str(v))
            c.execute(
                f"INSERT INTO units ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            self._reconcile_superseded(c, raw.SerialNumber)
        return "added"

    @staticmethod
    def _reconcile_superseded(c: sqlite3.Connection, serial: str) -> None:
        """Mark all but the newest run for this serial as superseded."""
        rows = c.execute(
            "SELECT id FROM units WHERE serial=? ORDER BY mtime DESC, id DESC",
            (serial,),
        ).fetchall()
        if not rows:
            return
        active_id = rows[0]["id"]
        c.execute("UPDATE units SET superseded=1 WHERE serial=?", (serial,))
        c.execute("UPDATE units SET superseded=0 WHERE id=?", (active_id,))

    # ------------------------------------------------------------------- reads
    def _row_to_unit(self, row: sqlite3.Row) -> Unit:
        def num(v):
            return None if v in (None, "", "None") else float(v)
        raw = RawFields(
            Categoria=row["categoria"],
            SerialNumber=row["serial"],
            Archivo=row["archivo"],
            Estatus_Tester=row["Estatus_Tester"],
            Consumo_mA=num(row["Consumo_mA"]),
            Offset_High_V=num(row["Offset_High_V"]),
            V_20A_High_V=num(row["V_20A_High_V"]),
            Tiempo_ms_High=num(row["Tiempo_ms_High"]),
            Offset_Low_V=num(row["Offset_Low_V"]),
            V_20A_Low_V=num(row["V_20A_Low_V"]),
            Tiempo_ms_Low=num(row["Tiempo_ms_Low"]),
            parse_error=row["parse_error"] or None,
        )
        return compute(raw)

    def iter_units(self, active_only: bool = True) -> Iterator[Unit]:
        """Stream computed Units. Ordered by insertion for stable CSV output."""
        sql = "SELECT * FROM units"
        if active_only:
            sql += " WHERE superseded=0"
        sql += " ORDER BY id"
        with self._conn() as c:
            for row in c.execute(sql):
                yield self._row_to_unit(row)

    def count(self, active_only: bool = True) -> int:
        with self._conn() as c:
            sql = "SELECT COUNT(*) n FROM units" + (" WHERE superseded=0" if active_only else "")
            return c.execute(sql).fetchone()["n"]

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM units")
