"""Unit tests for paperverifier.security.input_validator."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from paperverifier.security.input_validator import (
    InputValidationError,
    sanitize_filename,
    validate_uploaded_file,
    validate_url,
)


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------


class TestValidateURL:
    """Tests for SSRF-safe URL validation."""

    def test_valid_https_url(self) -> None:
        """A valid HTTPS URL pointing to a public IP should pass."""
        url = "https://example.com/paper.pdf"
        # Mock DNS resolution to return a public IP
        with patch("paperverifier.security.input_validator.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 443)),
            ]
            result = validate_url(url)
            assert result == url

    def test_rejects_http_scheme(self) -> None:
        """HTTP (non-TLS) URLs must be rejected."""
        with pytest.raises(InputValidationError, match="scheme.*not allowed"):
            validate_url("http://example.com/paper.pdf")

    def test_rejects_localhost_127(self) -> None:
        """127.0.0.1 (loopback) must be blocked for SSRF protection."""
        with pytest.raises(InputValidationError, match="private|blocked|reserved"):
            validate_url("https://127.0.0.1/admin")

    def test_rejects_private_10_network(self) -> None:
        """10.0.0.1 (RFC 1918 private) must be blocked."""
        with pytest.raises(InputValidationError, match="private|blocked|reserved"):
            validate_url("https://10.0.0.1/internal")

    def test_rejects_ipv4_mapped_ipv6(self) -> None:
        """IPv4-mapped IPv6 addresses like ::ffff:169.254.169.254 must be blocked."""
        with pytest.raises(InputValidationError, match="private|blocked|reserved"):
            validate_url("https://[::ffff:169.254.169.254]/metadata")

    def test_rejects_empty_url(self) -> None:
        """Empty string must be rejected."""
        with pytest.raises(InputValidationError, match="non-empty"):
            validate_url("")


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """Tests for filename sanitization and path traversal prevention."""

    def test_strips_path_traversal(self) -> None:
        """Path traversal components like ../../ must be stripped."""
        result = sanitize_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert "passwd" in result

    def test_strips_backslash_traversal(self) -> None:
        """Windows-style path traversal must also be stripped."""
        result = sanitize_filename("..\\..\\windows\\system32\\config")
        assert ".." not in result
        assert "\\" not in result

    def test_preserves_extension(self) -> None:
        """The file extension should be preserved after sanitization."""
        result = sanitize_filename("my-paper.pdf")
        assert result.endswith(".pdf")

    def test_adds_uuid_prefix(self) -> None:
        """Sanitized filenames should have a UUID prefix for uniqueness."""
        result = sanitize_filename("test.pdf")
        # UUID prefix is 8 chars + underscore
        assert "_" in result
        prefix = result.split("_")[0]
        assert len(prefix) == 8

    def test_rejects_empty_filename(self) -> None:
        """Empty filenames must be rejected."""
        with pytest.raises(InputValidationError):
            sanitize_filename("")


# ---------------------------------------------------------------------------
# validate_uploaded_file
# ---------------------------------------------------------------------------


class TestValidateUploadedFile:
    """Tests for uploaded file validation."""

    def test_rejects_empty_file(self) -> None:
        """An empty file (zero bytes) must be rejected."""
        with pytest.raises(InputValidationError, match="empty"):
            validate_uploaded_file("test.txt", b"")

    def test_rejects_oversized_file(self) -> None:
        """Files exceeding max_size must be rejected."""
        content = b"x" * 1001
        with pytest.raises(InputValidationError, match="too large"):
            validate_uploaded_file("test.txt", content, max_size=1000)

    def test_accepts_valid_text_file(self) -> None:
        """A valid .txt file within size limits should pass."""
        content = b"This is a valid text file for testing."
        safe_name, returned_content = validate_uploaded_file("paper.txt", content)
        assert safe_name.endswith(".txt")
        assert returned_content == content

    def test_rejects_disallowed_extension(self) -> None:
        """Files with disallowed extensions (e.g. .exe) must be rejected."""
        with pytest.raises(InputValidationError, match="extension"):
            validate_uploaded_file("malware.exe", b"MZ\x90\x00")
