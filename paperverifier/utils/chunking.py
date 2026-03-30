"""Model-aware document chunking for PaperVerifier.

Splits a :class:`~paperverifier.models.document.ParsedDocument` into
:class:`DocumentChunk` instances that fit within a target LLM's context
window.  The chunking strategy is context-window-aware: it first checks
whether the entire document fits (avoiding unnecessary splitting), and
falls back to section-level or sliding-window splitting when it does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paperverifier.models.document import ParsedDocument, Section
from paperverifier.utils.text import count_tokens_estimate, truncate_to_token_limit

# ---------------------------------------------------------------------------
# Context-window catalogue
# ---------------------------------------------------------------------------

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3.5-sonnet": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-opus": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Google
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    # xAI
    "grok-3": 131_072,
    "grok-3-mini": 131_072,
    "grok-2": 131_072,
    # DeepSeek
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # Meta (via API providers)
    "llama-3.3-70b": 128_000,
    "llama-3.1-405b": 128_000,
    "llama-3.1-70b": 128_000,
    # Mistral
    "mistral-large": 128_000,
    "mistral-medium": 32_000,
    "mistral-small": 32_000,
}

DEFAULT_CONTEXT_WINDOW: int = 64_000


def get_context_window(model: str) -> int:
    """Return the context window size (in tokens) for *model*.

    Resolution order:

    1. Exact match in :data:`MODEL_CONTEXT_WINDOWS`.
    2. Prefix match -- the longest key that is a prefix of *model* wins.
       This allows specifying ``"gpt-4o-2024-08-06"`` and matching the
       ``"gpt-4o"`` entry.
    3. Falls back to :data:`DEFAULT_CONTEXT_WINDOW`.

    Parameters
    ----------
    model:
        Model identifier string (e.g., ``"claude-sonnet-4"``).

    Returns
    -------
    int
        Context window in tokens.
    """
    # Exact match
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]

    # Prefix match (longest prefix wins)
    best_key: str | None = None
    best_len = 0
    for key in MODEL_CONTEXT_WINDOWS:
        if model.startswith(key) and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key is not None:
        return MODEL_CONTEXT_WINDOWS[best_key]

    return DEFAULT_CONTEXT_WINDOW


# ---------------------------------------------------------------------------
# DocumentChunk dataclass
# ---------------------------------------------------------------------------


@dataclass
class DocumentChunk:
    """A contiguous slice of a document that fits within a model's context.

    Attributes
    ----------
    text:
        The text content of this chunk.
    section_ids:
        IDs of the sections (fully or partially) included in this chunk.
    start_char:
        Starting character offset in the original ``full_text``.
    end_char:
        Ending character offset (exclusive) in the original ``full_text``.
    chunk_index:
        Zero-based index of this chunk among all chunks produced for the
        same document.
    total_chunks:
        Total number of chunks the document was split into.
    is_complete:
        ``True`` if the entire document fits in a single chunk (no
        splitting was necessary).
    """

    text: str
    section_ids: list[str] = field(default_factory=list)
    start_char: int = 0
    end_char: int = 0
    chunk_index: int = 0
    total_chunks: int = 1
    is_complete: bool = True


# ---------------------------------------------------------------------------
# Chunking logic
# ---------------------------------------------------------------------------


def _section_text(section: Section, full_text: str) -> str:
    """Extract the text for a section from *full_text* using char offsets.

    Falls back to reconstructing from paragraphs when char offsets are
    zero-length (e.g., the section was built programmatically).
    """
    if section.start_char < section.end_char:
        return full_text[section.start_char : section.end_char]
    # Fallback: reconstruct from paragraphs
    parts = [section.title]
    for para in section.paragraphs:
        parts.append(para.raw_text)
    for sub in section.subsections:
        parts.append(_section_text(sub, full_text))
    return "\n\n".join(parts)


def _collect_section_ids(section: Section) -> list[str]:
    """Recursively collect the IDs of *section* and all its subsections."""
    ids = [section.id]
    for sub in section.subsections:
        ids.extend(_collect_section_ids(sub))
    return ids


def _sliding_window_chunks(
    text: str,
    section_ids: list[str],
    start_char: int,
    max_chars: int,
    overlap_chars: int = 800,
) -> list[DocumentChunk]:
    """Split *text* into overlapping sliding-window chunks.

    Parameters
    ----------
    text:
        The text to split.
    section_ids:
        Section IDs to associate with each chunk.
    start_char:
        Character offset of *text* within the original full document.
    max_chars:
        Maximum characters per chunk.
    overlap_chars:
        Number of characters to overlap between consecutive chunks.

    Returns
    -------
    list[DocumentChunk]
        Ordered list of chunks (``chunk_index`` and ``total_chunks`` are
        set by the caller).
    """
    chunks: list[DocumentChunk] = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        # Try to break at a paragraph or sentence boundary
        if end < len(text):
            # Look for a paragraph break (\n\n) near the end
            boundary = text.rfind("\n\n", pos + max_chars // 2, end)
            if boundary == -1:
                # Try a sentence break (period followed by space or newline)
                boundary = text.rfind(". ", pos + max_chars // 2, end)
                if boundary != -1:
                    boundary += 2  # include the period and space
            if boundary > pos:
                end = boundary

        chunk_text = text[pos:end]
        chunks.append(
            DocumentChunk(
                text=chunk_text,
                section_ids=list(section_ids),
                start_char=start_char + pos,
                end_char=start_char + end,
                is_complete=False,
            )
        )
        if end >= len(text):
            break
        pos = end - overlap_chars
        if pos <= chunks[-1].start_char - start_char:
            # Guard against infinite loops with very small overlap
            pos = end
    return chunks


def chunk_document(
    document: ParsedDocument,
    model: str,
    reserve_tokens: int = 4000,
) -> list[DocumentChunk]:
    """Split *document* into chunks that fit within *model*'s context window.

    Strategy
    --------
    1. **Single-chunk fast path** -- If the entire document's estimated token
       count is at most 70% of the available context window (after reserving
       tokens for the system prompt and expected output), the document is
       returned as a single :class:`DocumentChunk` with ``is_complete=True``.

    2. **Section-level splitting** -- Each top-level section is treated as an
       independent chunk.  If a section fits within the budget, it becomes one
       chunk.  Adjacent small sections may be merged into a single chunk to
       reduce the total number of LLM calls.

    3. **Sliding-window fallback** -- If a single section exceeds the budget,
       it is further split using a sliding window with overlap so that no
       context is lost at chunk boundaries.

    Parameters
    ----------
    document:
        The parsed document to chunk.
    model:
        Model identifier used to look up the context window size.
    reserve_tokens:
        Tokens reserved for the system prompt, user prompt template text,
        and expected output.  Subtracted from the context window before
        computing the available budget.

    Returns
    -------
    list[DocumentChunk]
        Ordered list of chunks covering the entire document.
    """
    context_window = get_context_window(model)
    available_tokens = context_window - reserve_tokens
    # Use 70% of available space to leave room for prompt overhead
    budget_tokens = int(available_tokens * 0.70)
    budget_chars = budget_tokens * 4  # inverse of count_tokens_estimate

    full_text = document.full_text

    # ------------------------------------------------------------------
    # Fast path: entire document fits
    # ------------------------------------------------------------------
    doc_tokens = count_tokens_estimate(full_text)
    if doc_tokens <= budget_tokens:
        all_section_ids: list[str] = []
        for sec in document.sections:
            all_section_ids.extend(_collect_section_ids(sec))
        return [
            DocumentChunk(
                text=full_text,
                section_ids=all_section_ids,
                start_char=0,
                end_char=len(full_text),
                chunk_index=0,
                total_chunks=1,
                is_complete=True,
            )
        ]

    # ------------------------------------------------------------------
    # Section-level splitting with merging of small sections
    # ------------------------------------------------------------------
    raw_chunks: list[DocumentChunk] = []

    # Buffer for merging small consecutive sections
    merge_text = ""
    merge_ids: list[str] = []
    merge_start: int | None = None
    merge_end: int = 0

    def _flush_merge_buffer() -> None:
        nonlocal merge_text, merge_ids, merge_start, merge_end
        if merge_text:
            raw_chunks.append(
                DocumentChunk(
                    text=merge_text,
                    section_ids=list(merge_ids),
                    start_char=merge_start if merge_start is not None else 0,
                    end_char=merge_end,
                    is_complete=False,
                )
            )
            merge_text = ""
            merge_ids = []
            merge_start = None
            merge_end = 0

    for section in document.sections:
        sec_text = _section_text(section, full_text)
        sec_ids = _collect_section_ids(section)
        sec_tokens = count_tokens_estimate(sec_text)

        if sec_tokens > budget_tokens:
            # Flush any buffered small sections first
            _flush_merge_buffer()
            # Split this large section via sliding window
            window_chunks = _sliding_window_chunks(
                text=sec_text,
                section_ids=sec_ids,
                start_char=section.start_char,
                max_chars=budget_chars,
            )
            raw_chunks.extend(window_chunks)
        else:
            # Can we merge with the current buffer?
            merged_tokens = count_tokens_estimate(merge_text + "\n\n" + sec_text if merge_text else sec_text)
            if merged_tokens <= budget_tokens:
                # Merge
                if merge_text:
                    merge_text += "\n\n" + sec_text
                else:
                    merge_text = sec_text
                    merge_start = section.start_char
                merge_ids.extend(sec_ids)
                merge_end = section.end_char
            else:
                # Flush buffer and start a new one with this section
                _flush_merge_buffer()
                merge_text = sec_text
                merge_ids = list(sec_ids)
                merge_start = section.start_char
                merge_end = section.end_char

    _flush_merge_buffer()

    # ------------------------------------------------------------------
    # Assign chunk indices
    # ------------------------------------------------------------------
    total = len(raw_chunks)
    for i, chunk in enumerate(raw_chunks):
        chunk.chunk_index = i
        chunk.total_chunks = total

    # Edge case: if no sections were found, fall back to sliding window
    # on the full text.
    if not raw_chunks:
        raw_chunks = _sliding_window_chunks(
            text=full_text,
            section_ids=[],
            start_char=0,
            max_chars=budget_chars,
        )
        total = len(raw_chunks)
        for i, chunk in enumerate(raw_chunks):
            chunk.chunk_index = i
            chunk.total_chunks = total

    return raw_chunks


# ---------------------------------------------------------------------------
# Document summary for context prepending
# ---------------------------------------------------------------------------


def create_document_summary(
    document: ParsedDocument,
    max_tokens: int = 2000,
) -> str:
    """Create a compact structural summary of *document* for context prepending.

    This summary is designed to be prepended to every agent's prompt
    regardless of which chunk is being processed, giving each agent a
    global view of the paper's structure even when it only sees a portion
    of the text.

    The summary includes:

    - Title and authors.
    - Abstract (truncated if necessary).
    - Section outline with heading hierarchy.
    - Counts of key claims, paragraphs, and sentences per section.
    - Reference count and the first few reference titles.
    - Figures and tables count.

    Parameters
    ----------
    document:
        The parsed document.
    max_tokens:
        Maximum estimated tokens for the summary.  The output is
        truncated to this limit.

    Returns
    -------
    str
        A plain-text summary suitable for inclusion in an LLM prompt.
    """
    parts: list[str] = []

    # Title & authors
    if document.title:
        parts.append(f"TITLE: {document.title}")
    if document.authors:
        parts.append(f"AUTHORS: {', '.join(document.authors)}")

    # Abstract (truncated)
    if document.abstract:
        abstract_preview = truncate_to_token_limit(document.abstract, 300)
        parts.append(f"ABSTRACT: {abstract_preview}")

    # Section outline
    parts.append("")
    parts.append("SECTION OUTLINE:")

    def _outline_section(sec: Section, indent: int = 0) -> list[str]:
        prefix = "  " * indent
        para_count = len(sec.paragraphs)
        sent_count = sum(len(p.sentences) for p in sec.paragraphs)
        lines = [f"{prefix}- [{sec.id}] {sec.title} ({para_count} paragraphs, {sent_count} sentences)"]
        for sub in sec.subsections:
            lines.extend(_outline_section(sub, indent + 1))
        return lines

    for section in document.sections:
        parts.extend(_outline_section(section))

    # References summary
    ref_count = len(document.references)
    parts.append("")
    parts.append(f"REFERENCES: {ref_count} total")
    if document.references:
        preview_count = min(5, ref_count)
        for ref in document.references[:preview_count]:
            title_str = ref.title or ref.raw_text[:80]
            parts.append(f"  - {title_str}")
        if ref_count > preview_count:
            parts.append(f"  ... and {ref_count - preview_count} more")

    # Figures & tables
    fig_count = sum(1 for ft in document.figures_tables if ft.ref_type == "figure")
    tbl_count = sum(1 for ft in document.figures_tables if ft.ref_type == "table")
    if fig_count or tbl_count:
        parts.append(f"FIGURES: {fig_count}  |  TABLES: {tbl_count}")

    # Source info
    if document.source_type:
        parts.append(f"SOURCE: {document.source_type}")

    summary = "\n".join(parts)
    return truncate_to_token_limit(summary, max_tokens)
