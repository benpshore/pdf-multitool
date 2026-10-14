"""Bounded, fail-closed archive intake. Never use extract()/extractall().

All input members, including skipped ones, are validated and fully drained.
Expanded-byte and ratio limits include TAR headers/padding. Gzip concatenation
is supported, but is a single logical file; inner archives are never expanded.
The caller must also supervise this code in a time/memory-limited subprocess.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import math
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
import tempfile
import time
import unicodedata
import zipfile
import zlib
from typing import Callable, Iterator


_MIB = 1024 * 1024
_CHUNK = 64 * 1024
_METADATA_LIMIT = 64 * 1024


class ArchiveError(ValueError):
    """Unsafe, invalid, cancelled, or over-limit archive."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_compressed_bytes: int = 50 * _MIB
    max_expanded_bytes: int = 500 * _MIB
    max_member_bytes: int = 50 * _MIB
    max_files: int = 200
    max_entries: int = 1000
    max_path_depth: int = 8
    max_compression_ratio: float = 100
    wall_time_seconds: float = 30

    def __post_init__(self):
        for field in ("max_compressed_bytes", "max_expanded_bytes", "max_member_bytes",
                      "max_files", "max_entries", "max_path_depth"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        for field in ("max_compression_ratio", "wall_time_seconds"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field} must be finite and positive")


class _Budget:
    def __init__(self, limits, compressed, cancelled):
        self.limits = limits
        self.compressed = compressed
        self.cancelled = cancelled
        self.deadline = time.monotonic() + limits.wall_time_seconds
        self.expanded = 0
        self.entries = 0

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise ArchiveError("Archive extraction cancelled")
        if time.monotonic() >= self.deadline:
            raise ArchiveError("Archive wall-time limit exceeded")

    def add(self, size):
        self.check()
        self.expanded += size
        if self.expanded > self.limits.max_expanded_bytes:
            raise ArchiveError("Archive total expanded-byte limit exceeded")
        if self.expanded > max(1, self.compressed) * self.limits.max_compression_ratio:
            raise ArchiveError("Archive compression-ratio limit exceeded")

    def entry(self):
        self.check()
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise ArchiveError("Archive entry limit exceeded")


def _name_parts(name: str, limits: ArchiveLimits, *, directory=False):
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096:
        raise ArchiveError("Invalid archive member name")
    if name.startswith(("/", "\\")) or "\\" in name or ":" in name:
        raise ArchiveError("Archive member has an absolute, drive, UNC, or unsafe path")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in name):
        raise ArchiveError("Archive member name contains control characters")
    parts = []
    for part in name.split("/"):
        if part == "..":
            raise ArchiveError("Archive member path traversal rejected")
        if part in {"", "."}:
            continue
        if part.endswith((".", " ")):
            raise ArchiveError("Archive member name has an ambiguous trailing dot or space")
        # These names have special meanings on Windows, even with an extension.
        if part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ArchiveError("Archive member uses a reserved device name")
        parts.append(part)
    if not parts and not directory:
        raise ArchiveError("Archive file has an empty normalized name")
    if len(parts) > limits.max_path_depth:
        raise ArchiveError("Archive path-depth limit exceeded")
    return parts


class _Names:
    def __init__(self, limits):
        self.limits = limits
        self.entries = set()
        self.spellings = {}
        self.kinds = {}

    def add(self, name, directory=False):
        parts = _name_parts(name, self.limits, directory=directory)
        keys = []
        for i, part in enumerate(parts):
            keys.append(unicodedata.normalize("NFC", part).casefold())
            key = "/".join(keys)
            spelling = "/".join(parts[:i + 1])
            if key in self.spellings and self.spellings[key] != spelling:
                raise ArchiveError("Archive name normalization or case collision")
            self.spellings[key] = spelling
            kind = "directory" if i < len(parts) - 1 or directory else "file"
            if key in self.kinds and self.kinds[key] != kind:
                raise ArchiveError("Archive file/directory path collision")
            self.kinds[key] = kind
        key = "/".join(keys)
        if key in self.entries:
            raise ArchiveError("Duplicate archive member name")
        self.entries.add(key)
        return parts


