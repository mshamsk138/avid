"""Durable, symlink-safe local file writes (report 10.1, audit 38).

- atomic_write: a fresh temporary file from mkstemp (O_EXCL, mode 0600, so no symlink can be planted at
  a predictable name), flush + fsync, rename over the target, then fsync of the directory. After a
  power cut the file is either the old or the new version, never a mix.
- append_line: O_APPEND | O_NOFOLLOW, optional fsync; repairs a torn last line first so that a line
  half-written before a crash cannot swallow the next record.
- private_dir: a directory only the owner can enter (0700).
- read_private: read a secret through ONE file descriptor opened with O_NOFOLLOW; the checks (regular
  file, owner, permissions) are made on that descriptor (fstat), so the file cannot be swapped between
  the check and the read (no TOCTOU).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def fsync_dir(d) -> None:
    try:
        fd = os.open(d, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def private_dir(p) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if p.stat().st_uid == os.getuid():
            os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def atomic_write(path, data, *, mode=0o600, durable=True) -> None:
    path = Path(path)
    if isinstance(data, str):
        data = data.encode('utf-8')
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if durable:
        fsync_dir(path.parent)


def _ends_with_newline(fd) -> bool:
    size = os.fstat(fd).st_size
    if size == 0:
        return True
    return os.pread(fd, 1, size - 1) == b'\n'


def append_line(path, line: str, *, durable=False, repair=True) -> None:
    data = line.encode('utf-8')
    if not data.endswith(b'\n'):
        data += b'\n'
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o600)
    try:
        if repair and not _ends_with_newline(fd):
            data = b'\n' + data                      # a torn line from a crash stays one bad line
        os.write(fd, data)
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)


def open_lock(path):
    """Lock file opened without following symlinks."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    return os.fdopen(fd, 'a+')


class InsecureFile(PermissionError):
    pass


def read_private(path, what: str, *, allow_shared_read: bool = False, max_bytes: int = 64 * 1024) -> str:
    """Text of a secret file. Refused: symlinks, non-regular files, files owned by another user (root is
    accepted, e.g. Docker secrets), files writable by group/others, and - unless allow_shared_read -
    files readable by group/others."""
    import stat as _stat
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except OSError as e:
        if getattr(e, 'errno', None) == 40:            # ELOOP: it is a symlink
            raise InsecureFile(f'{what} file {path} is a symlink; refusing to read it') from None
        raise
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise InsecureFile(f'{what} file {path} is not a regular file')
        if st.st_uid not in (os.getuid(), 0):
            raise InsecureFile(f'{what} file {path} belongs to another user')
        mode = st.st_mode & 0o777
        if mode & 0o022:
            raise InsecureFile(f'{what} file {path} is writable by others ({oct(mode)}); run: chmod 600 {path}')
        if mode & 0o044 and not allow_shared_read:
            raise InsecureFile(f'{what} file {path} permissions {oct(mode)} too open; run: chmod 600 {path}')
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise InsecureFile(f'{what} file {path} is unexpectedly large')
    return data.decode('utf-8')
