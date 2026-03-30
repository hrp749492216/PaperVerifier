"""Input router for automatic document format detection.

Inspects the source string (or provided content bytes) and routes to
the correct parser.  Detection order:

1. GitHub URL (``https://github.com/owner/repo``)
2. HTTP/HTTPS URL
3. File extension (``.pdf``, ``.docx``, ``.md``, ``.tex``, ``.txt``)
4. Magic bytes (if ``content`` is provided)
5. Fallback to plain text parser
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import structlog

from paperverifier.models.document import ParsedDocument
from paperverifier.parsers.base import BaseParser

logger = structlog.get_logger(__name__)

# GitHub URL pattern (loose match -- full validation is in GitHubParser).
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/[\w\-\.]+/[\w\-\.]+",
)

# Extension to parser class mapping.
_EXTENSION_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".md": "markdown",
    ".markdown": "markdown",
    ".tex": "latex",
    ".txt": "text",
    ".text": "text",
}


class InputRouter:
    """Route document inputs to the correct parser.

    Examines the source string and optional content bytes to determine
    the appropriate parser, then delegates parsing.

    Usage::

        router = InputRouter()
        doc = await router.parse("paper.pdf")
        doc = await router.parse("https://arxiv.org/abs/2301.12345")
        doc = await router.parse("https://github.com/user/repo")
    """

    async def parse(
        self,
        source: str,
        content: bytes | None = None,
        **kwargs: object,
    ) -> ParsedDocument:
        """Route input to the correct parser and return a parsed document.

        Detection order:

        1. If *source* matches a GitHub URL pattern -> :class:`GitHubParser`.
        2. If *source* starts with ``http://`` or ``https://`` ->
           :class:`URLParser`.
        3. Detect by file extension (``.pdf``, ``.docx``, ``.md``,
           ``.tex``, ``.txt``).
        4. If *content* is provided, detect by magic bytes.
        5. Fall back to :class:`TextParser`.

        Args:
            source: A file path, URL, or other source identifier.
            content: Optional raw file content bytes.  When provided,
                the parser receives these bytes rather than reading
                from the file path.
            **kwargs: Passed through to the delegate parser.

        Returns:
            A fully populated :class:`ParsedDocument`.
        """
        source = source.strip()
        parser_name = self._detect(source, content)
        parser = self._instantiate(parser_name)

        logger.info(
            "input_routed",
            source=_truncate_source(source),
            parser=type(parser).__name__,
        )

        # If raw content was provided, pass it directly.
        if content is not None:
            return await parser.parse(content, **kwargs)
        return await parser.parse(source, **kwargs)

    # ------------------------------------------------------------------
    # Detection logic
    # ------------------------------------------------------------------

    def _detect(self, source: str, content: bytes | None) -> str:
        """Determine which parser to use.

        Returns a string key used by :meth:`_instantiate`.
        """
        # 1. GitHub URL?
        if _GITHUB_URL_RE.match(source):
            logger.debug("detected_github_url", source=_truncate_source(source))
            return "github"

        # 2. HTTP(S) URL?
        if source.lower().startswith(("http://", "https://")):
            logger.debug("detected_url", source=_truncate_source(source))
            return "url"

        # 3. File extension?
        ext = Path(source).suffix.lower()
        if ext in _EXTENSION_MAP:
            parser_name = _EXTENSION_MAP[ext]
            logger.debug(
                "detected_extension",
                extension=ext,
                parser=parser_name,
            )
            return parser_name

        # 4. Magic bytes (if content provided)?
        if content is not None:
            magic_parser = self._detect_by_magic(content)
            if magic_parser:
                logger.debug("detected_magic_bytes", parser=magic_parser)
                return magic_parser

        # 5. Fallback: text parser.
        logger.debug("fallback_text_parser", source=_truncate_source(source))
        return "text"

    @staticmethod
    def _detect_by_magic(content: bytes) -> str | None:
        """Detect file type from magic bytes."""
        if not content:
            return None

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
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "docx"  # Legacy .doc (OLE2).

        # Check if it looks like LaTeX.
        try:
            text_sample = content[:2048].decode("utf-8", errors="replace")
            if r"\documentclass" in text_sample or r"\begin{document}" in text_sample:
                return "latex"
            if text_sample.strip().startswith("#"):
                return "markdown"
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Parser instantiation
    # ------------------------------------------------------------------

    @staticmethod
    def _instantiate(parser_name: str) -> BaseParser:
        """Create a parser instance from a type name.

        Uses deferred imports to avoid loading all parser dependencies
        when only one is needed.
        """
        if parser_name == "pdf":
            from paperverifier.parsers.pdf_parser import PDFParser

            return PDFParser()
        elif parser_name == "docx":
            from paperverifier.parsers.docx_parser import DOCXParser

            return DOCXParser()
        elif parser_name == "markdown":
            from paperverifier.parsers.markdown_parser import MarkdownParser

            return MarkdownParser()
        elif parser_name == "latex":
            from paperverifier.parsers.latex_parser import LaTeXParser

            return LaTeXParser()
        elif parser_name == "github":
            from paperverifier.parsers.github_parser import GitHubParser

            return GitHubParser()
        elif parser_name == "url":
            from paperverifier.parsers.url_parser import URLParser

            return URLParser()
        else:
            from paperverifier.parsers.text_parser import TextParser

            return TextParser()


def _truncate_source(source: str, max_len: int = 100) -> str:
    """Truncate a source string for logging."""
    if len(source) <= max_len:
        return source
    return source[:max_len] + "..."
