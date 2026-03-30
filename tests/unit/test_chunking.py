"""Unit tests for paperverifier.utils.chunking."""

from __future__ import annotations

from paperverifier.models.document import Paragraph, ParsedDocument, Section, Sentence
from paperverifier.utils.chunking import chunk_document, create_document_summary


def _make_document(full_text: str, sections: list[Section] | None = None) -> ParsedDocument:
    """Helper to create a ParsedDocument with given text and sections."""
    return ParsedDocument(
        title="Test Paper",
        authors=["Author One"],
        abstract="This is a test abstract for unit testing.",
        full_text=full_text,
        sections=sections or [],
        source_type="test",
    )


def _make_section(
    section_id: str,
    title: str,
    text: str,
    start_char: int = 0,
    end_char: int = 0,
) -> Section:
    """Helper to create a Section with a single paragraph."""
    para = Paragraph(
        id=f"{section_id}.para-1",
        sentences=[
            Sentence(
                id=f"{section_id}.para-1.sent-1",
                text=text,
                start_char=start_char,
                end_char=end_char or start_char + len(text),
            ),
        ],
        raw_text=text,
        start_char=start_char,
        end_char=end_char or start_char + len(text),
    )
    return Section(
        id=section_id,
        title=title,
        paragraphs=[para],
        start_char=start_char,
        end_char=end_char or start_char + len(text),
    )


# ---------------------------------------------------------------------------
# chunk_document
# ---------------------------------------------------------------------------


class TestChunkDocumentSmall:
    """A small document should produce a single complete chunk."""

    def test_small_doc_single_chunk(self) -> None:
        text = "A short document that fits within any context window."
        section = _make_section("sec-1", "Introduction", text)
        doc = _make_document(text, sections=[section])

        # Use a model with a large context window
        chunks = chunk_document(doc, model="claude-sonnet-4")

        assert len(chunks) == 1
        assert chunks[0].is_complete is True
        assert chunks[0].text == text
        assert chunks[0].chunk_index == 0
        assert chunks[0].total_chunks == 1
        assert "sec-1" in chunks[0].section_ids

    def test_empty_document(self) -> None:
        doc = _make_document("")
        chunks = chunk_document(doc, model="claude-sonnet-4")
        # Empty text = 0 tokens, fits in a single chunk
        assert len(chunks) == 1
        assert chunks[0].is_complete is True


class TestChunkDocumentLarge:
    """A large document should be split into multiple chunks."""

    def test_large_doc_multiple_chunks(self) -> None:
        # Create a document large enough to exceed a small context window.
        # gpt-4 has 8192 tokens = ~32768 chars budget (before 70% factor).
        # Budget = (8192 - 4000) * 0.70 = ~2934 tokens = ~11736 chars.
        section_text = "This is a long paragraph. " * 300  # ~7800 chars
        sections = [
            _make_section("sec-1", "Section One", section_text, start_char=0, end_char=len(section_text)),
            _make_section(
                "sec-2",
                "Section Two",
                section_text,
                start_char=len(section_text),
                end_char=len(section_text) * 2,
            ),
        ]
        full_text = section_text + section_text
        doc = _make_document(full_text, sections=sections)

        chunks = chunk_document(doc, model="gpt-4")

        assert len(chunks) >= 2
        assert all(not c.is_complete for c in chunks)
        # Each chunk should have sequential indices
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i
            assert chunk.total_chunks == len(chunks)


# ---------------------------------------------------------------------------
# create_document_summary
# ---------------------------------------------------------------------------


class TestCreateDocumentSummary:
    """Tests for create_document_summary."""

    def test_summary_includes_title(self) -> None:
        doc = _make_document("Some text", sections=[])
        summary = create_document_summary(doc)
        assert "Test Paper" in summary

    def test_summary_includes_authors(self) -> None:
        doc = _make_document("Some text", sections=[])
        summary = create_document_summary(doc)
        assert "Author One" in summary

    def test_summary_includes_section_outline(self) -> None:
        sections = [
            _make_section("sec-1", "Introduction", "Intro text"),
            _make_section("sec-2", "Methods", "Methods text"),
        ]
        doc = _make_document("Full text here", sections=sections)
        summary = create_document_summary(doc)
        assert "Introduction" in summary
        assert "Methods" in summary
        assert "SECTION OUTLINE" in summary

    def test_summary_respects_max_tokens(self) -> None:
        # With a very small token limit, the summary should be truncated
        doc = _make_document("x" * 10000, sections=[])
        summary = create_document_summary(doc, max_tokens=10)
        # max_tokens=10 -> max_chars=40; summary should be short
        assert len(summary) <= 50  # some tolerance for word-boundary truncation
