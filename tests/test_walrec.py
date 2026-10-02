"""Engine tests: valid multi-transaction WAL, corrupted last frame,
no-commit WAL, plus the remaining validation and recovery semantics."""

from __future__ import annotations

import os
import sqlite3
import struct
import tempfile
import unittest

from app.walrec import MAX_WAL_SIZE, RecoverError, recover
from tests.walfixture import (
    WalBuilder,
    continue_wal,
    make_real_db_and_wal,
    minimal_db,
)


def expect_error(test, code, db, wal, offset="any"):
    with test.assertRaises(RecoverError) as ctx:
        recover(db, wal)
    test.assertEqual(ctx.exception.code, code)
    if offset != "any":
        test.assertEqual(ctx.exception.offset, offset)
    return ctx.exception


class ValidMultiTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_real_multi_transaction_wal(self):
        db, wal, meta = make_real_db_and_wal(self.tmp.name, page_size=1024, txns=3)
        self.assertGreaterEqual(meta["frames"], 3)

        # Two fully valid but uncommitted frames tailing the WAL must not
        # leak into the recovered image.
        garbage1 = b"\xee" * 1024
        garbage2 = b"\xdd" * 1024
        wal = continue_wal(wal, [(1, 0, garbage1), (2, 0, garbage2)])

        result = recover(db, wal, stable_page_order=True)

        # Recovery prefix ends at the last commit frame of the real WAL.
        self.assertEqual(result["commit_frame"], meta["frames"])
        self.assertEqual(
            len(result["image"]), result["commit_size_pages"] * meta["page_size"]
        )
        self.assertEqual(result["recovered_pages"], len(result["pages"]))
        pages = [p["page"] for p in result["pages"]]
        self.assertEqual(pages, sorted(pages))  # stable page-number order
        for entry in result["pages"]:
            self.assertLessEqual(entry["source_frame"], result["commit_frame"])
        self.assertNotEqual(result["image"][:1024], garbage1)
        self.assertNotEqual(result["image"][1024:2048], garbage2)

        # The recovered image must be a working database holding exactly the
        # committed rows.
        path = os.path.join(self.tmp.name, "recovered.db")
        with open(path, "wb") as fh:
            fh.write(result["image"])
        conn = sqlite3.connect(path)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM t").fetchone()[0], meta["rows"]
        )
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_matches_sqlite_checkpoint_byte_for_byte(self):
        db, wal, meta = make_real_db_and_wal(self.tmp.name, page_size=512, txns=2)
        result = recover(db, wal)

        # Let SQLite itself replay the same WAL on a private copy.
        other = os.path.join(self.tmp.name, "replay")
        os.mkdir(other)
        with open(os.path.join(other, "r.db"), "wb") as fh:
            fh.write(db)
        with open(os.path.join(other, "r.db-wal"), "wb") as fh:
            fh.write(wal)
        conn = sqlite3.connect(os.path.join(other, "r.db"), isolation_level=None)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        with open(os.path.join(other, "r.db"), "rb") as fh:
            expected = fh.read()

        self.assertEqual(result["image"], expected)

    def test_last_occurrence_wins(self):
        page_a = b"A" * 1024
        page_b = b"B" * 1024
        wal = (
            WalBuilder(1024)
            .add_frame(1, 0, page_a)
            .add_frame(1, 2, page_b)  # commit: page 1 rewritten
            .bytes()
        )
        result = recover(minimal_db(1024), wal, stable_page_order=True)
        self.assertEqual(result["commit_frame"], 2)
        self.assertEqual(result["recovered_pages"], 1)
        self.assertEqual(result["pages"], [{"page": 1, "source_frame": 2}])
        self.assertEqual(result["image"][:1024], page_b)

    def test_uncommitted_tail_excluded(self):
        committed = b"C" * 1024
        dirty = b"D" * 1024
        wal = (
            WalBuilder(1024)
            .add_frame(1, 1, committed)  # commit frame
            .add_frame(1, 0, dirty)  # valid but never committed
            .bytes()
        )
        result = recover(minimal_db(1024), wal)
        self.assertEqual(result["commit_frame"], 1)
        self.assertEqual(result["image"][:1024], committed)

    def test_database_shrunk_by_last_commit(self):
        builder = WalBuilder(1024)
        for page in (1, 2, 3):
            builder.add_frame(page, 0)
        builder.add_frame(4, 4)  # commit: 4 pages
        builder.add_frame(1, 2)  # commit: shrink to 2 pages
        result = recover(minimal_db(1024, pages=4), builder.bytes(),
                         stable_page_order=True)
        self.assertEqual(result["commit_frame"], 5)
        self.assertEqual(result["commit_size_pages"], 2)
        self.assertEqual(len(result["image"]), 2048)
        self.assertEqual(result["recovered_pages"], 2)
        self.assertEqual([p["page"] for p in result["pages"]], [1, 2])

    def test_stable_page_order_option(self):
        wal = (
            WalBuilder(1024)
            .add_frame(3, 0)
            .add_frame(1, 0)
            .add_frame(2, 3)  # commit
            .bytes()
        )
        db = minimal_db(1024)
        stable = recover(db, wal, stable_page_order=True)
        self.assertEqual([p["page"] for p in stable["pages"]], [1, 2, 3])
        unstable = recover(db, wal, stable_page_order=False)
        self.assertEqual([p["page"] for p in unstable["pages"]], [3, 1, 2])
        self.assertEqual(stable["image"], unstable["image"])

    def test_empty_main_database(self):
        page1 = bytearray(1024)
        page1[:16] = b"SQLite format 3\x00"
        struct.pack_into(">H", page1, 16, 1024)
        wal = (
            WalBuilder(1024)
            .add_frame(1, 0, bytes(page1))
            .add_frame(2, 2)
            .bytes()
        )
        result = recover(b"", wal)
        self.assertEqual(result["commit_frame"], 2)
        self.assertEqual(result["image"][:16], b"SQLite format 3\x00")


