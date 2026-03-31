"""LaTeX document parser.

Two-strategy approach:
1. **Primary**: Direct regex parsing of LaTeX commands (``\\section{}``,
   ``\\subsection{}``, ``\\cite{}``, ``\\ref{}``, ``\\input{}``, etc.).
2. **Fallback**: ``pypandoc`` conversion if installed.

Never calls ``pypandoc.download_pandoc()`` in production.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import structlog

from paperverifier.models.document import (
    ParsedDocument,
    Reference,
    Section,
)
from paperverifier.parsers.base import BaseParser
from paperverifier.security.input_validator import (
    InputValidationError,
    validate_file_path,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# LaTeX command patterns
# ---------------------------------------------------------------------------

# Section commands and their levels.
_SECTION_COMMANDS = {
    "part": 0,
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}

# Pattern for \section{...}, \subsection{...}, etc.
_SECTION_RE = re.compile(
    r"\\((?:sub)*(?:section|paragraph)|part|chapter)\*?\{([^}]+)\}",
)

# Pattern for \input{file} and \include{file}.
_INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")

# Pattern for \title{...}.
_TITLE_RE = re.compile(r"\\title\{([^}]+)\}")

# Pattern for \author{...} -- may span multiple lines.
_AUTHOR_RE = re.compile(r"\\author\{([^}]+)\}", re.DOTALL)

# Pattern for abstract environment.
_ABSTRACT_RE = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)

# Pattern for \cite{}, \citep{}, \citet{}, \citeauthor{}, etc.
_CITE_RE = re.compile(r"\\cite[a-z]*\{([^}]+)\}")

# Pattern for \ref{} and \label{}.
_REF_RE = re.compile(r"\\(?:ref|eqref|pageref)\{([^}]+)\}")
_LABEL_RE = re.compile(r"\\label\{([^}]+)\}")

# Pattern for bibliography entries: \bibitem{key} text...
_BIBITEM_RE = re.compile(
    r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}\s*(.*?)(?=\\bibitem|\\end\{thebibliography\}|$)",
    re.DOTALL,
)

# Common LaTeX commands to strip for clean text.
_COMMAND_STRIP_RE = re.compile(
    r"\\(?:textbf|textit|emph|underline|textsc|textrm|textsf|texttt)\{([^}]*)\}"
)
_SIMPLE_COMMANDS_RE = re.compile(
    r"\\(?:noindent|newline|clearpage|newpage|maketitle|tableofcontents"
    r"|bibliographystyle\{[^}]*\}|bibliography\{[^}]*\})\s*"
)
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_ENV_STRIP_RE = re.compile(r"\\(?:begin|end)\{(?:document|itemize|enumerate|description)\}")


class LaTeXParser(BaseParser):
    """Parse LaTeX documents into :class:`ParsedDocument`.

    Handles ``\\input{}``/``\\include{}`` directives by resolving
    relative paths from the main file's directory.
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Parse a LaTeX file from a path, bytes, or raw string.

        Args:
            source: File path (str), raw LaTeX string, or UTF-8 bytes.
            **kwargs: Optional ``allowed_dir`` (:class:`Path`) and
                ``base_dir`` (:class:`Path`) for resolving ``\\input{}``.

        Returns:
            A fully populated :class:`ParsedDocument`.
        """
        source_path = ""
        base_dir: Path | None = kwargs.get("base_dir")  # type: ignore[assignment]

        if isinstance(source, bytes):
            text = source.decode("utf-8", errors="replace")
        elif isinstance(source, str) and (
            "\\" in source and ("{" in source or "begin" in source)
        ):
            # Looks like raw LaTeX, not a path.
            text = source
        else:
            # Treat as file path.
            path = Path(source)
            allowed_dir = kwargs.get("allowed_dir")
            if allowed_dir is not None:
                validate_file_path(path, Path(allowed_dir))  # type: ignore[arg-type]

            if not path.exists():
                raise InputValidationError(f"LaTeX file not found: {source}")
            text = path.read_text(encoding="utf-8", errors="replace")
            source_path = str(path)
            if base_dir is None:
                base_dir = path.parent

        # Resolve \input{} and \include{} directives.
        if base_dir is not None:
            text = self._resolve_inputs(text, base_dir, depth=0)

        # Try direct regex parsing first.
        result = self._parse_latex_direct(text, source_path)
        if result is not None:
            return result

        # Fallback: try pypandoc if available.
        result = self._try_pypandoc(text, source_path)
        if result is not None:
            return result

        # Last resort: treat as plain text with section detection.
        logger.warning("latex_fallback_plain_text", source=source_path)
        return self._parse_as_plain(text, source_path)

    # ------------------------------------------------------------------
    # Direct regex parsing (primary strategy)
    # ------------------------------------------------------------------

    def _parse_latex_direct(
        self, text: str, source_path: str
    ) -> ParsedDocument | None:
        """Parse LaTeX using regex-based extraction."""
        # Extract metadata.
        title = self._extract_latex_field(_TITLE_RE, text)
        authors = self._extract_authors(text)
        abstract = self._extract_abstract(text)

        # Strip comments for cleaner parsing.
        clean_text = _COMMENT_RE.sub("", text)

        # Extract the document body (between \begin{document} and \end{document}).
        body_match = re.search(
            r"\\begin\{document\}(.*?)\\end\{document\}",
            clean_text,
            re.DOTALL,
        )
        body = body_match.group(1) if body_match else clean_text

        # Parse sections.
        sections = self._parse_sections(body)

        if not sections:
            # No sections found -- wrap body in a single section.
            cleaned_body = self._clean_latex(body)
            sections = [
                self._build_section(
                    section_id="sec-1",
                    title="Document",
                    text=cleaned_body,
                    level=1,
                    start_char=0,
                )
            ]

        # Extract references from bibliography.
        references = self._extract_bib_references(text)
        # Also extract inline \cite{} references.
        cite_refs = self._extract_references_regex(text)
        # Merge: add cite refs whose keys don't already appear.
        existing_keys = {r.citation_key for r in references if r.citation_key}
        for cr in cite_refs:
            if cr.citation_key and cr.citation_key not in existing_keys:
                references.append(cr)
                existing_keys.add(cr.citation_key)

        fig_table_refs = self._detect_figure_table_refs(body)

        # Build full text from cleaned body.
        full_text = self._clean_latex(body)

        return ParsedDocument(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            figures_tables=fig_table_refs,
            full_text=full_text,
            source_type="latex",
            source_path=source_path,
        )

    # ------------------------------------------------------------------
    # Section parsing
    # ------------------------------------------------------------------

    def _parse_sections(self, body: str) -> list[Section]:
        """Parse ``\\section{}``, ``\\subsection{}``, etc. into sections."""
        matches = list(_SECTION_RE.finditer(body))
        if not matches:
            return []

        sections: list[Section] = []

        for idx, match in enumerate(matches):
            cmd = match.group(1)
            title = match.group(2).strip()
            level = _SECTION_COMMANDS.get(cmd, 1)

            # Section body extends to the next section command.
            body_start = match.end()
            body_end = (
                matches[idx + 1].start()
                if idx + 1 < len(matches)
                else len(body)
            )
            section_body = body[body_start:body_end].strip()
            cleaned = self._clean_latex(section_body)

            section_id = f"sec-{idx + 1}"
            section = self._build_section(
                section_id=section_id,
                title=title,
                text=cleaned,
                level=level,
                start_char=match.start(),
            )
            sections.append(section)

        return sections

    # ------------------------------------------------------------------
    # Input/Include resolution
    # ------------------------------------------------------------------

    def _resolve_inputs(self, text: str, base_dir: Path, depth: int, root_dir: Path | None = None) -> str:
        """Recursively resolve ``\\input{}`` and ``\\include{}`` directives.

        Prevents infinite recursion with a depth limit of 10.

        Args:
            text: LaTeX source text.
            base_dir: Directory to resolve relative paths from.
            depth: Current recursion depth.
            root_dir: Original root directory for security validation.
                Preserved across recursive calls to prevent chained
                includes from escaping the project root.

        Returns:
            Text with ``\\input{}``/``\\include{}`` replaced by file contents.
        """
        if root_dir is None:
            root_dir = base_dir

        if depth > 10:
            logger.warning("latex_input_depth_exceeded", depth=depth)
            return text

        def _replace_input(match: re.Match[str]) -> str:
            filename = match.group(1).strip()
            # LaTeX \input omits .tex extension by convention.
            if not Path(filename).suffix:
                filename += ".tex"

            input_path = base_dir / filename
            resolved = input_path.resolve()

            # Security: ensure resolved path is within root_dir.
            try:
                resolved.relative_to(root_dir.resolve())
            except ValueError:
                logger.warning(
                    "latex_input_outside_basedir",
                    filename=filename,
                    resolved=str(resolved),
                )
                return f"% [PaperVerifier: skipped \\input{{{filename}}} -- outside project]"

            if not resolved.exists():
                logger.debug("latex_input_not_found", filename=filename)
                return f"% [PaperVerifier: \\input{{{filename}}} not found]"

            try:
                included = resolved.read_text(encoding="utf-8", errors="replace")
                return self._resolve_inputs(included, resolved.parent, depth + 1, root_dir=root_dir)
            except Exception as exc:
                logger.warning(
                    "latex_input_read_error",
                    filename=filename,
                    error=str(exc),
                )
                return f"% [PaperVerifier: error reading \\input{{{filename}}}]"

        return _INPUT_RE.sub(_replace_input, text)

    # ------------------------------------------------------------------
    # LaTeX text cleaning
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_latex(text: str) -> str:
        """Remove common LaTeX commands and produce readable plain text."""
        # Strip comments.
        result = _COMMENT_RE.sub("", text)

        # Unwrap formatting commands: \textbf{...} -> ...
        for _ in range(3):  # Handle nested commands.
            result = _COMMAND_STRIP_RE.sub(r"\1", result)

        # Remove simple commands.
        result = _SIMPLE_COMMANDS_RE.sub("", result)

        # Remove environment begin/end markers.
        result = _ENV_STRIP_RE.sub("", result)

        # Remove \label{} and \ref{} but keep surrounding text.
        result = re.sub(r"\\label\{[^}]*\}", "", result)

        # Replace \\ with newline.
        result = result.replace("\\\\", "\n")

        # Remove remaining backslash commands that are unlikely to be text.
        result = re.sub(r"\\[a-zA-Z]+(?:\[[^\]]*\])?\{([^}]*)\}", r"\1", result)

        # Remove lone backslash commands without arguments.
        result = re.sub(r"\\[a-zA-Z]+\s*", " ", result)

        # Clean up braces.
        result = result.replace("{", "").replace("}", "")

        # Collapse multiple blank lines.
        result = re.sub(r"\n{3,}", "\n\n", result)

        # Collapse multiple spaces.
        result = re.sub(r"[ \t]+", " ", result)

        return result.strip()

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_latex_field(pattern: re.Pattern[str], text: str) -> str | None:
        """Extract a single LaTeX field (e.g. ``\\title{...}``)."""
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            # Remove LaTeX formatting from the value.
            value = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", value)
            value = value.replace("{", "").replace("}", "").strip()
            return value if value else None
        return None

    @staticmethod
    def _extract_authors(text: str) -> list[str]:
        """Extract author names from ``\\author{...}``."""
        match = _AUTHOR_RE.search(text)
        if not match:
            return []

        raw = match.group(1)

        # Remove LaTeX commands within the author field.
        raw = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", raw)
        raw = re.sub(r"\\[a-zA-Z]+", "", raw)
        raw = raw.replace("{", "").replace("}", "")

        # Split on \and, commas, or newlines.
        parts = re.split(r"\\and\b|,|\n", raw)
        authors = [a.strip() for a in parts if a.strip() and len(a.strip()) > 1]
        return authors

    def _extract_abstract(self, text: str) -> str | None:
        """Extract the abstract environment content."""
        match = _ABSTRACT_RE.search(text)
        if match:
            abstract = self._clean_latex(match.group(1)).strip()
            abstract = re.sub(r"\s+", " ", abstract)
            if len(abstract) > 30:
                return abstract
        return None

    # ------------------------------------------------------------------
    # Bibliography extraction
    # ------------------------------------------------------------------

    def _extract_bib_references(self, text: str) -> list[Reference]:
        """Extract references from ``\\bibitem{}`` entries."""
        refs: list[Reference] = []

        for match in _BIBITEM_RE.finditer(text):
            key = match.group(1).strip()
            raw = match.group(2).strip()
            cleaned = self._clean_latex(raw)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()

            ref = Reference(
                raw_text=cleaned,
                citation_key=key,
                citation_style="bibtex",
            )

            # Try to extract year.
            year_match = re.search(r"\b((?:19|20)\d{2})\b", cleaned)
            if year_match:
                ref.year = int(year_match.group(1))

            # Try to extract DOI.
            doi_match = re.search(
                r"(?:doi:\s*|https?://doi\.org/)(10\.\d{4,}/\S+)",
                cleaned,
                re.IGNORECASE,
            )
            if doi_match:
                ref.doi = doi_match.group(1).rstrip(".")

            refs.append(ref)

        return refs

    # ------------------------------------------------------------------
    # pypandoc fallback
    # ------------------------------------------------------------------

    def _try_pypandoc(
        self, text: str, source_path: str
    ) -> ParsedDocument | None:
        """Try to convert LaTeX to plain text using pypandoc.

        Returns ``None`` if pypandoc or pandoc is not available.
        Never calls ``pypandoc.download_pandoc()``.
        """
        try:
            import pypandoc  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("pypandoc_not_available")
            return None

        # Guard against Pandoc versions vulnerable to CVE-2023-38745.
        try:
            version_str = pypandoc.get_pandoc_version()
            version_parts = tuple(int(x) for x in version_str.split(".")[:3])
            if version_parts < (3, 1, 6):
                logger.warning(
                    "pandoc_version_too_old",
                    version=version_str,
                    minimum="3.1.6",
                )
                return None
        except Exception:
            logger.debug("pandoc_version_check_failed")

        try:
            plain = pypandoc.convert_text(text, "plain", format="latex")
        except Exception as exc:
            logger.warning("pypandoc_conversion_failed", error=str(exc))
            return None

        # Build a simple document from the converted text.
        sections = [
            self._build_section(
                section_id="sec-1",
                title="Document",
                text=plain.strip(),
                level=1,
                start_char=0,
            )
        ]

        # Extract metadata from original LaTeX.
        title = self._extract_latex_field(_TITLE_RE, text)
        authors = self._extract_authors(text)
        abstract = self._extract_abstract(text)
        references = self._extract_bib_references(text)
        cite_refs = self._extract_references_regex(text)
        existing_keys = {r.citation_key for r in references if r.citation_key}
        for cr in cite_refs:
            if cr.citation_key and cr.citation_key not in existing_keys:
                references.append(cr)
                existing_keys.add(cr.citation_key)

        return ParsedDocument(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            full_text=plain.strip(),
            source_type="latex",
            source_path=source_path,
        )

    # ------------------------------------------------------------------
    # Plain text fallback
    # ------------------------------------------------------------------

    def _parse_as_plain(
        self, text: str, source_path: str
    ) -> ParsedDocument:
        """Last-resort fallback: clean and wrap as a single section."""
        cleaned = self._clean_latex(text)

        title = self._extract_latex_field(_TITLE_RE, text)
        authors = self._extract_authors(text)
        abstract = self._extract_abstract(text)
        references = self._extract_references_regex(text)

        sections = [
            self._build_section(
                section_id="sec-1",
                title=title or "Document",
                text=cleaned,
                level=1,
                start_char=0,
            )
        ]

        return ParsedDocument(
            title=title,
            authors=authors,
            abstract=abstract,
            sections=sections,
            references=references,
            full_text=cleaned,
            source_type="latex",
            source_path=source_path,
        )