def _open_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ArchiveError(f"Cannot read regular archive source: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArchiveError("Archive source must be a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _archive_magic(head):
    return (head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c", b"Rar!"))
            or head[257:262] == b"ustar")


def _classification(name, head):
    lower = name.lower()
    if lower.endswith((".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar")) or _archive_magic(head):
        return None, "nested archive (not expanded)"
    suffix = Path(name).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".j2k"}:
        magic = (head.startswith(b"\xff\xd8\xff") if suffix in {".jpg", ".jpeg"}
                 else head.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png"
                 else head.startswith((b"\x00\x00\x00\x0cjP  \r\n\x87\n", b"\xff\x4f\xff\x51")))
        return (suffix, None) if magic else (None, "image extension does not match file signature")
    if suffix not in {".pdf", ".html", ".htm"}:
        return None, "unsupported file type"
    if suffix == ".pdf":
        if b"%PDF-" not in head[:1024]:
            return None, "PDF extension does not match file content"
    else:
        if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
            return None, "HTML extension does not match file content"
        encoding = "utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = head.decode(encoding, errors="replace")
        if not re.search(r"<!doctype\s+html\b|<!--|</?[a-z][a-z0-9:-]*(?:\s|/?>)", text, re.IGNORECASE):
            return None, "HTML extension does not match file content"
    return suffix, None


def _with_prefix(chunks):
    """Coalesce a small sniffing prefix even when decompression yields tiny chunks."""
    iterator = iter(chunks)
    first = bytearray()
    for chunk in iterator:
        first.extend(chunk)
        if len(first) >= 4096:
            break
    if first:
        yield bytes(first)
    yield from iterator


class _Intake:
    def __init__(self, destination, dirfd, budget):
        self.destination = destination
        self.dirfd = dirfd
        self.budget = budget
        self.names = _Names(budget.limits)
        self.files = []
        self.warnings = []
        self.created = []
        self.skipped = 0

    def member(self, name, chunks: Iterator[bytes], expected=None):
        parts = self.names.add(name)
        total = 0
        output = None
        result = None
        reason = None
        try:
            for chunk in _with_prefix(chunks):
                self.budget.check()
                total += len(chunk)
                if total > self.budget.limits.max_member_bytes:
                    raise ArchiveError("Archive member expanded-byte limit exceeded")
                if result is None and reason is None:
                    suffix, reason = _classification(name, chunk[:4096])
                    if suffix:
                        if len(self.files) >= self.budget.limits.max_files:
                            raise ArchiveError("Archive accepted-file limit exceeded")
                        filename = f"document-{len(self.files) + 1:04d}{suffix}"
                        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self.dirfd)
                        self.created.append(filename)
                        output = os.fdopen(fd, "wb")
                        result = {"path": str(self.destination / filename), "name": parts[-1], "member_path": name}
                if output is not None:
                    output.write(chunk)
            if expected is not None and total != expected:
                raise ArchiveError("Archive member size does not match its header")
            if result is not None:
                result["size"] = total
                self.files.append(result)
            else:
                self.skipped += 1
                self.warnings.append(f"Skipped {name!r}: {reason or 'empty or unsupported file content'}.")
        finally:
            if output is not None:
                output.close()


def _zip_directory(source, size, budget):
    """Bound central-directory parsing before ZipFile can allocate ZipInfo objects."""
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or len(tail) - pos < 22:
        raise ArchiveError("Malformed ZIP end record")
    signature, disk, central_disk, disk_count, count, central_size, central_offset, comment_len = struct.unpack("<4s4H2IH", tail[pos:pos + 22])
    eocd = size - len(tail) + pos
    if pos + 22 + comment_len != len(tail) or disk or central_disk or disk_count != count:
        raise ArchiveError("Unsupported multipart or malformed ZIP")
    if count == 65535 or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        raise ArchiveError("ZIP64 directories are not supported by bounded intake")
    if count > budget.limits.max_entries:
        raise ArchiveError("Archive entry limit exceeded")
    if central_offset + central_size != eocd:
        raise ArchiveError("Malformed ZIP central-directory bounds")
    source.seek(central_offset)
    actual = 0
    while source.tell() < eocd:
        budget.check()
        fixed = source.read(46)
        if len(fixed) != 46 or fixed[:4] != b"PK\x01\x02":
            raise ArchiveError("Malformed ZIP central directory")
        actual += 1
        if actual > budget.limits.max_entries:
            raise ArchiveError("Archive entry limit exceeded")
        name_len, extra_len, member_comment = struct.unpack_from("<3H", fixed, 28)
        end = source.tell() + name_len + extra_len + member_comment
        if end > eocd:
            raise ArchiveError("Malformed ZIP central-directory entry")
        source.seek(end)
    if actual != count:
        raise ArchiveError("ZIP central-directory entry count mismatch")
    return central_offset


def _zip_chunks(source, info, offset, budget):
    source.seek(offset)
    remaining = info.compress_size
    actual = 0
    crc = 0
    decoder = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    while remaining:
        budget.check()
        data = source.read(min(_CHUNK, remaining))
        if not data:
            raise ArchiveError("Truncated ZIP member")
        remaining -= len(data)
        while data:
            if decoder is None:
                chunk, data = data, b""
            else:
                chunk = decoder.decompress(data, _CHUNK)
                data = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ArchiveError("ZIP member contains trailing compressed data")
            actual += len(chunk)
            budget.add(len(chunk))
            if actual > budget.limits.max_member_bytes:
                raise ArchiveError("Archive member expanded-byte limit exceeded")
            if actual > max(1, info.compress_size) * budget.limits.max_compression_ratio:
                raise ArchiveError("Archive member compression-ratio limit exceeded")
            crc = zlib.crc32(chunk, crc)
            if chunk:
                yield chunk
    if decoder is not None and not decoder.eof:
        raise ArchiveError("Truncated ZIP deflate stream")
    if actual != info.file_size:
        raise ArchiveError("ZIP expanded size does not match its header")
    if crc & 0xFFFFFFFF != info.CRC:
        raise ArchiveError("ZIP CRC integrity check failed")


def _extract_zip(source, intake):
    budget = intake.budget
    central = _zip_directory(source, budget.compressed, budget)
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        ranges = []
        for info in members:
            budget.entry()
            name = info.orig_filename
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            directory = info.is_dir() or kind == stat.S_IFDIR
            _name_parts(name, budget.limits, directory=directory)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ArchiveError("ZIP links, devices, and special files are rejected")
            if info.flag_bits & (1 | 32 | 64):
                raise ArchiveError("Encrypted or patched ZIP members are rejected")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ArchiveError("Unsupported ZIP compression method")
            if info.file_size > budget.limits.max_member_bytes:
                raise ArchiveError("Archive member expanded-byte limit exceeded")
            if info.file_size > max(1, info.compress_size) * budget.limits.max_compression_ratio:
                raise ArchiveError("Archive member compression-ratio limit exceeded")
            if info.header_offset < 0 or info.header_offset + 30 > central:
                raise ArchiveError("Invalid ZIP local-header bounds")
            source.seek(info.header_offset)
            header = source.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise ArchiveError("Invalid ZIP local header")
            flags, method = struct.unpack_from("<HH", header, 6)
            name_len, extra_len = struct.unpack_from("<HH", header, 26)
            raw_name = source.read(name_len)
            encoding = "utf-8" if flags & 0x800 else "cp437"
            if raw_name.decode(encoding) != name or flags != info.flag_bits or method != info.compress_type:
                raise ArchiveError("ZIP local and central headers disagree")
            offset = info.header_offset + 30 + name_len + extra_len
            end = offset + info.compress_size
            if end > central or offset > central or any(info.header_offset < old_end and end > old_start for old_start, old_end in ranges):
                raise ArchiveError("Overlapping or out-of-bounds ZIP members")
            ranges.append((info.header_offset, end))
            chunks = _zip_chunks(source, info, offset, budget)
            if directory:
                intake.names.add(name, True)
                if info.file_size:
                    raise ArchiveError("ZIP directory has a payload")
                for chunk in chunks:
                    if chunk:
                        raise ArchiveError("ZIP directory has a payload")
            else:
                intake.member(name, chunks, info.file_size)


