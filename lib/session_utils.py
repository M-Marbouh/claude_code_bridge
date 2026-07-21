"""
session_utils.py - Session file permission check utility
"""
from __future__ import annotations
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Tuple, Optional


CCB_PROJECT_CONFIG_DIRNAME = ".ccb"
CCB_PROJECT_CONFIG_LEGACY_DIRNAME = ".ccb_config"
CCB_SESSION_FILENAMES = (
    ".claude-session",
    ".codex-session",
    ".gemini-session",
    ".opencode-session",
)


def project_config_dir(work_dir: Path) -> Path:
    return Path(work_dir).resolve() / CCB_PROJECT_CONFIG_DIRNAME


def legacy_project_config_dir(work_dir: Path) -> Path:
    return Path(work_dir).resolve() / CCB_PROJECT_CONFIG_LEGACY_DIRNAME


def resolve_project_config_dir(work_dir: Path) -> Path:
    """Return primary config dir if present; otherwise legacy if it exists."""
    primary = project_config_dir(work_dir)
    legacy = legacy_project_config_dir(work_dir)
    if primary.is_dir() or not legacy.is_dir():
        return primary
    return legacy


def check_session_writable(session_file: Path) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Check if session file is writable

    Returns:
        (writable, error_reason, fix_suggestion)
    """
    session_file = Path(session_file)
    parent = session_file.parent

    # 1. Check if parent directory exists and is accessible
    if not parent.exists():
        return False, f"Directory not found: {parent}", f"mkdir -p {parent}"

    if not os.access(parent, os.X_OK):
        return False, f"Directory not accessible (missing x permission): {parent}", f"chmod +x {parent}"

    # 2. Check if parent directory is writable
    if not os.access(parent, os.W_OK):
        return False, f"Directory not writable: {parent}", f"chmod u+w {parent}"

    # 3. If file doesn't exist, directory writable is enough
    if not session_file.exists():
        return True, None, None

    # 4. Check if it's a regular file
    if session_file.is_symlink():
        target = session_file.resolve()
        return False, f"Is symlink pointing to {target}", f"rm -f {session_file}"

    if session_file.is_dir():
        return False, "Is directory, not file", f"rmdir {session_file} or rm -rf {session_file}"

    if not session_file.is_file():
        return False, "Not a regular file", f"rm -f {session_file}"

    # 5. Check file ownership (POSIX only)
    if os.name != "nt" and hasattr(os, "getuid"):
        try:
            file_stat = session_file.stat()
            file_uid = getattr(file_stat, "st_uid", None)
            current_uid = os.getuid()

            if isinstance(file_uid, int) and file_uid != current_uid:
                import pwd

                try:
                    owner_name = pwd.getpwuid(file_uid).pw_name
                except KeyError:
                    owner_name = str(file_uid)
                current_name = pwd.getpwuid(current_uid).pw_name
                return (
                    False,
                    f"File owned by {owner_name} (current user: {current_name})",
                    f"sudo chown {current_name}:{current_name} {session_file}",
                )
        except Exception:
            pass

    # 6. Check if file is writable
    if not os.access(session_file, os.W_OK):
        mode = stat.filemode(session_file.stat().st_mode)
        return False, f"File not writable (mode: {mode})", f"chmod u+w {session_file}"

    return True, None, None


def safe_write_session(session_file: Path, content: str) -> Tuple[bool, Optional[str]]:
    """
    Safely write session file, return friendly error on failure

    Returns:
        (success, error_message)
    """
    session_file = Path(session_file)

    # Pre-check
    writable, reason, fix = check_session_writable(session_file)
    if not writable:
        return False, f"❌ Cannot write {session_file.name}: {reason}\n💡 Fix: {fix}"

    # Attempt an owner-only atomic write. The mode is attached to the temporary
    # inode before rename, so the live target is never briefly group-readable.
    try:
        if os.name == "nt":
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{session_file.name}.",
                suffix=".tmp",
                dir=str(session_file.parent),
            )
            tmp_file = Path(tmp_name)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    fd = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_file, session_file)
                tmp_file = None
            finally:
                if fd >= 0:
                    os.close(fd)
                if tmp_file is not None:
                    try:
                        tmp_file.unlink()
                    except FileNotFoundError:
                        pass
        else:
            parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            parent_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
            parent_fd = os.open(session_file.parent, parent_flags)
            tmp_name = f".{session_file.name}.{secrets.token_hex(8)}.tmp"
            tmp_fd = -1
            try:
                parent_info = os.fstat(parent_fd)
                if not stat.S_ISDIR(parent_info.st_mode):
                    raise PermissionError("session parent is not a directory")
                if hasattr(os, "geteuid") and parent_info.st_uid != os.geteuid():
                    raise PermissionError("session parent is not owned by the current user")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
                tmp_fd = os.open(tmp_name, flags, 0o600, dir_fd=parent_fd)
                tmp_info = os.fstat(tmp_fd)
                if not stat.S_ISREG(tmp_info.st_mode):
                    raise PermissionError("session temporary is not a regular file")
                if hasattr(os, "geteuid") and tmp_info.st_uid != os.geteuid():
                    raise PermissionError("session temporary is not owned by the current user")
                os.fchmod(tmp_fd, 0o600)
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as handle:
                    tmp_fd = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(
                    tmp_name,
                    session_file.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            finally:
                if tmp_fd >= 0:
                    os.close(tmp_fd)
                try:
                    os.unlink(tmp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                os.close(parent_fd)
        return True, None
    except PermissionError as e:
        return False, f"❌ Cannot write {session_file.name}: {e}\n💡 Try: rm -f {session_file} then retry"
    except Exception as e:
        return False, f"❌ Write failed: {e}"


def _secure_existing_path(path: Path, *, expect_dir: bool, mode: int) -> Tuple[bool, Optional[str]]:
    """Validate and chmod one existing path through the same file descriptor."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, f"{path}: {exc}"

    if os.name == "nt":
        if path.is_symlink():
            return False, f"{path}: symbolic links are not allowed"
        try:
            info = path.stat()
            valid_type = stat.S_ISDIR(info.st_mode) if expect_dir else stat.S_ISREG(info.st_mode)
            if not valid_type:
                return False, f"{path}: unexpected path type"
            os.chmod(path, mode)
            return True, None
        except OSError as exc:
            return False, f"{path}: {exc}"

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    if expect_dir:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return False, f"{path}: {exc}"
    try:
        info = os.fstat(fd)
        valid_type = stat.S_ISDIR(info.st_mode) if expect_dir else stat.S_ISREG(info.st_mode)
        if not valid_type:
            return False, f"{path}: unexpected path type"
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return False, f"{path}: not owned by the current user"
        os.fchmod(fd, mode)
        return True, None
    except OSError as exc:
        return False, f"{path}: {exc}"
    finally:
        os.close(fd)


