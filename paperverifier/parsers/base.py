"""Abstract base class for all document parsers.

Provides shared utilities for sentence splitting, section building,
reference extraction, and citation style detection.
"""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod

import structlog

from paperverifier.models.document import (
    FigureTableRef,
    Paragraph,
    ParsedDocument,
    Reference,
    Section,
    Sentence,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

# Abbreviations that should NOT trigger sentence boundaries.
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc",
    "fig", "figs", "eq", "eqs", "ref", "refs", "vol", "no", "pp",
    "ed", "eds", "al", "e.g", "i.e", "viz", "approx", "dept",
    "assn", "bros", "inc", "ltd", "co", "corp", "jan", "feb", "mar",
    "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}

# Simple sentence boundary: sentence-ending punctuation followed by
# whitespace and an uppercase letter or quotation mark.
_SENTENCE_BOUNDARY_RE = re.compile(
    r'[.!?](?=\s+[A-Z"\'\(\[])'
)

# Pattern to extract the word immediately before a period (for abbreviation
# checking).
_WORD_BEFORE_DOT_RE = re.compile(r'(\b\w+)\.$')


class BaseParser(ABC):
    """Abstract base class for all document parsers.

    Subclasses must implement :meth:`parse`. Shared helper methods handle
    sentence splitting, section construction, reference extraction, and
    figure/table reference detection.
    """

    @abstractmethod
    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Parse a document source into a :class:`ParsedDocument`.

        Args:
            source: A file path, URL, raw text, or bytes content depending
                on the concrete parser.
            **kwargs: Parser-specific options.

        Returns:
            A fully populated :class:`ParsedDocument`.
        """

    # ------------------------------------------------------------------
    # Sentence splitting
    # ------------------------------------------------------------------

    def _split_into_sentences(self, text: str) -> list[str]:
        """Split *text* into sentences using heuristic regex rules.

        Handles common abbreviations (Dr., Fig., e.g.), decimal numbers,
        initials, and quotation marks. Returns a list of non-empty
        sentence strings with whitespace normalised.
        """
        if not text or not text.strip():
            return []

        # Normalise whitespace first (collapse runs, strip edges).
        normalised = re.sub(r"\s+", " ", text.strip())

        # Find candidate boundary positions, then filter out false
        # positives (abbreviations, initials, decimal numbers).
        sentences: list[str] = []
        last_end = 0

        for match in _SENTENCE_BOUNDARY_RE.finditer(normalised):
            boundary_pos = match.start()  # Position of the punctuation mark.
            punct = normalised[boundary_pos]

            # Only apply abbreviation/initial/decimal checks to periods.
            if punct == ".":
                prefix = normalised[last_end:boundary_pos]

                # Check for abbreviation (word before the period).
                word_match = _WORD_BEFORE_DOT_RE.search(prefix + ".")
                if word_match:
                    word = word_match.group(1).lower()
                    if word in _ABBREVIATIONS:
                        continue

                # Check for single-capital initial (e.g. "J." in "J. K. Rowling").
                if len(prefix) >= 1 and prefix[-1:].isupper() and (
                    len(prefix) < 2 or not prefix[-2].isalpha()
                ):
                    continue

                # Check for decimal number (digit before period).
                if prefix and prefix[-1].isdigit():
                    continue

            # Valid boundary found.
            boundary_end = match.end()
            sentence = normalised[last_end:boundary_end].strip()
            if sentence:
                sentences.append(sentence)
            last_end = boundary_end

        # Remaining text after the last boundary.
        remainder = normalised[last_end:].strip()
        if remainder:
            sentences.append(remainder)

        # If no boundaries were found, return the whole text.
        if not sentences:
            sentences = [normalised]

        return sentences

    # ------------------------------------------------------------------
    # Section building
    # ------------------------------------------------------------------

    def _build_section(
        self,
        section_id: str,
        title: str,
        text: str,
        level: int,
        start_char: int,
    ) -> Section:
        """Build a :class:`Section` with paragraphs and sentences.

        Paragraphs are delimited by double newlines.  Each paragraph is
        split into sentences via :meth:`_split_into_sentences`.  Hierarchical
        IDs follow the pattern ``sec-N.para-M.sent-K``.

        Args:
            section_id: The section ID (e.g. ``"sec-1"``).
            title: Section heading text.
            text: Body text of the section.
            level: Heading level (1 = top-level).
            start_char: Character offset of this section in the full document.

        Returns:
            A fully populated :class:`Section`.
        """
        paragraphs: list[Paragraph] = []

        # Split into paragraphs on double newlines.
        raw_paragraphs = re.split(r"\n\s*\n", text)
        char_offset = start_char

        for para_idx, raw_para in enumerate(raw_paragraphs, start=1):
            raw_para = raw_para.strip()
            if not raw_para:
                char_offset += 1  # Account for the empty space.
                continue

            para_id = f"{section_id}.para-{para_idx}"
            sentences: list[Sentence] = []
            sentence_texts = self._split_into_sentences(raw_para)

            sent_offset = char_offset
            for sent_idx, sent_text in enumerate(sentence_texts, start=1):
                sent_id = f"{para_id}.sent-{sent_idx}"
                sent_end = sent_offset + len(sent_text)
                sentences.append(
                    Sentence(
                        id=sent_id,
                        text=sent_text,
                        start_char=sent_offset,
                        end_char=sent_end,
                    )
                )
                sent_offset = sent_end + 1  # +1 for inter-sentence space.

            para_end = char_offset + len(raw_para)
            paragraphs.append(
                Paragraph(
                    id=para_id,
                    sentences=sentences,
                    raw_text=raw_para,
                    start_char=char_offset,
                    end_char=para_end,
                )
            )
            # Advance past the paragraph text + the paragraph separator.
            char_offset = para_end + 2  # +2 for the double-newline separator.

        section_end = start_char + len(text) if text else start_char
        return Section(
            id=section_id,
            title=title,
            level=level,
            paragraphs=paragraphs,
            start_char=start_char,
            end_char=section_end,
        )

    # ------------------------------------------------------------------
    # Reference extraction
    # ------------------------------------------------------------------

    def _extract_references_regex(self, text: str) -> list[Reference]:
        """Extract references using regex for common citation formats.

        Supported formats:
        - **Numbered**: ``[1] Author, "Title", Journal, Year``
        - **Author-year**: ``(Smith, 2020)`` or ``(Smith & Jones, 2019)``
        - **BibTeX keys**: ``\\cite{smith2020}``

        The citation style is auto-detected and stored in each reference.

        Returns:
            A list of :class:`Reference` objects.
        """
        references: list[Reference] = []
        style = self._detect_citation_style(text)

        if style == "numbered":
            references.extend(self._extract_numbered_refs(text))
        elif style == "author_year":
            references.extend(self._extract_author_year_refs(text))

        # Always try BibTeX-style \cite{} extraction (may appear alongside
        # any other style in LaTeX documents).
        references.extend(self._extract_bibtex_cite_refs(text))

        # Set the detected style on every reference.
        for ref in references:
            if ref.citation_style is None:
                ref.citation_style = style

        return references

    def _extract_numbered_refs(self, text: str) -> list[Reference]:
        """Extract numbered references like ``[1] Author, Title...``."""
        refs: list[Reference] = []
        # Match reference list entries: [N] followed by the rest of the line.
        pattern = re.compile(
            r"^\s*\[(\d+)\]\s+(.+?)$",
            re.MULTILINE,
        )
        for match in pattern.finditer(text):
            num = match.group(1)
            raw = match.group(2).strip()
            ref = Reference(
                raw_text=raw,
                citation_key=num,
                citation_style="numbered",
            )
            # Attempt to extract year.
            year_match = re.search(r"\b((?:19|20)\d{2})\b", raw)
            if year_match:
                ref.year = int(year_match.group(1))

            # Attempt to extract DOI.
            doi_match = re.search(
                r"(?:doi:\s*|https?://doi\.org/)(10\.\d{4,}/\S+)",
                raw,
                re.IGNORECASE,
            )
            if doi_match:
                ref.doi = doi_match.group(1).rstrip(".")

            # Find in-text citations [N] throughout the document.
            cite_pattern = re.compile(r"\[" + re.escape(num) + r"\]")
            ref.in_text_locations = [
                f"char-{m.start()}" for m in cite_pattern.finditer(text)
            ]

            refs.append(ref)
        return refs

    def _extract_author_year_refs(self, text: str) -> list[Reference]:
        """Extract author-year style references like ``(Smith, 2020)``."""
        refs: list[Reference] = []
        seen: set[str] = set()

        # Match (Author, Year) or (Author & Author, Year) patterns in text.
        pattern = re.compile(
            r"\(([A-Z][a-z]+(?:\s+(?:&|and)\s+[A-Z][a-z]+)?"
            r"(?:\s+et\s+al\.?)?),?\s*((?:19|20)\d{2}[a-z]?)\)",
        )
        for match in pattern.finditer(text):
            author = match.group(1).strip()
            year_str = match.group(2).strip()
            key = f"{author}_{year_str}"
            if key in seen:
                continue
            seen.add(key)

            ref = Reference(
                raw_text=f"{author}, {year_str}",
                citation_key=key,
                citation_style="author_year",
                authors=[author],
            )
            try:
                ref.year = int(year_str[:4])
            except ValueError:
                pass

            # Locate all in-text occurrences of this citation.
            cite_re = re.compile(
                re.escape(author) + r",?\s*" + re.escape(year_str)
            )
            ref.in_text_locations = [
                f"char-{m.start()}" for m in cite_re.finditer(text)
            ]
            refs.append(ref)

        return refs

    def _extract_bibtex_cite_refs(self, text: str) -> list[Reference]:
        """Extract BibTeX ``\\cite{key}`` references from LaTeX source."""
        refs: list[Reference] = []
        seen: set[str] = set()

        # Match \cite{key1,key2}, \citep{key}, \citet{key}, etc.
        pattern = re.compile(r"\\cite[pt]?\{([^}]+)\}")
        for match in pattern.finditer(text):
            keys_str = match.group(1)
            for key in keys_str.split(","):
                key = key.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                refs.append(
                    Reference(
                        raw_text=f"\\cite{{{key}}}",
                        citation_key=key,
                        citation_style="bibtex",
                    )
                )
        return refs

    # ------------------------------------------------------------------
    # Citation style detection
    # ------------------------------------------------------------------

    def _detect_citation_style(self, text: str) -> str:
        """Detect the dominant citation style in *text*.

        Returns one of ``'numbered'``, ``'author_year'``, or
        ``'superscript'``.
        """
        numbered_count = len(re.findall(r"\[\d+\]", text))
        author_year_count = len(
            re.findall(
                r"\([A-Z][a-z]+(?:\s+(?:&|and)\s+[A-Z][a-z]+)?"
                r"(?:\s+et\s+al\.?)?,?\s*(?:19|20)\d{2}[a-z]?\)",
                text,
            )
        )
        superscript_count = len(re.findall(r"(?<!\[)\d+(?=\s|,|;|\))", text))

        # Choose the style with the most matches (with sensible thresholds).
        if numbered_count >= 3 and numbered_count >= author_year_count:
            return "numbered"
        if author_year_count >= 2:
            return "author_year"
        if numbered_count >= 1:
            return "numbered"

        return "author_year"  # Default fallback.

    # ------------------------------------------------------------------
    # Figure / Table reference detection
    # ------------------------------------------------------------------

    def _detect_figure_table_refs(self, text: str) -> list[FigureTableRef]:
        """Extract Figure N and Table N references, matching to captions.

        Finds in-text references like ``Figure 1``, ``Fig. 2``, ``Table 3``
        and attempts to locate corresponding captions in the text.

        Returns:
            A list of :class:`FigureTableRef` objects.
        """
        refs: list[FigureTableRef] = []
        seen: set[str] = set()

        # Pattern for in-text references.
        ref_pattern = re.compile(
            r"\b(Fig(?:ure|\.)?|Table)\s+(\d+[a-z]?)\b",
            re.IGNORECASE,
        )

        for match in ref_pattern.finditer(text):
            ref_type_raw = match.group(1).lower()
            number = match.group(2)

            if ref_type_raw.startswith("fig"):
                ref_type = "figure"
            else:
                ref_type = "table"

            key = f"{ref_type}-{number}"
            if key not in seen:
                seen.add(key)

                # Try to find a caption for this figure/table.
                caption = self._find_caption(text, ref_type, number)

                fig_ref = FigureTableRef(
                    id=key,
                    ref_type=ref_type,
                    number=number,
                    caption=caption,
                )
                refs.append(fig_ref)

            # Record in-text location for existing ref.
            for r in refs:
                if r.id == key:
                    location = f"char-{match.start()}"
                    if location not in r.in_text_references:
                        r.in_text_references.append(location)

        return refs

    def _find_caption(self, text: str, ref_type: str, number: str) -> str | None:
        """Attempt to locate a caption for a figure or table."""
        if ref_type == "figure":
            # Match: Figure N. Caption text... or Figure N: Caption text...
            pattern = re.compile(
                r"(?:Fig(?:ure|\.)?)\s+"
                + re.escape(number)
                + r"[.:\s]+([^\n]+)",
                re.IGNORECASE,
            )
        else:
            pattern = re.compile(
                r"Table\s+"
                + re.escape(number)
                + r"[.:\s]+([^\n]+)",
                re.IGNORECASE,
            )

        match = pattern.search(text)
        if match:
            caption = match.group(1).strip()
            # Truncate very long captions.
            if len(caption) > 300:
                caption = caption[:297] + "..."
            return caption
        return None

    # ------------------------------------------------------------------
    # Text quality checking
    # ------------------------------------------------------------------

    @staticmethod
    def _check_text_quality(text: str) -> float:
        """Return the fraction of non-printable / garbled characters.

        Useful for detecting OCR failures or corrupted PDF extraction.
        A value above 0.2 (20%) indicates poor extraction quality.
        """
        if not text:
            return 0.0
        total = len(text)
        garbled = sum(
            1
            for ch in text
            if not ch.isprintable()
            and ch not in ("\n", "\r", "\t")
            and unicodedata.category(ch).startswith("C")
        )
        return garbled / total