class _CompressedReader:
    """Enforce time and actual input bounds even while gzip scans empty members."""
    def __init__(self, stream, budget):
        self.stream, self.budget, self.count = stream, budget, 0

    def read(self, size=-1):
        self.budget.check()
        chunk = self.stream.read(min(size, _CHUNK) if size >= 0 else _CHUNK)
        self.count += len(chunk)
        if self.count > self.budget.limits.max_compressed_bytes:
            raise ArchiveError("Archive compressed-byte limit exceeded")
        self.budget.check()
        return chunk


class _ExpandedReader:
    def __init__(self, stream, budget):
        self.stream = stream
        self.budget = budget

    def read(self, amount):
        self.budget.check()
        chunk = self.stream.read(min(amount, _CHUNK))
        self.budget.add(len(chunk))
        return chunk


def _exact(reader, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            raise ArchiveError("Truncated TAR archive")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _tar_payload(reader, size):
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            raise ArchiveError("Truncated TAR member")
        remaining -= len(chunk)
        yield chunk


def _pax(data, limits):
    result = {}
    offset = 0
    while offset < len(data):
        space = data.find(b" ", offset, offset + 12)
        if space < 0 or not data[offset:space].isdigit():
            raise ArchiveError("Malformed TAR PAX record")
        length = int(data[offset:space])
        end = offset + length
        if end > len(data) or end <= space + 2 or data[end - 1:end] != b"\n":
            raise ArchiveError("Malformed TAR PAX record length")
        record = data[space + 1:end - 1].decode("utf-8")
        if "=" not in record:
            raise ArchiveError("Malformed TAR PAX field")
        key, value = record.split("=", 1)
        if key in result or "sparse" in key.lower() or key == "linkpath" or key in {"SCHILY.filetype", "SCHILY.realsize"}:
            raise ArchiveError("Unsafe or duplicate TAR PAX metadata")
        if key == "path":
            _name_parts(value, limits)
        if key == "size" and (not value.isascii() or not value.isdecimal() or len(value) > 12 or int(value) > limits.max_member_bytes):
            raise ArchiveError("Invalid or excessive TAR PAX member size")
        result[key] = value
        offset = end
    return result


def _extract_tar(reader, intake):
    pending = {}
    while True:
        block = _exact(reader, 512)
        if block == b"\0" * 512:
            if pending:
                raise ArchiveError("TAR ends with unapplied extended metadata")
            if _exact(reader, 512) != b"\0" * 512:
                raise ArchiveError("Malformed TAR end marker")
            while chunk := reader.read(_CHUNK):
                if any(chunk):
                    raise ArchiveError("Nonzero data follows TAR end marker")
            return
        intake.budget.entry()
        info = tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="strict")
        _name_parts(info.name, intake.budget.limits, directory=info.isdir())
        if info.type == tarfile.GNUTYPE_SPARSE or info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME}:
            raise ArchiveError("TAR links, sparse files, devices, FIFOs, and special files are rejected")
        if info.size < 0 or info.size > intake.budget.limits.max_member_bytes:
            raise ArchiveError("Archive member expanded-byte limit exceeded")
        if info.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME}:
            if info.size > _METADATA_LIMIT:
                raise ArchiveError("TAR extended metadata limit exceeded")
            data = _exact(reader, info.size)
            if info.type == tarfile.GNUTYPE_LONGNAME:
                name = data.rstrip(b"\0").decode("utf-8")
                _name_parts(name, intake.budget.limits)
                metadata = {"path": name}
            else:
                metadata = _pax(data, intake.budget.limits)
            if info.type == tarfile.XGLTYPE:
                if "path" in metadata or "size" in metadata:
                    raise ArchiveError("Global TAR path/size overrides are rejected")
            else:
                if pending:
                    raise ArchiveError("Conflicting TAR extended headers")
                pending = metadata
            if info.size % 512:
                _exact(reader, 512 - info.size % 512)
            continue
        name = pending.get("path", info.name)
        size = int(pending.get("size", info.size))
        pending = {}
        if info.isdir():
            intake.names.add(name, True)
            if size:
                raise ArchiveError("TAR directory has a payload")
        else:
            intake.member(name, _tar_payload(reader, size), size)
        if size % 512:
            _exact(reader, 512 - size % 512)


