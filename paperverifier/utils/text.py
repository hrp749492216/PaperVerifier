"""Text utility functions for PaperVerifier.

Provides helpers for DOI extraction, title normalisation, similarity scoring,
token estimation, and text formatting used throughout the verification
pipeline.
"""

from __future__ import annotations

import re
import string
import unicodedata

# ---------------------------------------------------------------------------
# DOI extraction
# ---------------------------------------------------------------------------

# Matches DOI patterns like  10.1000/xyz123  or  https://doi.org/10.1000/xyz123
_DOI_RE = re.compile(
    r"""
    (?:https?://(?:dx\.)?doi\.org/)?   # optional resolver prefix
    (10\.\d{4,9}/[^\s,;)\]}"']+)       # capture the DOI itself
    """,
    re.VERBOSE,
)


def extract_dois(text: str) -> list[str]:
    """Extract all DOIs from *text* using regex.

    Handles bare DOIs (``10.1234/abc``) and URL-form DOIs
    (``https://doi.org/10.1234/abc``).  Trailing punctuation that is
    commonly part of surrounding prose (period, comma, semicolon) is
    stripped from each match.

    Parameters
    ----------
    text:
        Arbitrary text that may contain DOIs.

    Returns
    -------
    list[str]
        De-duplicated list of DOI strings in the order they first appear.
    """
    seen: set[str] = set()
    results: list[str] = []
    for match in _DOI_RE.finditer(text):
        doi = match.group(1).rstrip(".,;:)")
        if doi not in seen:
            seen.add(doi)
            results.append(doi)
    return results


# ---------------------------------------------------------------------------
# Title normalisation & similarity
# ---------------------------------------------------------------------------

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize_title(title: str) -> str:
    """Normalise a paper title for fuzzy comparison.

    Steps:
    1. Unicode NFKD normalisation (decompose accented characters).
    2. Lower-case.
    3. Strip all punctuation.
    4. Collapse runs of whitespace to a single space.
    5. Strip leading/trailing whitespace.

    Parameters
    ----------
    title:
        Raw title string.

    Returns
    -------
    str
        Normalised title.
    """
    title = unicodedata.normalize("NFKD", title)
    title = title.lower()
    title = title.translate(_PUNCT_TABLE)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def title_similarity(a: str, b: str) -> float:
    """Compute word-level Jaccard similarity between two titles.

    Both titles are normalised via :func:`normalize_title` before comparison.

    Parameters
    ----------
    a:
        First title.
    b:
        Second title.

    Returns
    -------
    float
        Similarity score in ``[0.0, 1.0]``.  ``1.0`` means the titles
        contain exactly the same set of words.
    """
    words_a = set(normalize_title(a).split())
    words_b = set(normalize_title(b).split())
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Line-numbered text
# ---------------------------------------------------------------------------


def add_line_numbers(text: str) -> str:
    """Prepend 1-based line numbers to every line in *text*.

    The line number column width adapts to the total number of lines so
    that numbers are right-aligned.

    Parameters
    ----------
    text:
        Multi-line text.

    Returns
    -------
    str
        The same text with each line prefixed by its number and a pipe
        separator (e.g., ``  1 | some text``).
    """
    lines = text.splitlines()
    width = len(str(len(lines))) if lines else 1
    return "\n".join(f"{i + 1:>{width}} | {line}" for i, line in enumerate(lines))


# ---------------------------------------------------------------------------
# Abstract extraction
# ---------------------------------------------------------------------------

_ABSTRACT_RE = re.compile(
    r"""
    (?:^|\n)                          # start of text or new line
    \s*abstract\s*[:\-.\n]*\s*        # heading "Abstract" with optional punctuation
    ([\s\S]+?)                        # capture the body (non-greedy)
    (?=\n\s*(?:                       # lookahead for next major section heading
        introduction
        |keywords
        |1[\s.\t]+\w                  # numbered section "1 ..."
        |i[\s.\t]+\w                  # roman-numeral section "I ..."
    ))
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_abstract(text: str) -> str | None:
    """Attempt to extract the abstract from unstructured paper text.

    Uses a heuristic regex that looks for the word ``Abstract`` followed
    by body text up to the next recognisable section heading.

    Parameters
    ----------
    text:
        Full text of the paper.

    Returns
    -------
    str | None
        The extracted abstract text, or ``None`` if no abstract could be
        identified.
    """
    match = _ABSTRACT_RE.search(text)
    if match:
        abstract = match.group(1).strip()
        # Sanity: abstract should be between 50 and 5000 characters
        if 50 <= len(abstract) <= 5000:
            return abstract
    return None


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def count_tokens_estimate(text: str) -> int:
    """Return a rough token count estimate for *text*.

    Uses the widely-accepted heuristic of ~4 characters per token for
    English text, which is a reasonable approximation across most modern
    tokenisers (BPE / SentencePiece).

    Parameters
    ----------
    text:
        Input text.

    Returns
    -------
    int
        Estimated token count (always >= 0).
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def truncate_to_token_limit(text: str, max_tokens: int) -> str:
    """Truncate *text* so that it fits within *max_tokens* (estimated).

    Truncation is performed at a word boundary when possible to avoid
    cutting in the middle of a word.

    Parameters
    ----------
    text:
        Input text.
    max_tokens:
        Maximum number of estimated tokens to keep.

    Returns
    -------
    str
        Truncated text.  If the text already fits, it is returned
        unchanged.
    """
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    # Try to break at a word boundary within the last 20 characters
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ", max(0, max_chars - 20))
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated
