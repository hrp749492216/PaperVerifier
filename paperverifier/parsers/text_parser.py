"""Plain text document parser.

Uses heuristic section detection to find structure in unformatted text
files.  Supports:

- ALL CAPS lines as headers
- Numbered sections (``1. Introduction``, ``2. Methods``, etc.)
- Setext-style underlines (``===`` or ``---``)

Falls back to treating the entire text as a single section.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from paperverifier.models.document import (
    ParsedDocument,
    Section,
)
from paperverifier.parsers.base import BaseParser
from paperverifier.security.input_validator import (
    InputValidationError,
    validate_file_path,
)

logger = structlog.get_logger(__name__)

# Pattern: numbered headings like "1. Introduction" or "2 Methods".
_NUMBERED_HEADING_RE = re.compile(
    r"^\s*(\d+\.?\s+[A-Z][A-Za-z\s,&:]+)\s*$",
    re.MULTILINE,
)

# Pattern: ALL CAPS lines (3-60 characters, at least 2 words or known section
# names).
_CAPS_HEADING_RE = re.compile(
    r"^([A-Z][A-Z\s]{2,59})$",
    re.MULTILINE,
)

# Pattern: setext underlines (line of = or - at least 3 chars, preceded by
# text).
_SETEXT_RE = re.compile(
    r"^(.+)\n([=-]){3,}\s*$",
    re.MULTILINE,
)


class TextParser(BaseParser):
    """Parse plain text documents into :class:`ParsedDocument`.

    Applies multiple heuristics to identify section structure in
    unformatted text.  The first heuristic that finds at least two
    sections wins.  If no structure is detected the entire document
    is wrapped in a single section.
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Parse a plain text file from a path, bytes, or raw string.

        Args:
            source: File path (str), raw text string, or UTF-8 bytes.
            **kwargs: Optional ``allowed_dir`` (:class:`Path`).

        Returns:
            A fully populated :class:`ParsedDocument`.
        """
        source_path = ""

        if isinstance(source, bytes):
            text = source.decode("utf-8", errors="replace")
        elif isinstance(source, str) and _looks_like_path(source):
            path = Path(source)
            allowed_dir = kwargs.get("allowed_dir")
            if allowed_dir is not None:
                validate_file_path(path, Path(allowed_dir))  # type: ignore[arg-type]

            if not path.exists():
                raise InputValidationError(f"Text file not found: {source}")
            text = path.read_text(encoding="utf-8", errors="replace")
            source_path = str(path)
        else:
            # Raw text content.
            text = source

        if not text.strip():
            raise InputValidationError("Text document is empty.")

        # Try section-detection strategies in order.
        sections = (
            self._try_numbered_headings(text)
            or self._try_setext_headings(text)
            or self._try_caps_headings(text)
            or self._single_section(text)
        )

        references = self._extract_references_regex(text)
        fig_table_refs = self._detect_figure_table_refs(text)
        abstract = self._extract_abstract(text)
        title = self._guess_title(text, sections)

        return ParsedDocument(
            title=title,
            abstract=abstract,
            sections=sections,
            references=references,
            figures_tables=fig_table_refs,
            full_text=text,
            source_type="text",
            source_path=source_path,
        )

    # ------------------------------------------------------------------
    # Numbered headings (1. Introduction, 2. Methods, ...)
    # ------------------------------------------------------------------

    def _try_numbered_headings(self, text: str) -> list[Section] | None:
        """Detect numbered section headings."""
        matches = list(_NUMBERED_HEADING_RE.finditer(text))
        if len(matches) < 2:
            return None

        sections: list[Section] = []
        for idx, match in enumerate(matches):
            heading = match.group(1).strip()
            body_start = match.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[body_start:body_end].strip()

            section_id = f"sec-{idx + 1}"
            section = self._build_section(
                section_id=section_id,
                title=heading,
                text=body,
                level=1,
                start_char=match.start(),
            )
            sections.append(section)

        logger.debug("text_sections_detected", strategy="numbered", count=len(sections))
        return sections

    # ------------------------------------------------------------------
    # Setext headings (underlined with === or ---)
    # ------------------------------------------------------------------

    def _try_setext_headings(self, text: str) -> list[Section] | None:
        """Detect setext-style headings (underlined with = or -)."""
        matches = list(_SETEXT_RE.finditer(text))
        if len(matches) < 2:
            return None

        sections: list[Section] = []
        for idx, match in enumerate(matches):
            heading = match.group(1).strip()
            underline_char = match.group(2)
            level = 1 if underline_char == "=" else 2

            body_start = match.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[body_start:body_end].strip()

            section_id = f"sec-{idx + 1}"
            section = self._build_section(
                section_id=section_id,
                title=heading,
                text=body,
                level=level,
                start_char=match.start(),
            )
            sections.append(section)

        logger.debug("text_sections_detected", strategy="setext", count=len(sections))
        return sections

    # ------------------------------------------------------------------
    # ALL CAPS headings
    # ------------------------------------------------------------------

    def _try_caps_headings(self, text: str) -> list[Section] | None:
        """Detect ALL CAPS lines as section headings."""
        matches = list(_CAPS_HEADING_RE.finditer(text))
        # Filter: the heading must not be the entire document and should be
        # reasonably short.
        matches = [
            m
            for m in matches
            if len(m.group(1).strip()) < 60
            and len(m.group(1).split()) >= 1
            and m.group(1).strip() not in ("I", "II", "III", "IV", "V")
        ]
        if len(matches) < 2:
            return None

        sections: list[Section] = []
        for idx, match in enumerate(matches):
            heading = match.group(1).strip()
            body_start = match.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[body_start:body_end].strip()

            section_id = f"sec-{idx + 1}"
            section = self._build_section(
                section_id=section_id,
                title=heading.title(),  # Convert to title case for readability.
                text=body,
                level=1,
                start_char=match.start(),
            )
            sections.append(section)

        logger.debug("text_sections_detected", strategy="caps", count=len(sections))
        return sections

    # ------------------------------------------------------------------
    # Fallback: single section
    # ------------------------------------------------------------------

    def _single_section(self, text: str) -> list[Section]:
        """Wrap the entire text in a single section."""
        logger.debug("text_sections_detected", strategy="single")
        return [
            self._build_section(
                section_id="sec-1",
                title="Document",
                text=text.strip(),
                level=1,
                start_char=0,
            )
        ]

    # ------------------------------------------------------------------
    # Metadata heuristics
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_title(text: str, sections: list[Section]) -> str | None:
        """Guess the document title.

        Prefers the first non-section line at the top of the document.
        Falls back to the first section title.
        """
        lines = text.strip().split("\n")
        for line in lines[:5]:
            line = line.strip()
            if (
                line
                and len(line) > 5
                and len(line) < 200
                and not line.startswith(("http", "//", "#"))
            ):
                return line
        if sections:
            return sections[0].title
        return None

    @staticmethod
    def _extract_abstract(text: str) -> str | None:
        """Extract an abstract-like section from the beginning."""
        match = re.search(
            r"(?:^|\n)\s*(?:Abstract|ABSTRACT)[:\s]*\n?(.*?)(?=\n\s*(?:\d+\.?\s+)?(?:Introduction|INTRODUCTION|1\s)|$)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            abstract = re.sub(r"\s+", " ", match.group(1).strip())
            if len(abstract) > 30:
                return abstract
        return None


def _looks_like_path(s: str) -> bool:
    """Heuristic check: does *s* look like a file path rather than text?"""
    if "\n" in s:
        return False
    if len(s) > 500:
        return False
    # Contains path separator or known extension.
    if "/" in s or "\\" in s:
        return True
    if Path(s).suffix in (".txt", ".text", ".md", ".rst"):
        return True
    return False