def extract_archive(source: Path, destination: Path, original_name: str,
                    limits: ArchiveLimits = ArchiveLimits(), *,
                    cancelled: Callable[[], bool] | None = None) -> dict:
    """Extract safe document members, or raise ArchiveError and remove all output.

    destination must be an existing empty nonsymlink directory; it is made 0700.
    Paths in the returned manifest are absolute and names are metadata only.
    Process termination must additionally be cleaned up by the caller.
    """
    destination = Path(destination).absolute()
    source = Path(source)
    if not isinstance(original_name, str) or not original_name:
        raise ArchiveError("An original archive filename is required")
    dirfd = None
    intake = None
    try:
        dirfd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        if os.listdir(dirfd):
            raise ArchiveError("Archive destination must be an empty directory")
        os.fchmod(dirfd, 0o700)
        with _open_regular(source) as stream:
            compressed = os.fstat(stream.fileno()).st_size
            if compressed > limits.max_compressed_bytes:
                raise ArchiveError("Archive compressed-byte limit exceeded")
            if not compressed:
                raise ArchiveError("Empty archive input")
            budget = _Budget(limits, compressed, cancelled)
            intake = _Intake(destination, dirfd, budget)
            budget.check()
            magic = stream.read(4)
            stream.seek(0)
            lower = original_name.lower()
            if lower.endswith(".zip"):
                if magic not in {b"PK\x03\x04", b"PK\x05\x06"}:
                    raise ArchiveError("ZIP extension does not match archive content")
                archive_format = "zip"
                _extract_zip(stream, intake)
            elif lower.endswith((".tar.gz", ".tgz", ".gz")):
                if not magic.startswith(b"\x1f\x8b"):
                    raise ArchiveError("Gzip extension does not match archive content")
                with gzip.GzipFile(fileobj=_CompressedReader(stream, budget), mode="rb") as inflated:
                    reader = _ExpandedReader(inflated, budget)
                    if lower.endswith((".tar.gz", ".tgz")):
                        archive_format = "tar.gz"
                        _extract_tar(reader, intake)
                    else:
                        archive_format = "gzip"
                        budget.entry()
                        name = original_name[:-3]
                        intake.member(name, iter(lambda: reader.read(_CHUNK), b""))
            elif lower.endswith(".tar"):
                archive_format = "tar"
                _extract_tar(_ExpandedReader(stream, budget), intake)
            else:
                raise ArchiveError("Supported archive formats are ZIP, TAR, TAR.GZ/TGZ, and GZ")
            budget.check()
            return {"files": intake.files, "warnings": intake.warnings,
                    "statistics": {"format": archive_format, "compressed_bytes": compressed,
                                   "expanded_bytes": budget.expanded, "entries": budget.entries,
                                   "accepted_files": len(intake.files), "skipped_files": intake.skipped}}
    except BaseException as exc:
        if intake is not None:
            for filename in intake.created:
                try:
                    os.unlink(filename, dir_fd=dirfd)
                except FileNotFoundError:
                    pass
        if isinstance(exc, (ArchiveError, KeyboardInterrupt, SystemExit)):
            raise
        raise ArchiveError(f"Invalid archive: {exc}") from exc
    finally:
        if dirfd is not None:
            os.close(dirfd)


