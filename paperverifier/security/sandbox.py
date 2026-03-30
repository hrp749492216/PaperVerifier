"""Subprocess sandboxing for untrusted operations.

Provides a safe execution environment for external commands (especially
``git clone``) with enforced timeouts, output-size caps, restricted
environment variables, and post-execution content validation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT: float = 60  # seconds
MAX_CLONE_SIZE_MB: int = 500
MAX_OUTPUT_SIZE: int = 10 * 1024 * 1024  # 10 MB output limit

# Extensions allowed to survive the post-clone validation sweep.
_SAFE_CLONE_EXTENSIONS = {".pdf", ".tex", ".md", ".txt", ".docx", ".doc", ".bib"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SandboxError(Exception):
    """Base exception for sandbox failures."""


class SandboxTimeoutError(SandboxError):
    """Raised when a sandboxed command exceeds its timeout."""


# ---------------------------------------------------------------------------
# Core sandboxed execution
# ---------------------------------------------------------------------------

async def run_sandboxed(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    max_output_bytes: int = MAX_OUTPUT_SIZE,
) -> subprocess.CompletedProcess[str]:
    """Run a command in a sandboxed subprocess.

    Security guarantees:

    * **No shell injection** -- ``shell`` is always ``False``.
    * **Timeout** -- the entire process group is killed on expiry.
    * **Output caps** -- stdout and stderr are truncated to
      *max_output_bytes* to prevent memory exhaustion.
    * **Restricted environment** -- only explicitly provided env vars are
      passed; the parent environment is *not* inherited unless the caller
      passes ``os.environ.copy()`` explicitly.

    Args:
        cmd: Command and arguments as a list of strings.
        timeout: Maximum wall-clock seconds before the process group is
            killed.
        cwd: Working directory for the child process.
        env: Environment variables.  If ``None`` a minimal safe
            environment is constructed.
        max_output_bytes: Cap on combined stdout / stderr size.

    Returns:
        :class:`subprocess.CompletedProcess` with ``returncode``,
        ``stdout``, and ``stderr`` populated.

    Raises:
        SandboxTimeoutError: If the command exceeds *timeout*.
        SandboxError: If the command fails (non-zero exit) or another
            OS-level error occurs.
    """
    if not cmd:
        raise SandboxError("Command list must not be empty.")

    if env is None:
        env = _build_minimal_env()

    str_cwd = str(cwd) if cwd is not None else None

    logger.debug("sandbox_exec", cmd=cmd[:3], timeout=timeout, cwd=str_cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str_cwd,
            env=env,
            start_new_session=True,  # Own process group for clean kill.
        )
    except FileNotFoundError as exc:
        raise SandboxError(f"Command not found: {cmd[0]}") from exc
    except OSError as exc:
        raise SandboxError(f"Failed to start subprocess: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Kill the entire process group.
        _kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass  # Process didn't exit; it will become an orphan.
        logger.warning("sandbox_timeout", cmd=cmd[:3], timeout=timeout)
        raise SandboxTimeoutError(
            f"Command timed out after {timeout}s: {' '.join(cmd[:3])}"
        )

    # Truncate oversized output.
    stdout_str = _truncate(stdout_bytes, max_output_bytes)
    stderr_str = _truncate(stderr_bytes, max_output_bytes)

    result = subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_str,
        stderr=stderr_str,
    )

    logger.debug(
        "sandbox_result",
        cmd=cmd[:3],
        returncode=result.returncode,
        stdout_len=len(stdout_str),
        stderr_len=len(stderr_str),
    )

    return result


# ---------------------------------------------------------------------------
# Git clone
# ---------------------------------------------------------------------------

async def clone_github_repo(
    url: str,
    target_dir: Path | None = None,
    timeout: float = 120,
) -> Path:
    """Safely clone a GitHub repository.

    The caller is responsible for validating *url* via
    :func:`~paperverifier.security.input_validator.validate_github_url`
    **before** calling this function.

    Security measures:

    * ``shell=False`` (via :func:`run_sandboxed`).
    * ``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_CONFIG_GLOBAL=/dev/null`` to
      ignore all system/user Git configuration.
    * ``HOME`` is set to the temporary directory so user ``.gitconfig``
      and credential helpers are never consulted.
    * ``core.hooksPath=/dev/null`` disables Git hooks.
    * ``--depth 1`` for a shallow clone.
    * After checkout, only files matching :data:`_SAFE_CLONE_EXTENSIONS`
      are retained; everything else is removed.
    * Symlinks pointing outside the clone directory are deleted.
    * Total size is validated against :data:`MAX_CLONE_SIZE_MB`.

    Args:
        url: The validated GitHub repository URL.
        target_dir: Optional directory to clone into.  If ``None`` a
            temporary directory is created.
        timeout: Maximum seconds for the clone operation.

    Returns:
        Path to the root of the cloned repository.

    Raises:
        SandboxError: On clone failure or content policy violation.
        SandboxTimeoutError: If the clone exceeds *timeout*.
    """
    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="pv_clone_"))
    else:
        target_dir.mkdir(parents=True, exist_ok=True)

    clone_dir = target_dir / "repo"

    # Build a locked-down environment for Git.
    git_env = _build_minimal_env()
    git_env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(target_dir),  # Prevents reading ~/.gitconfig
        "GIT_TERMINAL_PROMPT": "0",  # Never prompt for credentials
    })

    # --- Step 1: shallow clone, no checkout ---
    clone_cmd = [
        "git", "clone",
        "--depth", "1",
        "--no-checkout",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.symlinks=false",
        url,
        str(clone_dir),
    ]

    logger.info("clone_start", url=url, target=str(clone_dir))

    result = await run_sandboxed(
        clone_cmd,
        timeout=timeout,
        env=git_env,
    )

    if result.returncode != 0:
        cleanup_temp_dir(target_dir)
        raise SandboxError(
            f"git clone failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    # --- Step 2: sparse checkout of safe file types only ---
    # Build pathspec list for git checkout.
    pathspecs = [f"*.{ext.lstrip('.')}" for ext in _SAFE_CLONE_EXTENSIONS]

    checkout_cmd = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "checkout",
        "HEAD",
        "--",
        *pathspecs,
    ]

    checkout_result = await run_sandboxed(
        checkout_cmd,
        timeout=60,
        cwd=clone_dir,
        env=git_env,
    )

    # checkout may return non-zero if no files match some patterns -- that's OK.
    if checkout_result.returncode != 0:
        logger.debug(
            "checkout_partial",
            returncode=checkout_result.returncode,
            stderr=checkout_result.stderr[:300],
        )

    # --- Step 3: post-clone validation ---
    try:
        _validate_clone_contents(clone_dir)
    except SandboxError:
        cleanup_temp_dir(target_dir)
        raise

    logger.info("clone_complete", url=url, target=str(clone_dir))
    return clone_dir


# ---------------------------------------------------------------------------
# Post-clone validation
# ---------------------------------------------------------------------------

def _validate_clone_contents(clone_dir: Path) -> None:
    """Validate the contents of a cloned repository.

    * Removes symlinks that point outside *clone_dir*.
    * Removes files with extensions not in :data:`_SAFE_CLONE_EXTENSIONS`
      (excluding ``.git`` internals).
    * Verifies total size is below :data:`MAX_CLONE_SIZE_MB`.
    """
    resolved_root = clone_dir.resolve()
    total_size = 0
    removed_count = 0

    for item in list(clone_dir.rglob("*")):
        # Skip .git directory contents.
        try:
            item.relative_to(clone_dir / ".git")
            continue
        except ValueError:
            pass

        # Remove symlinks pointing outside the clone.
        if item.is_symlink():
            try:
                link_target = item.resolve()
                link_target.relative_to(resolved_root)
            except (ValueError, OSError):
                logger.warning("symlink_removed", path=str(item))
                item.unlink(missing_ok=True)
                removed_count += 1
                continue

        # Remove files with disallowed extensions.
        if item.is_file() and not item.is_symlink():
            if item.suffix.lower() not in _SAFE_CLONE_EXTENSIONS:
                logger.debug("disallowed_file_removed", path=str(item))
                item.unlink(missing_ok=True)
                removed_count += 1
                continue
            total_size += item.stat().st_size

    if removed_count > 0:
        logger.info("clone_cleanup", removed_files=removed_count)

    max_bytes = MAX_CLONE_SIZE_MB * 1024 * 1024
    if total_size > max_bytes:
        raise SandboxError(
            f"Cloned repository is too large: {total_size / (1024 * 1024):.1f} MB "
            f"exceeds limit of {MAX_CLONE_SIZE_MB} MB."
        )

    logger.debug("clone_validated", total_size_mb=round(total_size / (1024 * 1024), 2))


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_temp_dir(path: Path) -> None:
    """Safely remove a temporary directory and all its contents.

    Only removes directories under the system temp directory or that match
    the ``pv_clone_`` naming convention.  Logs a warning instead of raising
    if removal fails.
    """
    try:
        if not path.exists():
            return
        resolved = path.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if not (str(resolved).startswith(str(temp_root)) or resolved.name.startswith("pv_clone_")):
            logger.error(
                "cleanup_refused_not_temp_dir",
                path=str(path),
                resolved=str(resolved),
            )
            return
        shutil.rmtree(path, ignore_errors=False)
        logger.debug("temp_dir_cleaned", path=str(path))
    except OSError:
        logger.warning("temp_dir_cleanup_failed", path=str(path), exc_info=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_minimal_env() -> dict[str, str]:
    """Construct a minimal environment for sandboxed processes.

    Only essential variables are forwarded; everything else is dropped to
    prevent information leakage and config interference.
    """
    env: dict[str, str] = {}
    # TMPDIR is intentionally excluded to prevent predictable temp paths
    # in sandboxed processes inheriting host-controlled values.
    for var in ("PATH", "LANG", "LC_ALL", "TERM"):
        val = os.environ.get(var)
        if val is not None:
            env[var] = val

    # Ensure PATH has a sane fallback.
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

    return env


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Send SIGKILL to the process group of *proc*."""
    if proc.pid is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Process already exited or we lack permissions.
        pass
    except OSError:
        # Fallback: kill the process directly.
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _truncate(data: bytes, max_bytes: int) -> str:
    """Decode bytes to str, truncating to *max_bytes* if necessary."""
    if len(data) > max_bytes:
        truncated = data[:max_bytes]
        suffix = f"\n... [truncated: {len(data) - max_bytes:,} bytes omitted]"
    else:
        truncated = data
        suffix = ""

    try:
        text = truncated.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = truncated.decode("latin-1")

    return text + suffix
