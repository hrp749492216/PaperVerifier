"""PDF document parser.

Uses pdfplumber as the primary extraction engine.  PyMuPDF (fitz) was
removed because it is AGPL-3.0 licensed, incompatible with the proprietary
license (CRIT-5).  Section detection uses text heuristics.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

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

# Maximum file size for PDF processing (100 MB).
_MAX_PDF_SIZE = 100 * 1024 * 1024

# Threshold for garbled text detection.
_GARBLED_THRESHOLD = 0.20


class PDFParser(BaseParser):
    """Parse PDF documents into :class:`ParsedDocument`.

    Strategy:
    1. Use ``pdfplumber`` for text extraction with table support.
    2. If extracted text has > 20% non-printable characters, log a
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
                    f"PDF file too large: {len(source):,} bytes (max {_MAX_PDF_SIZE:,})."
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
                    f"PDF file too large: {path.stat().st_size:,} bytes (max {_MAX_PDF_SIZE:,})."
                )
            pdf_bytes = path.read_bytes()
            source_path = str(path)

        # Verify magic bytes.
        if not pdf_bytes[:4].startswith(b"%PDF"):
            raise InputValidationError(
                "File does not appear to be a valid PDF (missing %PDF header)."
            )

        # Use pdfplumber for extraction (PyMuPDF removed due to AGPL license).
        # Distinguish between "library not installed" and "extraction failed"
        # so operators get the correct remediation guidance (Codex-1 fix #7).
        text, sections, metadata = self._try_pdfplumber(pdf_bytes)

        if text is None:
            try:
                import pdfplumber  # noqa: F401,PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "pdfplumber is required for PDF parsing. Install with: pip install pdfplumber"
                ) from exc
            # pdfplumber is installed but extraction failed.
            raise InputValidationError(
                "PDF text extraction failed. The file may be corrupted, "
                "image-only (scanned), or in an unsupported format."
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
        authors = [a.strip() for a in authors_raw.split(",") if a.strip()] if authors_raw else []
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
    # pdfplumber extraction (primary engine)
    # ------------------------------------------------------------------

    def _try_pdfplumber(self, pdf_bytes: bytes) -> tuple[str | None, list[Section], dict[str, Any]]:
        """Attempt extraction using pdfplumber.

        Returns ``(full_text, sections, metadata)`` or ``(None, [], {})``
        if the library is unavailable.
        """
        try:
            import pdfplumber  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("pdfplumber_not_available")
            return None, [], {}

        text_parts: list[str] = []
        metadata: dict[str, Any] = {}

        # Narrow the exception boundary: only catch failures from
        # pdfplumber.open() itself, not from the entire extraction block.
        try:
            pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        except Exception as exc:
            logger.warning("pdfplumber_open_failed", error=str(exc), exc_info=True)
            return None, [], {}

        with pdf:
            try:
                raw_meta = pdf.metadata or {}
                for key in ("Title", "Author", "Subject", "Keywords", "Creator"):
                    val = raw_meta.get(key)
                    if val:
                        metadata[key.lower()] = val
            except Exception:
                logger.debug("pdf_metadata_extraction_failed", exc_info=True)

            # Enforce page limit (MED-S4).
            from paperverifier.config import get_settings

            max_pages = get_settings().max_document_pages
            pages_to_process = pdf.pages[:max_pages]
            if len(pdf.pages) > max_pages:
                logger.warning(
                    "pdf_page_limit",
                    total_pages=len(pdf.pages),
                    max_pages=max_pages,
                )

            for page in pages_to_process:
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

        if not text_parts:
            return None, [], metadata

        full_text = "\n\n".join(text_parts)
        sections = self._heuristic_sections(full_text)

        return full_text, sections, metadata

    # ------------------------------------------------------------------
    # Heuristic sections
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
