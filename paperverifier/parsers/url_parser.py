"""URL document parser.

Downloads a document from a URL (with SSRF protection via
:func:`~paperverifier.security.input_validator.validate_url`) and
delegates to the appropriate parser based on content type.

Includes special handling for arXiv URLs (converting abstract pages
to direct PDF download links).

DNS-pinning: After validating a URL, the resolved IP is used for the
actual HTTP connection to prevent DNS-rebinding / TOCTOU attacks.
"""

from __future__ import annotations

import re
import socket
import urllib.parse
from typing import Any

import structlog

from paperverifier.models.document import ParsedDocument
from paperverifier.parsers.base import BaseParser
from paperverifier.security.input_validator import (
    InputValidationError,
    resolve_and_validate_url,
)

logger = structlog.get_logger(__name__)

# Maximum download size (100 MB).
_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# Download timeout in seconds.
_DOWNLOAD_TIMEOUT = 60

# arXiv URL patterns.
_ARXIV_ABS_RE = re.compile(
    r"https?://arxiv\.org/abs/([\d.]+(?:v\d+)?)",
)
_ARXIV_PDF_RE = re.compile(
    r"https?://arxiv\.org/pdf/([\d.]+(?:v\d+)?)",
)

# Content-type to parser mapping.
_CONTENT_TYPE_MAP = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/markdown": "markdown",
    "text/x-tex": "latex",
    "application/x-tex": "latex",
    "text/x-latex": "latex",
}