class CorruptedTailTest(unittest.TestCase):
    def test_corrupted_last_frame_reports_offset(self):
        builder = WalBuilder(1024)
        builder.add_frame(1, 2).add_frame(2, 2)  # frame 2 commits
        wal = bytearray(builder.bytes())
        wal[-1] ^= 0xFF  # damage page data of the last frame
        expect_error(
            self, "checksum_mismatch", minimal_db(1024), bytes(wal),
            offset=builder.frame_offset(2),
        )

    def test_truncated_last_frame_reports_offset(self):
        builder = WalBuilder(1024)
        builder.add_frame(1, 1).add_frame(2, 2)
        wal = builder.bytes()[:-10]  # cut the last frame's page data short
        expect_error(
            self, "truncated_frame", minimal_db(1024), wal,
            offset=builder.frame_offset(2),
        )

    def test_truncated_first_frame(self):
        wal = WalBuilder(1024).add_frame(1, 1).bytes()[:100]
        expect_error(self, "truncated_frame", minimal_db(1024), wal, offset=32)

    def test_salt_change_reports_offset(self):
        builder = WalBuilder(1024)
        builder.add_frame(1, 2).add_frame(2, 2)
        wal = bytearray(builder.bytes())
        salt_at = builder.frame_offset(2) + 8
        wal[salt_at] ^= 0x01  # salt-1 of frame 2 no longer matches header
        expect_error(
            self, "salt_mismatch", minimal_db(1024), bytes(wal),
            offset=builder.frame_offset(2),
        )

    def test_invalid_page_number_reports_offset(self):
        builder = WalBuilder(1024)
        builder.add_frame(0, 1)  # page number 0 is never valid
        expect_error(
            self, "invalid_page_number", minimal_db(1024), builder.bytes(),
            offset=builder.frame_offset(1),
        )


class NoCommitTest(unittest.TestCase):
    def test_frames_without_commit(self):
        wal = WalBuilder(1024).add_frame(1, 0).add_frame(2, 0).bytes()
        err = expect_error(self, "no_commit", minimal_db(1024), wal, offset=None)
        self.assertIn("no complete commit", str(err))

    def test_header_only_wal(self):
        wal = WalBuilder(1024).bytes()
        expect_error(self, "no_commit", minimal_db(1024), wal, offset=None)


class HeaderValidationTest(unittest.TestCase):
    def test_wal_too_large(self):
        wal = b"\x00" * (MAX_WAL_SIZE + 1)
        expect_error(self, "wal_too_large", minimal_db(1024), wal, offset=None)

    def test_wal_shorter_than_header(self):
        expect_error(self, "invalid_wal_header", minimal_db(1024), b"\x00" * 10, 0)

    def test_bad_magic(self):
        wal = bytearray(WalBuilder(1024).bytes())
        wal[3] ^= 0xFF
        expect_error(self, "unsupported_magic", minimal_db(1024), bytes(wal), 0)

    def test_big_endian_checksum_wal_rejected(self):
        wal = WalBuilder(1024, magic=0x377F0683).bytes()
        expect_error(self, "unsupported_magic", minimal_db(1024), wal, 0)

    def test_bad_version(self):
        wal = WalBuilder(1024, version=3007001).bytes()
        expect_error(self, "unsupported_version", minimal_db(1024), wal, 4)

    def test_header_checksum_mismatch(self):
        wal = bytearray(WalBuilder(1024).bytes())
        wal[12] ^= 0x01  # salt-1 changed, header checksum not recomputed
        expect_error(
            self, "header_checksum_mismatch", minimal_db(1024), bytes(wal), 24
        )

    def test_page_size_limits(self):
        for bad in (256, 8192, 1000):
            wal = WalBuilder(bad).bytes()
            expect_error(self, "invalid_page_size", b"", wal, 8)
        for good in (512, 1024, 2048, 4096):
            wal = WalBuilder(good).add_frame(1, 1).bytes()
            result = recover(b"", wal)
            self.assertEqual(len(result["image"]), good)

    def test_page_size_mismatch(self):
        wal = WalBuilder(512).add_frame(1, 1).bytes()
        expect_error(self, "page_size_mismatch", minimal_db(1024), wal, None)

    def test_invalid_database(self):
        expect_error(self, "invalid_database", b"tiny", WalBuilder(1024).bytes())
        expect_error(
            self, "invalid_database", b"x" * 200, WalBuilder(1024).bytes()
        )

    def test_unsupported_database_page_size(self):
        db = bytearray(minimal_db(1024))
        struct.pack_into(">H", db, 16, 8192)
        wal = WalBuilder(1024).add_frame(1, 1).bytes()
        expect_error(self, "unsupported_page_size", bytes(db), wal, None)

    def test_image_too_large(self):
        wal = WalBuilder(1024).add_frame(1, 0x10000000).bytes()
        expect_error(self, "image_too_large", b"", wal, None)


if __name__ == "__main__":
    unittest.main()