def remediate_ccb_permissions(
    work_dir: Path,
    *,
    home_dir: Optional[Path] = None,
) -> Tuple[bool, list[str]]:
    """Repair CCB-owned config/session modes without following symbolic links."""
    root = Path(work_dir).expanduser()
    home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    primary = root / CCB_PROJECT_CONFIG_DIRNAME
    legacy = root / CCB_PROJECT_CONFIG_LEGACY_DIRNAME
    global_ccb = home / ".ccb"

    directory_targets = [primary, legacy, global_ccb, global_ccb / "local", global_ccb / "run"]
    file_targets = [root / name for name in CCB_SESSION_FILENAMES]
    for directory in (primary, legacy):
        file_targets.append(directory / "ccb.config")
        file_targets.append(directory / ".mcpv-launch-status.json")
        file_targets.extend(directory / name for name in CCB_SESSION_FILENAMES)
    file_targets.append(global_ccb / "ccb.config")

    errors: list[str] = []
    secured_directories: list[Path] = []
    for path in directory_targets:
        ok, error = _secure_existing_path(path, expect_dir=True, mode=0o700)
        if not ok and error:
            errors.append(error)
        elif path.is_dir() and not path.is_symlink():
            secured_directories.append(path)
    for directory in secured_directories:
        if directory not in (primary, legacy):
            continue
        try:
            for entry in os.scandir(directory):
                if entry.name.startswith(".") and entry.name.endswith("-session"):
                    file_targets.append(Path(entry.path))
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
    file_targets = list(dict.fromkeys(file_targets))
    for path in file_targets:
        ok, error = _secure_existing_path(path, expect_dir=False, mode=0o600)
        if not ok and error:
            errors.append(error)
    return not errors, errors


def print_session_error(msg: str, to_stderr: bool = True) -> None:
    """Output session-related error"""
    import sys
    output = sys.stderr if to_stderr else sys.stdout
    print(msg, file=output)


def find_project_session_file(work_dir: Path, session_filename: str) -> Optional[Path]:
    """
    Find a session file for the given work_dir.

    Lookup walks upward from `work_dir` to support calls from subdirectories:
      1) <dir>/.ccb/<session_filename>
      2) <dir>/.ccb_config/<session_filename>  (legacy)
      3) <dir>/<session_filename>  (legacy)

    The nearest match wins.
    """
    try:
        current = Path(work_dir).resolve()
    except Exception:
        current = Path(work_dir).absolute()

    for root in [current, *current.parents]:
        candidate = root / CCB_PROJECT_CONFIG_DIRNAME / session_filename
        if candidate.exists():
            return candidate
        legacy_candidate = root / CCB_PROJECT_CONFIG_LEGACY_DIRNAME / session_filename
        if legacy_candidate.exists():
            return legacy_candidate
        legacy = root / session_filename
        if legacy.exists():
            return legacy
    return None