class _BoundedOutput:
    def __init__(self, stream, budget, maximum):
        self.stream, self.budget, self.maximum = stream, budget, maximum

    def write(self, data):
        self.budget.check()
        if self.stream.tell() + len(data) > self.maximum:
            raise ArchiveError("Output archive byte limit exceeded")
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()

    def seek(self, *args):
        self.budget.check()
        return self.stream.seek(*args)

    def flush(self):
        return self.stream.flush()


class _CountedSource:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def read(self, size):
        self.budget.check()
        data = self.stream.read(size)
        self.budget.add(len(data))
        return data


def archive_output(files: list[dict], destination: Path, format: str = "zip") -> dict:
    """Package at most 1,000 regular results, 500 MiB input/output, within 30s.

    Optional archive_path preserves safe relative paths for companion assets;
    otherwise name must be a flat basename. Collisions are rejected. The final
    destination filepath is created exclusively and removed on any failure.
    No source timestamps, ownership, permissions, or symlink targets are copied.
    The caller must also supervise process memory, time, and kill cleanup.
    """
    if format not in {"zip", "tar.gz"}:
        raise ArchiveError("Output archive format must be zip or tar.gz")
    if not files or len(files) > 1000:
        raise ArchiveError("Output archive requires 1 to 1000 files")
    limits = ArchiveLimits()
    budget = _Budget(limits, limits.max_expanded_bytes, None)
    names = _Names(limits)
    planned = []
    total = 0
    for item in files:
        budget.check()
        name = item.get("archive_path", item["name"])
        parts = names.add(name)
        if name != "/".join(parts) or ("archive_path" not in item and len(parts) != 1):
            raise ArchiveError("Output archive names must be exact safe relative paths")
        with _open_regular(item["path"]) as src:
            size = os.fstat(src.fileno()).st_size
        total += size
        if total > limits.max_expanded_bytes:
            raise ArchiveError("Output archive input-byte limit exceeded")
        planned.append((item["path"], name, size))
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ArchiveError("Output archive destination already exists")
    temporary = None
    published = False
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=".archive-", suffix=".part", dir=destination.parent)
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as raw:
            out = _BoundedOutput(raw, budget, limits.max_expanded_bytes)
            if format == "zip":
                with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for path, name, size in planned:
                        with _open_regular(path) as src:
                            if os.fstat(src.fileno()).st_size != size:
                                raise ArchiveError("Output source changed during packaging")
                            counted = _CountedSource(src, budget)
                            info = zipfile.ZipInfo(name)
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.external_attr = (stat.S_IFREG | 0o600) << 16
                            copied = 0
                            with archive.open(info, "w") as member:
                                while chunk := counted.read(_CHUNK):
                                    copied += len(chunk)
                                    member.write(chunk)
                            if copied != size:
                                raise ArchiveError("Output source changed during packaging")
            else:
                with gzip.GzipFile(fileobj=out, mode="wb", filename="", mtime=0) as gz:
                    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                        for path, name, size in planned:
                            with _open_regular(path) as src:
                                if os.fstat(src.fileno()).st_size != size:
                                    raise ArchiveError("Output source changed during packaging")
                                info = tarfile.TarInfo(name)
                                info.size = size
                                info.mode = 0o600
                                info.mtime = 0
                                archive.addfile(info, _CountedSource(src, budget))
        budget.check()
        # A hard-link publication is atomic and fails if the destination appeared
        # concurrently. Unlike replace(), it can never overwrite another file.
        os.link(temporary, destination)
        published = True
        temporary.unlink()
        temporary = None
        return {"path": str(destination), "name": destination.name, "size": destination.stat().st_size}
    except BaseException as exc:
        if published:
            destination.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(exc, (ArchiveError, KeyboardInterrupt, SystemExit)):
            raise
        raise ArchiveError(f"Cannot create output archive: {exc}") from exc
