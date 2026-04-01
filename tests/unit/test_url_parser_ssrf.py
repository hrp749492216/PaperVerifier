"""Tests for SSRF protection in the URL parser download path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperverifier.security.input_validator import InputValidationError


class _AsyncContextManager:
    """Helper that wraps a value as an async context manager."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


class TestURLParserSSRFRedirect:
    """Tests that URL parser re-validates redirects with DNS pinning."""

    @pytest.mark.asyncio
    async def test_parse_uses_resolve_and_validate(self) -> None:
        """The parse() entry point must call resolve_and_validate_url."""
        from paperverifier.parsers.url_parser import URLParser

        parser = URLParser()

        with patch("paperverifier.parsers.url_parser.resolve_and_validate_url") as mock_resolve:
            mock_resolve.return_value = ("https://example.com/paper.pdf", "93.184.216.34")

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.content_type = "application/pdf"
            mock_response.url = "https://example.com/paper.pdf"
            mock_response.content_length = 100

            async def _mock_iter(size):
                yield b"%PDF-1.4 test content"

            mock_response.content = MagicMock()
            mock_response.content.iter_chunked = _mock_iter

            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=_AsyncContextManager(mock_response))

            with (
                patch("aiohttp.ClientTimeout", return_value=MagicMock()),
                patch("aiohttp.ClientSession", return_value=_AsyncContextManager(mock_session)),
            ):
                # Mock the delegate parser so we don't need a real PDF parser
                mock_delegate = AsyncMock()
                mock_parsed_doc = MagicMock()
                mock_parsed_doc.metadata = {}
                mock_delegate.parse.return_value = mock_parsed_doc
                with patch.object(parser, "_get_parser", return_value=mock_delegate):
                    await parser.parse("https://example.com/paper.pdf")

            mock_resolve.assert_called_once_with("https://example.com/paper.pdf")

    @pytest.mark.asyncio
    async def test_redirect_revalidation_uses_resolve(self) -> None:
        """Redirect targets must also go through resolve_and_validate_url."""
        from paperverifier.parsers.url_parser import URLParser

        parser = URLParser()

        call_count = 0

        def side_effect(url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (url, "93.184.216.34")
            # Second call is the redirect target - block it
            raise InputValidationError("Access to private/reserved IP range is blocked")

        with patch(
            "paperverifier.parsers.url_parser.resolve_and_validate_url",
            side_effect=side_effect,
        ):
            mock_response = MagicMock()
            mock_response.status = 302
            mock_response.headers = {"Location": "https://evil.internal/steal"}

            mock_session = MagicMock()
            mock_session.get = MagicMock(return_value=_AsyncContextManager(mock_response))

            with (
                patch("aiohttp.ClientTimeout", return_value=MagicMock()),
                patch("aiohttp.ClientSession", return_value=_AsyncContextManager(mock_session)),
            ):
                with pytest.raises(InputValidationError, match="private|blocked|reserved"):
                    await parser._download("https://example.com/paper.pdf", "93.184.216.34")
