"""Hierarchical document model with stable semantic IDs for location addressing."""

from __future__ import annotations

import hashlib
import uuid

from pydantic import BaseModel, Field


class Reference(BaseModel):
    """A bibliographic reference cited in the document."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    raw_text: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    citation_key: str | None = None
    citation_style: str | None = None
    in_text_locations: list[str] = Field(default_factory=list)


class Sentence(BaseModel):
    """A single sentence within a paragraph, addressable by stable ID."""

    id: str  # e.g., "sec-2.para-3.sent-1"
    text: str
    start_char: int
    end_char: int
    line_number: int | None = None
    references_cited: list[str] = Field(default_factory=list)


class Paragraph(BaseModel):
    """A paragraph within a section, containing ordered sentences."""

    id: str  # e.g., "sec-2.para-3"
    sentences: list[Sentence] = Field(default_factory=list)
    raw_text: str
    start_char: int
    end_char: int


class Section(BaseModel):
    """A document section with optional nested subsections.

    Uses recursive definition to support arbitrary heading depth.
    """

    id: str  # e.g., "sec-2"
    title: str
    level: int = 1
    paragraphs: list[Paragraph] = Field(default_factory=list)
    subsections: list[Section] = Field(default_factory=list)
    start_char: int = 0
    end_char: int = 0


class FigureTableRef(BaseModel):
    """A reference to a figure or table in the document."""

    id: str
    ref_type: str  # "figure" or "table"
    number: int | str
    caption: str | None = None
    in_text_references: list[str] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    """Fully parsed document with hierarchical addressing.

    Every text segment (section, paragraph, sentence) has a stable semantic ID
    of the form ``sec-N.para-M.sent-K``.  These IDs survive edits to unrelated
    parts of the document, enabling deterministic feedback targeting, conflict
    detection, and cross-section analysis.
    """

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    sections: list[Section] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    figures_tables: list[FigureTableRef] = Field(default_factory=list)
    full_text: str = ""
    source_type: str = ""
    source_path: str = ""
    content_hash: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def model_post_init(self, __context: object) -> None:
        """Compute content_hash after construction if not already set."""
        if not self.content_hash and self.full_text:
            self.content_hash = self.compute_hash()

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    def compute_hash(self) -> str:
        """Return the SHA-256 hex digest of ``full_text``."""
        return hashlib.sha256(self.full_text.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Segment lookup
    # ------------------------------------------------------------------

    def get_segment(self, segment_id: str) -> Sentence | Paragraph | Section | None:
        """Navigate the document tree and return the segment matching *segment_id*.

        Supports IDs at any depth (section, paragraph, or sentence).
        """
        return _find_segment_in_sections(self.sections, segment_id)

    def get_section_by_title(self, title: str) -> Section | None:
        """Case-insensitive lookup of a section by its title.

        Searches recursively through subsections as well.
        """
        return _find_section_by_title(self.sections, title.lower())

    # ------------------------------------------------------------------
    # Flattening helpers
    # ------------------------------------------------------------------

    def get_all_sentences(self) -> list[Sentence]:
        """Flatten and return every sentence across all sections (depth-first)."""
        sentences: list[Sentence] = []
        _collect_sentences(self.sections, sentences)
        return sentences

    # ------------------------------------------------------------------
    # Text extraction
    # ------------------------------------------------------------------

    def get_text_by_char_range(self, start: int, end: int) -> str:
        """Extract a substring from *full_text* by character offsets."""
        return self.full_text[start:end]

    def to_numbered_text(self) -> str:
        """Reconstruct *full_text* with 1-based line numbers for display."""
        lines = self.full_text.splitlines()
        width = len(str(len(lines)))
        return "\n".join(
            f"{i + 1:>{width}} | {line}" for i, line in enumerate(lines)
        )


# ======================================================================
# Private recursive helpers (module-level to avoid Pydantic serialisation
# issues with bound methods on recursive models).
# ======================================================================


def _find_segment_in_sections(
    sections: list[Section],
    segment_id: str,
) -> Sentence | Paragraph | Section | None:
    for section in sections:
        if section.id == segment_id:
            return section
        for paragraph in section.paragraphs:
            if paragraph.id == segment_id:
                return paragraph
            for sentence in paragraph.sentences:
                if sentence.id == segment_id:
                    return sentence
        # Recurse into subsections
        result = _find_segment_in_sections(section.subsections, segment_id)
        if result is not None:
            return result
    return None


def _find_section_by_title(
    sections: list[Section],
    title_lower: str,
) -> Section | None:
    for section in sections:
        if section.title.lower() == title_lower:
            return section
        result = _find_section_by_title(section.subsections, title_lower)
        if result is not None:
            return result
    return None


def _collect_sentences(
    sections: list[Section],
    accumulator: list[Sentence],
) -> None:
    for section in sections:
        for paragraph in section.paragraphs:
            accumulator.extend(paragraph.sentences)
        _collect_sentences(section.subsections, accumulator)
