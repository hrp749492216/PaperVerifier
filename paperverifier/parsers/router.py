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
from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# Parser plugin registry
# ---------------------------------------------------------------------------

# Maps parser name -> factory callable that returns a BaseParser instance.
# Uses deferred imports so dependencies are loaded on demand.
_PARSER_REGISTRY: dict[str, Callable[[], BaseParser]] = {}


def register_parser(name: str, factory: Callable[[], BaseParser]) -> None:
    """Register a parser factory under *name*.

    This allows new parsers (e.g. ``.epub``) to be added without modifying
    the router's ``_instantiate`` method (Open/Closed Principle)::

        from paperverifier.parsers.router import register_parser
        register_parser("epub", lambda: EPUBParser())
    """
    _PARSER_REGISTRY[name] = factory


def _register_builtin_parsers() -> None:
    """Register the built-in parsers with deferred imports."""
    def _pdf() -> BaseParser:
        from paperverifier.parsers.pdf_parser import PDFParser
        return PDFParser()

    def _docx() -> BaseParser:
        from paperverifier.parsers.docx_parser import DOCXParser
        return DOCXParser()

    def _markdown() -> BaseParser:
        from paperverifier.parsers.markdown_parser import MarkdownParser
        return MarkdownParser()

    def _latex() -> BaseParser:
        from paperverifier.parsers.latex_parser import LaTeXParser
        return LaTeXParser()

    def _github() -> BaseParser:
        from paperverifier.parsers.github_parser import GitHubParser
        return GitHubParser()

    def _url() -> BaseParser:
        from paperverifier.parsers.url_parser import URLParser
        return URLParser()

    def _text() -> BaseParser:
        from paperverifier.parsers.text_parser import TextParser
        return TextParser()

    for name, factory in [
        ("pdf", _pdf), ("docx", _docx), ("markdown", _markdown),
        ("latex", _latex), ("github", _github), ("url", _url),
        ("text", _text),
    ]:
        register_parser(name, factory)


_register_builtin_parsers()


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
            except zipfile.BadZipFile:
                pass  # Not a valid ZIP.
            except Exception:
                logger.debug("magic_bytes_zip_check_failed", exc_info=True)
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
            logger.debug("magic_bytes_text_check_failed", exc_info=True)

        return None

    # ------------------------------------------------------------------
    # Parser instantiation (plugin registry)
    # ------------------------------------------------------------------

    @staticmethod
    def _instantiate(parser_name: str) -> BaseParser:
        """Create a parser instance from the :data:`_PARSER_REGISTRY`.

        Uses deferred imports (via factory callables) so that parser
        dependencies are only loaded when actually needed.  New parsers
        can be added with :func:`register_parser` without modifying this
        method (Open/Closed Principle).
        """
        factory = _PARSER_REGISTRY.get(parser_name)
        if factory is None:
            factory = _PARSER_REGISTRY["text"]  # Fallback
        return factory()


def _truncate_source(source: str, max_len: int = 100) -> str:
    """Truncate a source string for logging."""
    if len(source) <= max_len:
        return source
    return source[:max_len] + "..."
