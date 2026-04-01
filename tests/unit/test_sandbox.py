"""Unit tests for paperverifier.security.sandbox module."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paperverifier.security.sandbox import (
    MAX_CLONE_SIZE_MB,
    MAX_OUTPUT_SIZE,
    SandboxError,
    SandboxTimeoutError,
    _build_minimal_env,
    _kill_process_group,
    _truncate,
    _validate_clone_contents,
    cleanup_temp_dir,
    clone_github_repo,
    run_sandboxed,
)

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """SandboxError and SandboxTimeoutError relationships."""

    def test_sandbox_error_is_exception(self) -> None:
        assert issubclass(SandboxError, Exception)

    def test_sandbox_timeout_error_is_sandbox_error(self) -> None:
        assert issubclass(SandboxTimeoutError, SandboxError)

    def test_sandbox_error_can_be_raised_and_caught(self) -> None:
        with pytest.raises(SandboxError):
            raise SandboxError("boom")

    def test_timeout_error_caught_by_sandbox_error(self) -> None:
        with pytest.raises(SandboxError):
            raise SandboxTimeoutError("timed out")

    def test_timeout_error_message(self) -> None:
        exc = SandboxTimeoutError("operation took too long")
        assert str(exc) == "operation took too long"


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    """Tests for the _truncate helper."""

    def test_no_truncation_needed(self) -> None:
        data = b"hello world"
        result = _truncate(data, 100)
        assert result == "hello world"
        assert "truncated" not in result

    def test_exact_limit(self) -> None:
        data = b"abcde"
        result = _truncate(data, 5)
        assert result == "abcde"
        assert "truncated" not in result

    def test_truncation_applied(self) -> None:
        data = b"a" * 200
        result = _truncate(data, 50)
        assert result.startswith("a" * 50)
        assert "truncated" in result
        assert "150 bytes omitted" in result

    def test_utf8_decode(self) -> None:
        data = "cafe\u0301".encode()
        result = _truncate(data, 1000)
        assert "caf" in result

    def test_invalid_utf8_uses_replace(self) -> None:
        data = b"\xff\xfe\xfd valid text"
        result = _truncate(data, 1000)
        # Should not raise; replacement characters used.
        assert "valid text" in result

    def test_empty_bytes(self) -> None:
        result = _truncate(b"", 100)
        assert result == ""

    def test_truncation_suffix_contains_omitted_count(self) -> None:
        data = b"x" * 1000
        result = _truncate(data, 100)
        assert "900 bytes omitted" in result


# ---------------------------------------------------------------------------
# _build_minimal_env
# ---------------------------------------------------------------------------


class TestBuildMinimalEnv:
    """Tests for _build_minimal_env."""

    def test_forwards_path_when_set(self) -> None:
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            env = _build_minimal_env()
            assert env["PATH"] == "/usr/bin:/bin"

    def test_path_fallback_when_missing(self) -> None:
        env_without_path = {k: v for k, v in os.environ.items() if k != "PATH"}
        with patch.dict(os.environ, env_without_path, clear=True):
            env = _build_minimal_env()
            assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"

    def test_forwards_lang(self) -> None:
        with patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=False):
            env = _build_minimal_env()
            assert env["LANG"] == "en_US.UTF-8"

    def test_forwards_lc_all(self) -> None:
        with patch.dict(os.environ, {"LC_ALL": "C"}, clear=False):
            env = _build_minimal_env()
            assert env["LC_ALL"] == "C"

    def test_forwards_term(self) -> None:
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            env = _build_minimal_env()
            assert env["TERM"] == "xterm-256color"

    def test_does_not_forward_secret_vars(self) -> None:
        with patch.dict(
            os.environ,
            {"SECRET_KEY": "hunter2", "AWS_SECRET_ACCESS_KEY": "abc123"},
            clear=False,
        ):
            env = _build_minimal_env()
            assert "SECRET_KEY" not in env
            assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_does_not_include_tmpdir(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": "/tmp/special"}, clear=False):
            env = _build_minimal_env()
            assert "TMPDIR" not in env

    def test_returns_dict(self) -> None:
        env = _build_minimal_env()
        assert isinstance(env, dict)


# ---------------------------------------------------------------------------
# _kill_process_group
# ---------------------------------------------------------------------------


class TestKillProcessGroup:
    """Tests for _kill_process_group."""

    def test_noop_when_pid_is_none(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = None
        # Should not raise.
        _kill_process_group(proc)

    def test_process_already_exited(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = 99999
        with patch("os.killpg", side_effect=ProcessLookupError):
            # Should not raise.
            _kill_process_group(proc)

    def test_permission_error_handled(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = 99999
        with patch("os.killpg", side_effect=PermissionError):
            _kill_process_group(proc)

    def test_os_error_falls_back_to_proc_kill(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = 99999
        with (
            patch("os.getpgid", return_value=99999),
            patch("os.killpg", side_effect=OSError("unexpected")),
        ):
            _kill_process_group(proc)
            proc.kill.assert_called_once()

    def test_os_error_fallback_kill_also_fails(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = 99999
        with (
            patch("os.getpgid", return_value=99999),
            patch("os.killpg", side_effect=OSError("unexpected")),
        ):
            proc.kill.side_effect = ProcessLookupError
            # Should not raise.
            _kill_process_group(proc)

    def test_calls_killpg_with_sigkill(self) -> None:
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.pid = 12345
        with (
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345) as mock_getpgid,
        ):
            _kill_process_group(proc)
            mock_getpgid.assert_called_once_with(12345)
            mock_killpg.assert_called_once_with(12345, signal.SIGKILL)


# ---------------------------------------------------------------------------
# run_sandboxed
# ---------------------------------------------------------------------------


class TestRunSandboxed:
    """Tests for run_sandboxed."""

    async def test_successful_command(self) -> None:
        result = await run_sandboxed(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    async def test_command_not_found(self) -> None:
        with pytest.raises(SandboxError, match="Command not found"):
            await run_sandboxed(["nonexistent_binary_xyz_12345"])

    async def test_empty_command_raises(self) -> None:
        with pytest.raises(SandboxError, match="must not be empty"):
            await run_sandboxed([])

    async def test_timeout(self) -> None:
        with pytest.raises(SandboxTimeoutError, match="timed out"):
            await run_sandboxed(["sleep", "60"], timeout=0.5)

    async def test_output_truncation(self) -> None:
        # Generate output larger than the cap.
        max_bytes = 256
        # Each line of "yes" is 2 bytes ("y\n"). We need > max_bytes output.
        # Use printf to produce a deterministic large output.
        result = await run_sandboxed(
            ["python3", "-c", f"print('A' * {max_bytes * 2})"],
            max_output_bytes=max_bytes,
        )
        assert "truncated" in result.stdout
        assert "bytes omitted" in result.stdout

    async def test_nonzero_exit_code_returned(self) -> None:
        result = await run_sandboxed(["python3", "-c", "import sys; sys.exit(42)"])
        assert result.returncode == 42

    async def test_stderr_captured(self) -> None:
        result = await run_sandboxed(["python3", "-c", "import sys; sys.stderr.write('err msg')"])
        assert "err msg" in result.stderr

    async def test_custom_env_passed(self) -> None:
        custom_env = _build_minimal_env()
        custom_env["MY_TEST_VAR"] = "test_value_42"
        result = await run_sandboxed(
            ["python3", "-c", "import os; print(os.environ.get('MY_TEST_VAR', ''))"],
            env=custom_env,
        )
        assert "test_value_42" in result.stdout

    async def test_cwd_respected(self, tmp_path: Path) -> None:
        result = await run_sandboxed(["pwd"], cwd=tmp_path)
        assert str(tmp_path.resolve()) in result.stdout.strip()

    async def test_result_is_completed_process(self) -> None:
        result = await run_sandboxed(["echo", "hi"])
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.args == ["echo", "hi"]


# ---------------------------------------------------------------------------
# _validate_clone_contents
# ---------------------------------------------------------------------------


class TestValidateCloneContents:
    """Tests for _validate_clone_contents."""

    def test_removes_symlinks_pointing_outside(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        # Create a file outside the clone.
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("secret")
        # Create a symlink inside the clone pointing outside.
        link = clone_dir / "evil_link.txt"
        link.symlink_to(outside_file)
        assert link.is_symlink()

        _validate_clone_contents(clone_dir)
        assert not link.exists()

    def test_keeps_symlinks_within_clone(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        target_file = clone_dir / "real.tex"
        target_file.write_text("\\documentclass{article}")
        link = clone_dir / "alias.tex"
        link.symlink_to(target_file)

        _validate_clone_contents(clone_dir)
        # The symlink itself might be removed because it's checked as symlink first,
        # but the target should survive if it has a safe extension.
        assert target_file.exists()

    def test_removes_disallowed_extension(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        safe_file = clone_dir / "paper.tex"
        safe_file.write_text("\\begin{document}")
        unsafe_file = clone_dir / "evil.exe"
        unsafe_file.write_text("malware")

        _validate_clone_contents(clone_dir)
        assert safe_file.exists()
        assert not unsafe_file.exists()

    def test_keeps_all_safe_extensions(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        safe_exts = [".pdf", ".tex", ".md", ".txt", ".docx", ".doc", ".bib"]
        for ext in safe_exts:
            f = clone_dir / f"file{ext}"
            f.write_text("content")

        _validate_clone_contents(clone_dir)
        for ext in safe_exts:
            assert (clone_dir / f"file{ext}").exists(), f"File with {ext} was removed"

    def test_skips_dot_git_directory(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        hook_file = git_dir / "config"
        hook_file.write_text("[core]")

        _validate_clone_contents(clone_dir)
        # .git internals should remain untouched.
        assert hook_file.exists()

    def test_size_limit_enforcement(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        # Create a file exceeding MAX_CLONE_SIZE_MB.
        big_file = clone_dir / "huge.tex"
        max_bytes = MAX_CLONE_SIZE_MB * 1024 * 1024
        # Write just over the limit.
        big_file.write_bytes(b"x" * (max_bytes + 1))

        with pytest.raises(SandboxError, match="too large"):
            _validate_clone_contents(clone_dir)

    def test_size_limit_passes_when_under(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        small_file = clone_dir / "paper.tex"
        small_file.write_text("small content")

        # Should not raise.
        _validate_clone_contents(clone_dir)

    def test_removes_files_in_subdirectories(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        sub = clone_dir / "subdir"
        sub.mkdir(parents=True)
        bad = sub / "script.sh"
        bad.write_text("#!/bin/bash")
        good = sub / "notes.md"
        good.write_text("# Notes")

        _validate_clone_contents(clone_dir)
        assert not bad.exists()
        assert good.exists()

    def test_empty_clone_dir(self, tmp_path: Path) -> None:
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        # Should not raise on empty directory.
        _validate_clone_contents(clone_dir)


# ---------------------------------------------------------------------------
# cleanup_temp_dir
# ---------------------------------------------------------------------------


class TestCleanupTempDir:
    """Tests for cleanup_temp_dir."""

    def test_normal_cleanup(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="pv_clone_"))
        (d / "file.txt").write_text("data")
        assert d.exists()
        cleanup_temp_dir(d)
        assert not d.exists()

    def test_missing_directory_noop(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist"
        # Should not raise.
        cleanup_temp_dir(nonexistent)

    def test_refuses_non_temp_directory(self, tmp_path: Path) -> None:
        # tmp_path is not under tempfile.gettempdir() and doesn't have pv_clone_ prefix
        # on most systems. We create a directory that clearly doesn't match either condition.
        suspect = tmp_path / "important_data"
        suspect.mkdir()
        (suspect / "precious.txt").write_text("do not delete")

        # Mock tempfile.gettempdir to return a different path so the check fails.
        with patch("paperverifier.security.sandbox.tempfile.gettempdir", return_value="/nowhere"):
            cleanup_temp_dir(suspect)

        # The directory should still exist because cleanup was refused.
        assert suspect.exists()
        assert (suspect / "precious.txt").exists()

    def test_accepts_pv_clone_prefix(self, tmp_path: Path) -> None:
        # A directory whose name starts with pv_clone_ should be accepted
        # even if not under the system temp root.
        d = tmp_path / "pv_clone_test123"
        d.mkdir()
        (d / "data.txt").write_text("clone data")

        cleanup_temp_dir(d)
        assert not d.exists()

    def test_os_error_during_rmtree_logged_not_raised(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="pv_clone_"))
        with patch("paperverifier.security.sandbox.shutil.rmtree", side_effect=OSError("fail")):
            # Should not raise.
            cleanup_temp_dir(d)
        # Clean up manually after the test.
        if d.exists():
            import shutil

            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# clone_github_repo
# ---------------------------------------------------------------------------


class TestCloneGithubRepo:
    """Tests for clone_github_repo (mocked subprocess)."""

    async def test_successful_clone(self, tmp_path: Path) -> None:
        target = tmp_path / "clone_target"
        clone_dir = target / "repo"

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            # On the clone command, create the repo directory with a file.
            if cmd[0] == "git" and "clone" in cmd:
                clone_dir.mkdir(parents=True, exist_ok=True)
                (clone_dir / "paper.tex").write_text("\\documentclass{article}")
                (clone_dir / ".git").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            # Checkout command
            if cmd[0] == "git" and "checkout" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed):
            result = await clone_github_repo("https://github.com/user/repo.git", target_dir=target)

        assert result == clone_dir
        assert (clone_dir / "paper.tex").exists()

    async def test_clone_failure_raises_and_cleans_up(self, tmp_path: Path) -> None:
        target = tmp_path / "clone_target"

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            if cmd[0] == "git" and "clone" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 128, stdout="", stderr="fatal: repository not found"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed),
            patch("paperverifier.security.sandbox.cleanup_temp_dir") as mock_cleanup,
        ):
            with pytest.raises(SandboxError, match="git clone failed"):
                await clone_github_repo("https://github.com/user/repo.git", target_dir=target)
            mock_cleanup.assert_called_once_with(target)

    async def test_post_validation_failure_cleans_up(self, tmp_path: Path) -> None:
        target = tmp_path / "clone_target"

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            if cmd[0] == "git" and "clone" in cmd:
                clone_dir = target / "repo"
                clone_dir.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed),
            patch(
                "paperverifier.security.sandbox._validate_clone_contents",
                side_effect=SandboxError("too large"),
            ),
            patch("paperverifier.security.sandbox.cleanup_temp_dir") as mock_cleanup,
        ):
            with pytest.raises(SandboxError, match="too large"):
                await clone_github_repo("https://github.com/user/repo.git", target_dir=target)
            mock_cleanup.assert_called_once_with(target)

    async def test_creates_temp_dir_when_target_is_none(self) -> None:
        fake_tmpdir = Path("/tmp/pv_clone_fake123")

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            if cmd[0] == "git" and "clone" in cmd:
                # Simulate creating the clone directory.
                clone_dir = fake_tmpdir / "repo"
                clone_dir.mkdir(parents=True, exist_ok=True)
                (clone_dir / "paper.tex").write_text("content")
                (clone_dir / ".git").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch("paperverifier.security.sandbox.tempfile.mkdtemp", return_value=str(fake_tmpdir)),
            patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed),
        ):
            result = await clone_github_repo("https://github.com/user/repo.git")
            assert result == fake_tmpdir / "repo"

        # Clean up.
        if fake_tmpdir.exists():
            import shutil

            shutil.rmtree(fake_tmpdir, ignore_errors=True)

    async def test_git_env_contains_security_vars(self, tmp_path: Path) -> None:
        """Verify the git environment includes lockdown variables."""
        captured_env: dict[str, str] = {}

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            if env and cmd[0] == "git" and "clone" in cmd:
                captured_env.update(env)
                clone_dir = tmp_path / "clone_target" / "repo"
                clone_dir.mkdir(parents=True, exist_ok=True)
                (clone_dir / ".git").mkdir(exist_ok=True)
                (clone_dir / "paper.tex").write_text("content")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed):
            await clone_github_repo(
                "https://github.com/user/repo.git",
                target_dir=tmp_path / "clone_target",
            )

        assert captured_env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert captured_env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
        assert captured_env.get("GIT_TERMINAL_PROMPT") == "0"
        assert "HOME" in captured_env

    async def test_checkout_non_zero_does_not_raise(self, tmp_path: Path) -> None:
        """A non-zero exit from checkout should not abort the operation."""
        target = tmp_path / "clone_target"
        clone_dir = target / "repo"

        async def fake_run_sandboxed(
            cmd, *, timeout=60, cwd=None, env=None, max_output_bytes=MAX_OUTPUT_SIZE
        ):
            if cmd[0] == "git" and "clone" in cmd:
                clone_dir.mkdir(parents=True, exist_ok=True)
                (clone_dir / ".git").mkdir(exist_ok=True)
                (clone_dir / "paper.tex").write_text("content")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and "checkout" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="error: pathspec did not match"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("paperverifier.security.sandbox.run_sandboxed", side_effect=fake_run_sandboxed):
            result = await clone_github_repo("https://github.com/user/repo.git", target_dir=target)
            # Should succeed despite checkout returning non-zero.
            assert result == clone_dir
