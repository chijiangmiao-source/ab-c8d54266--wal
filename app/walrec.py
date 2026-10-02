"""SQLite 3 WAL recovery engine.

Parses a SQLite 3 main database image and a standard WAL file from their
original raw bytes, validates the WAL header and every frame (magic, format
version, salts and the cumulative cross-frame checksum chain), and rebuilds
the database image as of the last frame that passes validation and carries a
non-zero commit database size.

Only little-endian-checksum WAL files are supported (magic 0x377F0682), which
is what SQLite writes on little-endian hosts.  Page sizes are restricted to
512..4096 bytes and the WAL to 2 MiB.

Failure policy is strict: a truncated frame, a checksum mismatch, a salt
change, an invalid page number or the absence of a complete commit aborts the
whole recovery.  The raised RecoverError pinpoints the byte offset of the
first invalid structure and no partial image is ever produced.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SQLITE_DB_MAGIC = b"SQLite format 3\x00"

WAL_MAGIC_LE = 0x377F0682  # standard WAL, little-endian checksums
WAL_MAGIC_BE = 0x377F0683  # big-endian checksums, not supported
WAL_FORMAT_VERSION = 3007000

WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24

MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 4096
MAX_WAL_SIZE = 2 * 1024 * 1024  # 2 MiB
MAX_IMAGE_SIZE = 64 * 1024 * 1024  # guard against absurd commit sizes
MAX_PAGE_NUMBER = 0xFFFFFFFE  # largest page number SQLite can address


class RecoverError(Exception):
    """Recovery failure with a machine-readable code and optional WAL offset."""

    def __init__(self, code: str, message: str, offset: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset


@dataclass
class Frame:
    number: int  # 1-based frame number
    offset: int  # byte offset of the frame header within the WAL
    page: int  # page number written by this frame
    commit_size: int  # db size in pages after commit; 0 => not a commit frame
    data: bytes  # page image carried by the frame (page_size bytes)


def _be32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _checksum(data: bytes, s1: int = 0, s2: int = 0) -> tuple[int, int]:
    """SQLite WAL checksum over little-endian 32-bit words.

    ``data`` length must be a multiple of 8.  Returns the running (s1, s2)
    pair so the caller can chain header, frame-header and page-data segments.
    """
    for i in range(0, len(data), 8):
        w0, w1 = struct.unpack_from("<II", data, i)
        s1 = (s1 + w0 + s2) & 0xFFFFFFFF
        s2 = (s2 + w1 + s1) & 0xFFFFFFFF
    return s1, s2


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def database_page_size(db: bytes) -> int | None:
    """Validate the main database header and return its page size.

    An empty database is accepted: a brand-new database may have every page
    still living in the WAL, in which case the WAL page size governs.
    """
    if len(db) == 0:
        return None
    if len(db) < 100:
        raise RecoverError(
            "invalid_database",
            "main database is shorter than the 100-byte SQLite header",
        )
    if db[:16] != SQLITE_DB_MAGIC:
        raise RecoverError(
            "invalid_database", "bad SQLite 3 magic in main database header"
        )
    raw = struct.unpack_from(">H", db, 16)[0]
    size = 65536 if raw == 1 else raw
    if not _is_power_of_two(size) or size < MIN_PAGE_SIZE or size > MAX_PAGE_SIZE:
        raise RecoverError(
            "unsupported_page_size",
            f"main database page size {size} outside 512..4096",
        )
    return size


def parse_wal(wal: bytes) -> tuple[int, list[Frame]]:
    """Parse and fully validate a WAL, returning ``(page_size, frames)``.

    The first invalid structure (bad header, truncated frame, salt change,
    invalid page number, checksum mismatch) raises RecoverError carrying the
    byte offset of that structure; no partial frame list is returned.
    """
    if len(wal) > MAX_WAL_SIZE:
        raise RecoverError(
            "wal_too_large", f"WAL is {len(wal)} bytes, limit is {MAX_WAL_SIZE}"
        )
    if len(wal) < WAL_HEADER_SIZE:
        raise RecoverError(
            "invalid_wal_header", "WAL is shorter than its 32-byte header", 0
        )

    magic = _be32(wal, 0)
    if magic == WAL_MAGIC_BE:
        raise RecoverError(
            "unsupported_magic",
            "big-endian-checksum WAL (0x377f0683) is not supported",
            0,
        )
    if magic != WAL_MAGIC_LE:
        raise RecoverError(
            "unsupported_magic", f"bad WAL magic 0x{magic:08x}", 0
        )

    version = _be32(wal, 4)
    if version != WAL_FORMAT_VERSION:
        raise RecoverError(
            "unsupported_version",
            f"unsupported WAL format version {version}",
            4,
        )

    page_size = _be32(wal, 8)
    if (
        not _is_power_of_two(page_size)
        or page_size < MIN_PAGE_SIZE
        or page_size > MAX_PAGE_SIZE
    ):
        raise RecoverError(
            "invalid_page_size",
            f"WAL page size {page_size} outside 512..4096",
            8,
        )

    salt1, salt2 = _be32(wal, 16), _be32(wal, 20)
    s1, s2 = _checksum(wal[:24])
    if (s1, s2) != (_be32(wal, 24), _be32(wal, 28)):
        raise RecoverError(
            "header_checksum_mismatch", "WAL header checksum mismatch", 24
        )

    frames: list[Frame] = []
    offset = WAL_HEADER_SIZE
    while offset < len(wal):
        number = len(frames) + 1
        if len(wal) - offset < FRAME_HEADER_SIZE + page_size:
            raise RecoverError(
                "truncated_frame",
                f"frame {number} is truncated at offset {offset}",
                offset,
            )
        header = wal[offset : offset + FRAME_HEADER_SIZE]
        page = _be32(header, 0)
        commit_size = _be32(header, 4)
        if (_be32(header, 8), _be32(header, 12)) != (salt1, salt2):
            raise RecoverError(
                "salt_mismatch",
                f"frame {number} salts do not match the WAL header",
                offset,
            )
        if page == 0 or page > MAX_PAGE_NUMBER:
            raise RecoverError(
                "invalid_page_number",
                f"frame {number} has invalid page number {page}",
                offset,
            )
        data = wal[offset + FRAME_HEADER_SIZE : offset + FRAME_HEADER_SIZE + page_size]
        s1, s2 = _checksum(header[:8], s1, s2)
        s1, s2 = _checksum(data, s1, s2)
        if (s1, s2) != (_be32(header, 16), _be32(header, 20)):
            raise RecoverError(
                "checksum_mismatch",
                f"frame {number} cumulative checksum mismatch",
                offset,
            )
        frames.append(Frame(number, offset, page, commit_size, data))
        offset += FRAME_HEADER_SIZE + page_size

    return page_size, frames


def recover(db: bytes, wal: bytes, stable_page_order: bool = False) -> dict:
    """Rebuild the database image as of the last valid commit frame.

    Returns a dict with ``commit_frame``, ``commit_size_pages``,
    ``recovered_pages``, ``pages`` (per-page source frame list) and the raw
    ``image`` bytes.  Raises RecoverError on any validation failure.
    """
    db_page_size = database_page_size(db)
    wal_page_size, frames = parse_wal(wal)
    if db_page_size is not None and wal_page_size != db_page_size:
        raise RecoverError(
            "page_size_mismatch",
            f"WAL page size {wal_page_size} != database page size {db_page_size}",
        )
    page_size = wal_page_size

    last_commit = None
    for frame in frames:
        if frame.commit_size != 0:
            last_commit = frame
    if last_commit is None:
        raise RecoverError("no_commit", "WAL contains no complete commit frame")

    total_pages = last_commit.commit_size
    image_size = total_pages * page_size
    if image_size > MAX_IMAGE_SIZE:
        raise RecoverError(
            "image_too_large",
            f"recovered image would be {image_size} bytes, limit {MAX_IMAGE_SIZE}",
        )

    # Within the committed prefix, the last occurrence of each page wins.
    pages: dict[int, tuple[int, bytes]] = {}
    for frame in frames:
        if frame.number > last_commit.number:
            break
        pages[frame.page] = (frame.number, frame.data)

    image = bytearray(image_size)
    copied = min(len(db), image_size)
    image[:copied] = db[:copied]
    for page, (_, data) in pages.items():
        start = (page - 1) * page_size
        if start + page_size <= image_size:
            image[start : start + page_size] = data

    # Only pages that survive in the final image are reported.
    visible = {page: source for page, (source, _) in pages.items() if page <= total_pages}
    if stable_page_order:
        ordered = sorted(visible.items(), key=lambda kv: kv[0])
    else:
        ordered = sorted(visible.items(), key=lambda kv: (kv[1], kv[0]))

    return {
        "commit_frame": last_commit.number,
        "commit_size_pages": total_pages,
        "recovered_pages": len(visible),
        "pages": [{"page": page, "source_frame": source} for page, source in ordered],
        "image": bytes(image),
    }
