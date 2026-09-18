"""Private file access, JSON records, path validation and advisory locks."""
import fcntl
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from errors import RecordError, StylesError


@contextmanager
def regular_file(path, *, write=False, limit=None):
    """Check the opened inode before reading or truncating it, never a symlink."""
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags | (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe file: expected a private regular file.")
        if limit is not None and info.st_size > limit:
            raise ValueError("File exceeds the size limit.")
        if write:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "w+b" if write else "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError("The style directory belongs to another user.")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def harden_tree(root):
    """Upgrade old stores without following links or changing outside inodes."""
    private_directory(root)
    for _, _, files, directory_fd in os.fwalk(root, follow_symlinks=False):
        os.fchmod(directory_fd, 0o700)
        for name in files:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid():
                    os.fchmod(fd, info.st_mode & 0o700)
            finally:
                os.close(fd)


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as exc:
        raise RecordError(f"Invalid JSON record: {path}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with regular_file(temporary, write=True) as handle:
            handle.write((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_slug(value):
    if not isinstance(value, str) or not value or value.startswith(".") or "/" in value or "\\" in value or any(ord(c) < 32 for c in value):
        raise StylesError("Invalid theme identifier.")
    return value


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise StylesError("Invalid saved style identifier.")
    return value


@contextmanager
def lock(path, *, shared=False, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(handle, flags | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise StylesError("A style operation is already running. Try again shortly.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
