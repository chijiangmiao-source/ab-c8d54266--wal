"""Fixtures for the WAL recovery tests.

``WalBuilder`` hand-crafts WAL files (including deliberately broken ones),
``continue_wal`` appends valid uncommitted frames onto an existing WAL, and
``make_real_db_and_wal`` produces a genuine SQLite main database plus WAL
through the sqlite3 module so the engine is tested against real bytes.
"""

from __future__ import annotations

import os
import sqlite3
import struct

WAL_MAGIC_LE = 0x377F0682
WAL_VERSION = 3007000
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24


def checksum(data: bytes, s1: int = 0, s2: int = 0) -> tuple[int, int]:
    """Same little-endian cumulative checksum the engine validates."""
    for i in range(0, len(data), 8):
        w0, w1 = struct.unpack_from("<II", data, i)
        s1 = (s1 + w0 + s2) & 0xFFFFFFFF
        s2 = (s2 + w1 + s1) & 0xFFFFFFFF
    return s1, s2


class WalBuilder:
    """Builds a WAL file frame by frame, keeping the checksum chain intact."""

    def __init__(
        self,
        page_size: int = 1024,
        salt1: int = 0xA5A5A5A5,
        salt2: int = 0x5A5A5A5A,
        checkpoint_seq: int = 0,
        magic: int = WAL_MAGIC_LE,
        version: int = WAL_VERSION,
    ):
        self.page_size = page_size
        self.salt1 = salt1
        self.salt2 = salt2
        header = bytearray(WAL_HEADER_SIZE)
        struct.pack_into(">IIII", header, 0, magic, version, page_size, checkpoint_seq)
        struct.pack_into(">II", header, 16, salt1, salt2)
        s1, s2 = checksum(bytes(header[:24]))
        struct.pack_into(">II", header, 24, s1, s2)
        self._parts = [bytes(header)]
        self._s1, self._s2 = s1, s2
        self.nframes = 0

    def add_frame(self, page: int, commit_size: int = 0, data: bytes | None = None):
        if data is None:
            data = bytes([self.nframes & 0xFF]) * self.page_size
        assert len(data) == self.page_size
        header = bytearray(FRAME_HEADER_SIZE)
        struct.pack_into(">IIII", header, 0, page, commit_size, self.salt1, self.salt2)
        s1, s2 = checksum(bytes(header[:8]), self._s1, self._s2)
        s1, s2 = checksum(data, s1, s2)
        struct.pack_into(">II", header, 16, s1, s2)
        self._s1, self._s2 = s1, s2
        self._parts.append(bytes(header) + data)
        self.nframes += 1
        return self

    def frame_offset(self, number: int) -> int:
        """Byte offset of 1-based frame ``number`` in the built WAL."""
        return WAL_HEADER_SIZE + (number - 1) * (FRAME_HEADER_SIZE + self.page_size)

    def bytes(self) -> bytes:
        return b"".join(self._parts)


def continue_wal(wal: bytes, frames: list[tuple[int, int, bytes]]) -> bytes:
    """Append frames to an existing WAL, extending its checksum chain.

    ``frames`` is a list of ``(page, commit_size, data)`` tuples.  Salts are
    inherited from the WAL header so the new frames are fully valid.
    """
    _, _, page_size, _, salt1, salt2, s1, s2 = struct.unpack(">8I", wal[:32])
    offset = WAL_HEADER_SIZE
    while offset + FRAME_HEADER_SIZE + page_size <= len(wal):
        s1, s2 = checksum(wal[offset : offset + 8], s1, s2)
        s1, s2 = checksum(
            wal[offset + FRAME_HEADER_SIZE : offset + FRAME_HEADER_SIZE + page_size],
            s1,
            s2,
        )
        offset += FRAME_HEADER_SIZE + page_size
    out = bytearray(wal)
    for page, commit_size, data in frames:
        assert len(data) == page_size
        header = bytearray(FRAME_HEADER_SIZE)
        struct.pack_into(">IIII", header, 0, page, commit_size, salt1, salt2)
        s1, s2 = checksum(bytes(header[:8]), s1, s2)
        s1, s2 = checksum(data, s1, s2)
        struct.pack_into(">II", header, 16, s1, s2)
        out += bytes(header) + data
    return bytes(out)


def make_real_db_and_wal(
    directory: str,
    page_size: int = 1024,
    txns: int = 3,
    rows_per_txn: int = 10,
    seed_rows: int = 5,
) -> tuple[bytes, bytes, dict]:
    """Create a real SQLite database and leave committed transactions in the WAL.

    The schema plus ``seed_rows`` rows are checkpointed into the main database
    file; ``txns`` further transactions are committed with autocheckpoint
    disabled so their frames stay in the WAL.  Returns ``(db_bytes,
    wal_bytes, meta)`` where meta carries ``rows``, ``frames`` and
    ``page_size``.
    """
    db_path = os.path.join(directory, "main.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(f"PRAGMA page_size={page_size}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("BEGIN")
    for j in range(seed_rows):
        conn.execute("INSERT INTO t(v) VALUES (?)", (f"seed-row{j}-" * 20,))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for i in range(txns):
        conn.execute("BEGIN")
        for j in range(rows_per_txn):
            conn.execute("INSERT INTO t(v) VALUES (?)", (f"txn{i}-row{j}-" * 20,))
        conn.execute("COMMIT")
    with open(db_path, "rb") as fh:
        db = fh.read()
    with open(db_path + "-wal", "rb") as fh:
        wal = fh.read()
    conn.close()
    nframes = (len(wal) - WAL_HEADER_SIZE) // (FRAME_HEADER_SIZE + page_size)
    meta = {
        "rows": seed_rows + txns * rows_per_txn,
        "frames": nframes,
        "page_size": page_size,
    }
    return db, wal, meta


def minimal_db(page_size: int = 1024, pages: int = 4) -> bytes:
    """A minimal but well-formed SQLite 3 database image (header only)."""
    image = bytearray(page_size * pages)
    image[:16] = b"SQLite format 3\x00"
    struct.pack_into(">H", image, 16, page_size)
    return bytes(image)
