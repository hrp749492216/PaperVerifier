"""PDF document parser.

Uses PyMuPDF (fitz) as the primary extraction engine for its superior
layout analysis and font metadata, with pdfplumber as a fallback.
Detects sections based on font size differences and handles two-column
layouts.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import structlog

from paperverifier.models.document import (
    FigureTableRef,
    ParsedDocument,
    Reference,
    Section,
)
from paperverifier.parsers.base import BaseParser
from paperverifier.security.input_validator import (
    InputValidationError,
    validate_file_path,
    validate_uploaded_file,
)

logger = structlog.get_logger(__name__)

# Maximum file size for PDF processing (100 MB).
_MAX_PDF_SIZE = 100 * 1024 * 1024

# Threshold for garbled text detection.
_GARBLED_THRESHOLD = 0.20


class PDFParser(BaseParser):
    """Parse PDF documents into :class:`ParsedDocument`.

    Strategy:
    1. Try PyMuPDF (``fitz``) first -- better layout analysis, font
       metadata, and two-column support.
    2. Fall back to ``pdfplumber`` if PyMuPDF is unavailable or fails.
    3. If extracted text has > 20% non-printable characters, log a
       quality warning.
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Parse a PDF from a file path or raw bytes.

        Args:
            source: Either a file path (str) or raw PDF bytes.
            **kwargs: Optional ``allowed_dir`` (:class:`Path`) for
                path validation.

        Returns:
            A fully populated :class:`ParsedDocument`.

        Raises:
            InputValidationError: If the file fails validation.
            RuntimeError: If no PDF extraction library is available.
        """
        pdf_bytes: bytes
        source_path = ""

        if isinstance(source, bytes):
            # Raw bytes -- validate size.
            if len(source) > _MAX_PDF_SIZE:
                raise InputValidationError(
                    f"PDF file too large: {len(source):,} bytes "
                    f"(max {_MAX_PDF_SIZE:,})."
                )
            if len(source) == 0:
                raise InputValidationError("PDF file is empty.")
            pdf_bytes = source
        else:
            # File path.
            path = Path(source)
            allowed_dir = kwargs.get("allowed_dir")
            if allowed_dir is not None:
                validate_file_path(path, Path(allowed_dir))  # type: ignore[arg-type]

            if not path.exists():
                raise InputValidationError(f"PDF file not found: {source}")
            if path.stat().st_size > _MAX_PDF_SIZE:
                raise InputValidationError(
                    f"PDF file too large: {path.stat().st_size:,} bytes "
                    f"(max {_MAX_PDF_SIZE:,})."
                )
            pdf_bytes = path.read_bytes()
            source_path = str(path)

        # Verify magic bytes.
        if not pdf_bytes[:4].startswith(b"%PDF"):
            raise InputValidationError(
                "File does not appear to be a valid PDF (missing %PDF header)."
            )

        # Try extraction engines in priority order.
        text, sections, metadata = self._try_pymupdf(pdf_bytes)
        if text is None:
            text, sections, metadata = self._try_pdfplumber(pdf_bytes)

        if text is None:
            raise RuntimeError(
                "No PDF extraction library available. "
                "Install pymupdf or pdfplumber: pip install pymupdf pdfplumber"
            )

        # Quality check.
        garbled_ratio = self._check_text_quality(text)
        if garbled_ratio > _GARBLED_THRESHOLD:
            logger.warning(
                "pdf_quality_low",
                garbled_ratio=f"{garbled_ratio:.1%}",
                hint="The PDF may contain scanned images or corrupted text.",
            )

        # Build structured document.
        if not sections:
            sections = self._heuristic_sections(text)

        # Extract references and figure/table refs.
        references = self._extract_references_regex(text)
        fig_table_refs = self._detect_figure_table_refs(text)

        # Extract title and abstract heuristically.
        title = metadata.get("title") or self._guess_title(text)
        authors_raw = metadata.get("author", "")
        authors = (
            [a.strip() for a in authors_raw.split(",") if a.strip()]
            if authors_raw
            else []
        )
        abstract = self._extract_abstract(text)

        return ParsedDocument(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            figures_tables=fig_table_refs,
            full_text=text,
            source_type="pdf",
            source_path=source_path,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # PyMuPDF extraction
    # ------------------------------------------------------------------

    def _try_pymupdf(
        self, pdf_bytes: bytes
    ) -> tuple[str | None, list[Section], dict[str, Any]]:
        """Attempt extraction using PyMuPDF (fitz).

        Returns ``(full_text, sections, metadata)`` or ``(None, [], {})``
        if the library is unavailable.
        """
        try:
            import fitz  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("pymupdf_not_available")
            return None, [], {}

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            logger.warning("pymupdf_open_failed", error=str(exc))
            return None, [], {}

        metadata: dict[str, Any] = {}
        try:
            raw_meta = doc.metadata or {}
            for key in ("title", "author", "subject", "keywords", "creator"):
                val = raw_meta.get(key)
                if val:
                    metadata[key] = val
        except Exception:
            pass

        # Extract text with font information for section detection.
        all_blocks: list[dict[str, Any]] = []
        full_text_parts: list[str] = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            # sort=True handles two-column layouts by reading left-to-right.
            blocks = page.get_text("dict", sort=True).get("blocks", [])
            for block in blocks:
                if block.get("type") != 0:
                    continue  # Skip image blocks.
                for line in block.get("lines", []):
                    line_text = ""
                    max_font_size = 0.0
                    is_bold = False
                    for span in line.get("spans", []):
                        span_text = span.get("text", "")
                        line_text += span_text
                        font_size = span.get("size", 0)
                        if font_size > max_font_size:
                            max_font_size = font_size
                        flags = span.get("flags", 0)
                        if flags & 2 ** 4:  # Bold flag.
                            is_bold = True
                    line_text = line_text.strip()
                    if line_text:
                        all_blocks.append({
                            "text": line_text,
                            "font_size": max_font_size,
                            "bold": is_bold,
                            "page": page_num,
                        })
                        full_text_parts.append(line_text)

        doc.close()

        if not all_blocks:
            return None, [], metadata

        full_text = "\n".join(full_text_parts)

        # Detect sections from font metadata.
        sections = self._detect_sections(all_blocks, full_text)

        return full_text, sections, metadata

    # ------------------------------------------------------------------
    # pdfplumber extraction
    # ------------------------------------------------------------------

    def _try_pdfplumber(
        self, pdf_bytes: bytes
    ) -> tuple[str | None, list[Section], dict[str, Any]]:
        """Attempt extraction using pdfplumber.

        Returns ``(full_text, sections, metadata)`` or ``(None, [], {})``
        if the library is unavailable.
        """
        try:
            import pdfplumber  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("pdfplumber_not_available")
            return None, [], {}

        try:
            pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        except Exception as exc:
            logger.warning("pdfplumber_open_failed", error=str(exc))
            return None, [], {}

        metadata: dict[str, Any] = {}
        try:
            raw_meta = pdf.metadata or {}
            for key in ("Title", "Author", "Subject", "Keywords", "Creator"):
                val = raw_meta.get(key)
                if val:
                    metadata[key.lower()] = val
        except Exception:
            pass

        text_parts: list[str] = []
        for page in pdf.pages:
            try:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            except Exception as exc:
                logger.warning(
                    "pdfplumber_page_error",
                    page=page.page_number,
                    error=str(exc),
                )

        pdf.close()

        if not text_parts:
            return None, [], metadata

        full_text = "\n\n".join(text_parts)
        sections = self._heuristic_sections(full_text)

        return full_text, sections, metadata

    # ------------------------------------------------------------------
    # Section detection from font metadata (PyMuPDF)
    # ------------------------------------------------------------------

    def _detect_sections(
        self,
        blocks: list[dict[str, Any]],
        full_text: str,
    ) -> list[Section]:
        """Detect sections based on font size differences.

        Lines with font sizes significantly larger than the body text
        median are treated as headings.  Bold text at larger sizes is
        weighted more heavily.
        """
        if not blocks:
            return self._heuristic_sections(full_text)

        # Compute median body font size.
        font_sizes = [b["font_size"] for b in blocks if b["font_size"] > 0]
        if not font_sizes:
            return self._heuristic_sections(full_text)

        font_sizes_sorted = sorted(font_sizes)
        median_size = font_sizes_sorted[len(font_sizes_sorted) // 2]

        # Heading threshold: at least 1.2x the median font size.
        heading_threshold = median_size * 1.2

        # Build section boundaries.
        current_heading: str | None = None
        current_text_parts: list[str] = []
        section_data: list[tuple[str, str]] = []

        for block in blocks:
            is_heading = (
                block["font_size"] >= heading_threshold
                or (block["bold"] and block["font_size"] >= median_size * 1.1)
            )
            # Additional heuristic: headings are usually short.
            is_short = len(block["text"].split()) <= 12

            if is_heading and is_short:
                # Save previous section.
                if current_heading is not None:
                    section_data.append(
                        (current_heading, "\n".join(current_text_parts))
                    )
                elif current_text_parts:
                    # Text before first heading -- treat as introduction.
                    section_data.append(
                        ("Introduction", "\n".join(current_text_parts))
                    )
                current_heading = block["text"]
                current_text_parts = []
            else:
                current_text_parts.append(block["text"])

        # Don't forget the last section.
        if current_heading is not None:
            section_data.append((current_heading, "\n".join(current_text_parts)))
        elif current_text_parts:
            section_data.append(("Document", "\n".join(current_text_parts)))

        if not section_data:
            return self._heuristic_sections(full_text)

        # Convert to Section objects.
        sections: list[Section] = []
        char_offset = 0
        for idx, (heading, body) in enumerate(section_data, start=1):
            section_id = f"sec-{idx}"
            section = self._build_section(
                section_id=section_id,
                title=heading,
                text=body,
                level=1,
                start_char=char_offset,
            )
            sections.append(section)
            char_offset += len(heading) + len(body) + 2

        return sections

    # ------------------------------------------------------------------
    # Heuristic sections (fallback)
    # ------------------------------------------------------------------

    def _heuristic_sections(self, text: str) -> list[Section]:
        """Detect sections using text heuristics when font metadata is
        unavailable.

        Looks for patterns like:
        - Numbered headings: ``1. Introduction``, ``2 Methods``
        - ALL-CAPS lines that are short (likely headings)
        """
        section_pattern = re.compile(
            r"^(\d+\.?\s+[A-Z][A-Za-z\s:]+)$"
            r"|^([A-Z][A-Z\s]{3,50})$",
            re.MULTILINE,
        )

        matches = list(section_pattern.finditer(text))
        if not matches:
            # No sections found -- wrap entire text.
            return [
                self._build_section(
                    section_id="sec-1",
                    title="Document",
                    text=text,
                    level=1,
                    start_char=0,
                )
            ]

        sections: list[Section] = []
        for idx, match in enumerate(matches):
            heading = (match.group(1) or match.group(2)).strip()
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[start:end].strip()

            section_id = f"sec-{idx + 1}"
            section = self._build_section(
                section_id=section_id,
                title=heading,
                text=body,
                level=1,
                start_char=match.start(),
            )
            sections.append(section)

        return sections

    # ------------------------------------------------------------------
    # Metadata heuristics
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_title(text: str) -> str | None:
        """Guess the paper title from the first non-empty line."""
        for line in text.split("\n"):
            line = line.strip()
            if line and len(line) > 5 and len(line) < 300:
                return line
        return None

    @staticmethod
    def _extract_abstract(text: str) -> str | None:
        """Extract the abstract if present."""
        abstract_match = re.search(
            r"(?:^|\n)\s*(?:Abstract|ABSTRACT)[:\s]*\n?(.*?)(?=\n\s*(?:\d+\.?\s+)?(?:Introduction|INTRODUCTION|Keywords|1\s)|$)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if abstract_match:
            abstract = abstract_match.group(1).strip()
            # Collapse whitespace.
            abstract = re.sub(r"\s+", " ", abstract)
            if len(abstract) > 30:  # Sanity check.
                return abstract
        return None