class URLParser(BaseParser):
    """Parse a document from a URL.

    Validates the URL for SSRF protection, downloads the content with
    size limits and timeouts, detects the content type, and delegates
    to the appropriate parser.

    Special handling:
    - **arXiv**: Converts ``/abs/`` URLs to ``/pdf/`` URLs.
    - **HTML**: If the page is an arXiv abstract page, extracts the
      PDF link.  Other HTML pages are rejected (use a more specific
      URL to a downloadable document).
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Download and parse a document from a URL.

        Args:
            source: An HTTPS URL pointing to a downloadable document.
            **kwargs: Passed through to the delegate parser.

        Returns:
            A fully populated :class:`ParsedDocument`.

        Raises:
            InputValidationError: If the URL fails validation or the
                content type is unsupported.
        """
        if not isinstance(source, str):
            raise InputValidationError(
                "URL parser requires a URL string as source."
            )

        url = source.strip()

        # Handle arXiv URLs: convert /abs/ to /pdf/.
        url = self._normalize_arxiv_url(url)

        # Validate URL for SSRF protection and pin the resolved IP
        # to prevent DNS-rebinding attacks (TOCTOU).
        validated_url, resolved_ip = resolve_and_validate_url(url)

        # Download the content using the pinned IP.
        content, content_type, final_url = await self._download(
            validated_url, resolved_ip
        )

        # Determine the parser to use.
        parser_type = self._detect_parser_type(content_type, final_url, content)

        if parser_type is None:
            raise InputValidationError(
                f"Unsupported content type '{content_type}' from URL: {url}. "
                f"Please provide a direct link to a PDF, LaTeX, Markdown, "
                f"or plain text file."
            )

        # Delegate to the appropriate parser.
        parser = self._get_parser(parser_type)
        result = await parser.parse(content, **kwargs)

        # Update source metadata.
        result.source_type = "url"
        result.source_path = validated_url
        result.metadata["download_url"] = final_url
        result.metadata["content_type"] = content_type

        return result

    # ------------------------------------------------------------------
    # arXiv URL handling
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_arxiv_url(url: str) -> str:
        """Convert arXiv abstract URLs to PDF download URLs.

        ``https://arxiv.org/abs/2301.12345`` becomes
        ``https://arxiv.org/pdf/2301.12345.pdf``.
        """
        match = _ARXIV_ABS_RE.match(url)
        if match:
            arxiv_id = match.group(1)
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            logger.info(
                "arxiv_url_converted",
                original=url,
                pdf_url=pdf_url,
            )
            return pdf_url
        return url

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def _download(
        self, url: str, resolved_ip: str
    ) -> tuple[bytes, str, str]:
        """Download content from a URL with size limits and timeout.

        Args:
            url: The validated URL to download.
            resolved_ip: The DNS-pinned IP address to connect to,
                preventing DNS-rebinding attacks.

        Returns:
            A tuple of ``(content_bytes, content_type, final_url)``.
            The ``final_url`` may differ from the input after redirects.
        """
        try:
            import aiohttp  # type: ignore[import-untyped]
        except ImportError:
            # Fall back to httpx if aiohttp is not available.
            return await self._download_httpx(url, resolved_ip)

        try:
            timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)

            # Disable auto-redirects and re-validate each redirect
            # target to prevent SSRF bypass via redirect (CRIT-2).
            # Each hop gets a fresh session with a pinned-DNS connector
            # so the connection goes to the validated IP, not a
            # potentially rebinding hostname.
            current_url = url
            current_ip = resolved_ip
            max_redirects = 5
            for _ in range(max_redirects + 1):
                connector = self._make_pinned_connector(
                    current_url, current_ip
                )
                async with aiohttp.ClientSession(
                    timeout=timeout, connector=connector
                ) as session:
                    async with session.get(
                        current_url, allow_redirects=False
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise InputValidationError(
                                    f"Redirect with no Location header from: {current_url}"
                                )
                            # Resolve relative redirects.
                            location = urllib.parse.urljoin(
                                current_url, location
                            )
                            # Re-validate the redirect target for SSRF
                            # and pin the new resolved IP.
                            current_url, current_ip = (
                                resolve_and_validate_url(location)
                            )
                            continue

                        if response.status != 200:
                            raise InputValidationError(
                                f"Failed to download URL (HTTP {response.status}): {current_url}"
                            )

                        content_type = response.content_type or ""
                        final_url = str(response.url)

                        # Check Content-Length before downloading.
                        content_length = response.content_length
                        if content_length and content_length > _MAX_DOWNLOAD_SIZE:
                            raise InputValidationError(
                                f"File too large: {content_length:,} bytes "
                                f"(max {_MAX_DOWNLOAD_SIZE:,})."
                            )

                        # Read with size limit.
                        chunks: list[bytes] = []
                        total_size = 0
                        async for chunk in response.content.iter_chunked(
                            8192
                        ):
                            total_size += len(chunk)
                            if total_size > _MAX_DOWNLOAD_SIZE:
                                raise InputValidationError(
                                    f"Download exceeded size limit of "
                                    f"{_MAX_DOWNLOAD_SIZE:,} bytes."
                                )
                            chunks.append(chunk)

                        content = b"".join(chunks)
                        logger.info(
                            "url_downloaded",
                            url=url,
                            size=len(content),
                            content_type=content_type,
                        )
                        return content, content_type, final_url
            else:
                raise InputValidationError(
                    f"Too many redirects (>{max_redirects}) from: {url}"
                )

        except InputValidationError:
            raise
        except Exception as exc:
            raise InputValidationError(
                f"Failed to download URL: {url} -- {exc}"
            ) from exc

    async def _download_httpx(
        self, url: str, resolved_ip: str
    ) -> tuple[bytes, str, str]:
        """Fallback download using httpx with DNS pinning."""
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "Either aiohttp or httpx is required for URL downloads. "
                "Install with: pip install aiohttp  or  pip install httpx"
            )

        try:
            transport = self._make_pinned_httpx_transport(url, resolved_ip)
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=_DOWNLOAD_TIMEOUT,
                transport=transport,
            ) as client:
                # Manually follow redirects with SSRF re-validation (CRIT-2).
                # Use a single request chain -- no duplicate final fetch
                # (Codex-1 fix #6).
                current_url = url
                current_ip = resolved_ip
                response = await client.get(current_url)
                for _ in range(max_redirects := 5):
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise InputValidationError(
                                f"Redirect with no Location header from: {current_url}"
                            )
                        location = urllib.parse.urljoin(current_url, location)
                        current_url, current_ip = resolve_and_validate_url(
                            location
                        )
                        # Create a fresh client with the new pinned IP.
                        transport = self._make_pinned_httpx_transport(
                            current_url, current_ip
                        )
                        async with httpx.AsyncClient(
                            follow_redirects=False,
                            timeout=_DOWNLOAD_TIMEOUT,
                            transport=transport,
                        ) as redirect_client:
                            response = await redirect_client.get(current_url)
                        continue
                    break
                else:
                    raise InputValidationError(
                        f"Too many redirects (>{max_redirects}) from: {url}"
                    )

                if response.status_code != 200:
                    raise InputValidationError(
                        f"Failed to download URL (HTTP {response.status_code}): {url}"
                    )

                # Check Content-Length header before buffering body.
                cl = response.headers.get("content-length")
                if cl:
                    try:
                        cl_int = int(cl)
                    except ValueError:
                        cl_int = 0
                    if cl_int > _MAX_DOWNLOAD_SIZE:
                        raise InputValidationError(
                            f"File too large: {cl_int:,} bytes "
                            f"(max {_MAX_DOWNLOAD_SIZE:,})."
                        )

                content_type = response.headers.get("content-type", "")
                content_type = content_type.split(";")[0].strip()
                final_url = str(response.url)

                content = response.content
                if len(content) > _MAX_DOWNLOAD_SIZE:
                    raise InputValidationError(
                        f"Download exceeded size limit of "
                        f"{_MAX_DOWNLOAD_SIZE:,} bytes."
                    )

                logger.info(
                    "url_downloaded",
                    url=url,
                    size=len(content),
                    content_type=content_type,
                )
                return content, content_type, final_url

        except InputValidationError:
            raise
        except Exception as exc:
            raise InputValidationError(
                f"Failed to download URL: {url} -- {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # DNS-pinning helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_pinned_connector(
        url: str, resolved_ip: str
    ) -> Any:
        """Create an aiohttp ``TCPConnector`` that pins DNS resolution.

        The connector uses a custom resolver that always returns the
        pre-validated *resolved_ip* for the hostname in *url*, preventing
        DNS-rebinding attacks between validation and connection time.
        """
        import aiohttp  # type: ignore[import-untyped]
        from aiohttp.abc import AbstractResolver  # type: ignore[import-untyped]

        parsed = urllib.parse.urlparse(url)
        pinned_host = parsed.hostname

        class _PinnedResolver(AbstractResolver):
            """Resolver that returns the pre-validated IP."""

            async def resolve(
                self,
                host: str,
                port: int = 0,
                family: int = socket.AF_INET,
            ) -> list[dict[str, Any]]:
                if host == pinned_host:
                    return [
                        {
                            "hostname": host,
                            "host": resolved_ip,
                            "port": port,
                            "family": family,
                            "proto": 0,
                            "flags": socket.AI_NUMERICHOST,
                        }
                    ]
                # For any other host (should not happen), fall back to
                # standard resolution -- but this path is unexpected.
                raise OSError(
                    f"DNS resolution blocked: unexpected host '{host}' "
                    f"(expected '{pinned_host}')"
                )

            async def close(self) -> None:
                pass

        return aiohttp.TCPConnector(resolver=_PinnedResolver())

    @staticmethod
    def _make_pinned_httpx_transport(
        url: str, resolved_ip: str
    ) -> Any:
        """Create an httpx transport that pins DNS to the validated IP.

        Uses httpx's ``transport`` parameter with a custom map so the
        hostname resolves to the pre-validated IP, preventing DNS-rebinding.
        """
        import httpx  # type: ignore[import-untyped]

        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # httpx's AsyncHTTPTransport accepts a local_address mapping
        # through the underlying httpcore backend.  The simplest robust
        # approach is to use the `uds` / socket-level pinning via a
        # custom transport wrapper that overrides connection creation.

        class _PinnedTransport(httpx.AsyncHTTPTransport):
            """Transport that forces connections to the pinned IP."""

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                # Rewrite the URL's host to the pinned IP for the
                # actual TCP connection, preserving the original Host
                # header for TLS SNI and virtual hosting.
                if request.url.host == hostname:
                    pinned_url = request.url.copy_with(
                        host=resolved_ip,
                        port=port,
                    )
                    request = httpx.Request(
                        method=request.method,
                        url=pinned_url,
                        headers=request.headers,
                        content=request.content,
                        extensions=request.extensions,
                    )
                    # Ensure the Host header reflects the original
                    # hostname (not the IP) for correct TLS/SNI.
                    request.headers["host"] = hostname
                return await super().handle_async_request(request)

        return _PinnedTransport(verify=True)

    # ------------------------------------------------------------------
    # Content type detection
    # ------------------------------------------------------------------

    def _detect_parser_type(
        self,
        content_type: str,
        url: str,
        content: bytes,
    ) -> str | None:
        """Determine which parser to use based on content type, URL, and
        magic bytes.

        Returns:
            A parser type string (``'pdf'``, ``'latex'``, ``'markdown'``,
            ``'text'``) or ``None`` if unsupported.
        """
        # Normalise content type (strip charset, etc.).
        ct = content_type.split(";")[0].strip().lower()

        # Check content-type map first.
        parser_type = _CONTENT_TYPE_MAP.get(ct)
        if parser_type:
            return parser_type

        # HTML handling: check for arXiv-like pages.
        if ct in ("text/html", "application/xhtml+xml"):
            # We don't parse HTML pages -- only arXiv should have been
            # caught by _normalize_arxiv_url.  But check content for
            # a PDF link just in case.
            logger.warning(
                "html_content_received",
                url=url,
                hint="Expected a direct file download, got HTML.",
            )
            return None

        # Fallback: detect by URL extension.
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        if path.endswith(".pdf"):
            return "pdf"
        if path.endswith(".tex"):
            return "latex"
        if path.endswith(".md"):
            return "markdown"
        if path.endswith((".txt", ".text")):
            return "text"
        if path.endswith(".docx"):
            return "docx"

        # Fallback: detect by magic bytes.
        if content[:4] == b"%PDF":
            return "pdf"
        if content[:2] == b"PK":
            # Verify it's actually a DOCX (not just any ZIP file).
            import zipfile, io
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    if "word/document.xml" in zf.namelist():
                        return "docx"
            except (zipfile.BadZipFile, Exception):
                pass  # Not a valid ZIP or not a DOCX.

        # Last resort: if it looks like text, use text parser.
        if ct.startswith("text/") or self._looks_like_text(content[:1024]):
            return "text"

        return None

    @staticmethod
    def _looks_like_text(sample: bytes) -> bool:
        """Heuristic check: does the byte sample look like text?"""
        if not sample:
            return False
        try:
            sample.decode("utf-8")
            return True
        except UnicodeDecodeError:
            # Check if mostly printable ASCII.
            printable = sum(
                1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13)
            )
            return printable / len(sample) > 0.85

    # ------------------------------------------------------------------
    # Parser instantiation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_parser(parser_type: str) -> BaseParser:
        """Instantiate the appropriate parser for the detected type."""
        if parser_type == "pdf":
            from paperverifier.parsers.pdf_parser import PDFParser

            return PDFParser()
        elif parser_type == "latex":
            from paperverifier.parsers.latex_parser import LaTeXParser

            return LaTeXParser()
        elif parser_type == "markdown":
            from paperverifier.parsers.markdown_parser import MarkdownParser

            return MarkdownParser()
        elif parser_type == "docx":
            from paperverifier.parsers.docx_parser import DOCXParser

            return DOCXParser()
        else:
            from paperverifier.parsers.text_parser import TextParser

            return TextParser()
