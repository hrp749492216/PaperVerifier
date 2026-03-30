"""Markdown document parser.

Parses Markdown files by detecting ATX headings (``# Heading``) and
setext headings (underlined with ``===`` or ``---``).  Maps the heading
hierarchy to sections, paragraphs, and sentences.
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

# ATX heading: 1-6 hash marks followed by space and text.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", re.MULTILINE)

# Setext headings: text line followed by === or --- line.
_SETEXT_H1_RE = re.compile(r"^(.+)\n={3,}\s*$", re.MULTILINE)
_SETEXT_H2_RE = re.compile(r"^(.+)\n-{3,}\s*$", re.MULTILINE)


class MarkdownParser(BaseParser):
    """Parse Markdown documents into :class:`ParsedDocument`.

    Supports ATX headings (``# H1`` through ``###### H6``) and setext
    headings (underlined with ``===`` or ``---``).  YAML front-matter
    is extracted for metadata if present.
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Parse a Markdown file from a path, bytes, or raw string.

        Args:
            source: File path (str), raw Markdown string, or UTF-8 bytes.
            **kwargs: Optional ``allowed_dir`` (:class:`Path`).

        Returns:
            A fully populated :class:`ParsedDocument`.
        """
        source_path = ""

        if isinstance(source, bytes):
            text = source.decode("utf-8", errors="replace")
        elif isinstance(source, str) and (
            "\n" in source or len(source) > 500 or not Path(source).suffix
        ):
            # Looks like raw Markdown content, not a file path.
            text = source
        else:
            # Treat as file path.
            path = Path(source)
            allowed_dir = kwargs.get("allowed_dir")
            if allowed_dir is not None:
                validate_file_path(path, Path(allowed_dir))  # type: ignore[arg-type]

            if not path.exists():
                raise InputValidationError(f"Markdown file not found: {source}")
            text = path.read_text(encoding="utf-8", errors="replace")
            source_path = str(path)

        if not text.strip():
            raise InputValidationError("Markdown document is empty.")

        # Extract YAML front-matter if present.
        metadata, text = self._extract_frontmatter(text)

        # Build sections from headings.
        sections = self._parse_headings(text)

        # Extract metadata fields.
        title = metadata.get("title") or (
            sections[0].title if sections else None
        )
        authors_raw = metadata.get("author") or metadata.get("authors", "")
        if isinstance(authors_raw, list):
            authors = [str(a) for a in authors_raw]
        elif isinstance(authors_raw, str) and authors_raw:
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
        else:
            authors = []

        abstract = metadata.get("abstract") or self._extract_abstract(text)

        references = self._extract_references_regex(text)
        fig_table_refs = self._detect_figure_table_refs(text)

        return ParsedDocument(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            figures_tables=fig_table_refs,
            full_text=text,
            source_type="markdown",
            source_path=source_path,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Front-matter extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_frontmatter(text: str) -> tuple[dict[str, object], str]:
        """Extract YAML front-matter delimited by ``---``.

        Returns ``(metadata_dict, remaining_text)``.  If no front-matter
        is found, returns ``({}, text)``.
        """
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not fm_match:
            return {}, text

        fm_raw = fm_match.group(1)
        remaining = text[fm_match.end():]

        # Simple key-value extraction (avoids requiring PyYAML).
        metadata: dict[str, object] = {}
        for line in fm_raw.split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")
                if value.startswith("[") and value.endswith("]"):
                    # Simple list parsing.
                    items = value[1:-1].split(",")
                    metadata[key] = [i.strip().strip('"').strip("'") for i in items]
                else:
                    metadata[key] = value

        return metadata, remaining

    # ------------------------------------------------------------------
    # Heading parsing
    # ------------------------------------------------------------------

    def _parse_headings(self, text: str) -> list[Section]:
        """Parse headings and build a flat list of sections.

        Collects all ATX and setext headings, sorts them by position,
        and splits the text into sections.
        """
        headings: list[tuple[int, int, str, int]] = []
        # (start_pos, end_pos, title, level)

        # ATX headings.
        for match in _ATX_HEADING_RE.finditer(text):
            level = len(match.group(1))
            title = match.group(2).strip()
            headings.append((match.start(), match.end(), title, level))

        # Setext H1 (===).
        for match in _SETEXT_H1_RE.finditer(text):
            title = match.group(1).strip()
            headings.append((match.start(), match.end(), title, 1))

        # Setext H2 (---), but only if the text before is not a
        # front-matter delimiter.
        for match in _SETEXT_H2_RE.finditer(text):
            title = match.group(1).strip()
            if title != "---":
                headings.append((match.start(), match.end(), title, 2))

        # Sort by position in document.
        headings.sort(key=lambda h: h[0])

        if not headings:
            # No headings found -- wrap entire text in one section.
            return [
                self._build_section(
                    section_id="sec-1",
                    title="Document",
                    text=text.strip(),
                    level=1,
                    start_char=0,
                )
            ]

        sections: list[Section] = []

        # Text before the first heading.
        if headings[0][0] > 0:
            preamble = text[: headings[0][0]].strip()
            if preamble:
                sections.append(
                    self._build_section(
                        section_id="sec-1",
                        title="Preamble",
                        text=preamble,
                        level=1,
                        start_char=0,
                    )
                )

        for idx, (start, end, title, level) in enumerate(headings):
            # Section body extends to the next heading.
            body_start = end
            body_end = (
                headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
            )
            body = text[body_start:body_end].strip()

            sec_num = len(sections) + 1
            section_id = f"sec-{sec_num}"
            section = self._build_section(
                section_id=section_id,
                title=title,
                text=body,
                level=level,
                start_char=start,
            )
            sections.append(section)

        return sections

    # ------------------------------------------------------------------
    # Abstract extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_abstract(text: str) -> str | None:
        """Extract abstract from Markdown text."""
        match = re.search(
            r"(?:^|\n)#+\s*Abstract\s*\n(.*?)(?=\n#+\s|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            abstract = re.sub(r"\s+", " ", match.group(1).strip())
            if len(abstract) > 30:
                return abstract
        return None
