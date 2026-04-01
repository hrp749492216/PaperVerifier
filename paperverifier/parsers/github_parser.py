"""GitHub repository parser.

Clones a GitHub repository using the security sandbox, identifies the
paper file(s) within it, and delegates to the appropriate parser.

Supports multi-file LaTeX projects (resolves ``\\input{}`` directives)
and a ``.paperverifier.yml`` configuration file for explicit file
specification.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from paperverifier.models.document import ParsedDocument
from paperverifier.parsers.base import BaseParser
from paperverifier.security.input_validator import (
    InputValidationError,
    validate_github_url,
)
from paperverifier.security.sandbox import (
    cleanup_temp_dir,
    clone_github_repo,
)

logger = structlog.get_logger(__name__)

# Priority filenames for paper identification.
_MAIN_TEX_NAMES = {"main.tex", "paper.tex", "manuscript.tex", "article.tex"}
_MAIN_PDF_NAMES = {"paper.pdf", "manuscript.pdf", "main.pdf", "article.pdf"}

# Minimum word count for a README to be treated as a paper.
_README_MIN_WORDS = 1000


class GitHubParser(BaseParser):
    """Parse a paper from a GitHub repository.

    Paper file identification heuristics (in priority order):

    1. ``.paperverifier.yml`` config in repo root (``paper_file`` key).
    2. Files named ``main.tex``, ``paper.tex``, ``manuscript.tex``.
    3. ``.tex`` files containing ``\\documentclass``.
    4. PDF files (prefer ``paper.pdf``, ``manuscript.pdf``).
    5. ``.md`` files that are not README, or README with > 1000 words.

    Delegates to the appropriate parser once the paper file is found.
    """

    async def parse(self, source: str | bytes, **kwargs: object) -> ParsedDocument:
        """Clone a GitHub repository and parse the paper within.

        Args:
            source: A GitHub repository URL
                (``https://github.com/owner/repo``).
            **kwargs: Passed through to the delegate parser.

        Returns:
            A fully populated :class:`ParsedDocument`.

        Raises:
            InputValidationError: If the URL is invalid.
            SandboxError: If cloning fails.
            RuntimeError: If no paper file is found in the repository.
        """
        if not isinstance(source, str):
            raise InputValidationError("GitHub parser requires a URL string as source.")

        # Validate the GitHub URL.
        validated_url = validate_github_url(source)

        # Clone into a temporary directory.
        clone_dir: Path | None = None
        try:
            clone_dir = await clone_github_repo(validated_url)
            logger.info("github_repo_cloned", url=validated_url, path=str(clone_dir))

            # Identify the paper file.
            paper_path, parser = await self._identify_paper(clone_dir)
            logger.info(
                "paper_file_identified",
                file=str(paper_path),
                parser=type(parser).__name__,
            )

            # Parse the identified file.
            result = await parser.parse(
                str(paper_path),
                allowed_dir=clone_dir,
                base_dir=paper_path.parent,
                **kwargs,
            )

            # Update source metadata.
            result.source_type = "github"
            result.source_path = validated_url
            result.metadata["github_url"] = validated_url
            result.metadata["paper_file"] = str(paper_path.relative_to(clone_dir))

            return result

        finally:
            # Always clean up the temp directory.
            if clone_dir is not None:
                parent = clone_dir.parent
                cleanup_temp_dir(parent)
                logger.debug("github_temp_cleaned", path=str(parent))

    # ------------------------------------------------------------------
    # Paper identification
    # ------------------------------------------------------------------

    async def _identify_paper(self, clone_dir: Path) -> tuple[Path, BaseParser]:
        """Identify the paper file and return the appropriate parser.

        Returns:
            A tuple of ``(paper_path, parser_instance)``.

        Raises:
            RuntimeError: If no paper file can be identified.
        """
        # Strategy 1: .paperverifier.yml config.
        config_result = self._check_config(clone_dir)
        if config_result is not None:
            return config_result

        # Strategy 2: Well-known .tex filenames.
        for name in _MAIN_TEX_NAMES:
            candidate = clone_dir / name
            if candidate.exists():
                from paperverifier.parsers.latex_parser import LaTeXParser

                return candidate, LaTeXParser()

        # Strategy 3: .tex files containing \documentclass.
        tex_files = list(clone_dir.rglob("*.tex"))
        for tex_file in tex_files:
            try:
                content = tex_file.read_text(encoding="utf-8", errors="replace")
                if r"\documentclass" in content:
                    from paperverifier.parsers.latex_parser import LaTeXParser

                    logger.debug("tex_documentclass_found", file=str(tex_file))
                    return tex_file, LaTeXParser()
            except OSError:
                continue

        # Strategy 4: PDF files with priority names.
        pdf_files = list(clone_dir.rglob("*.pdf"))
        for name in _MAIN_PDF_NAMES:
            for pdf in pdf_files:
                if pdf.name.lower() == name:
                    from paperverifier.parsers.pdf_parser import PDFParser

                    return pdf, PDFParser()
        # Any PDF file.
        if pdf_files:
            from paperverifier.parsers.pdf_parser import PDFParser

            return pdf_files[0], PDFParser()

        # Strategy 5: Markdown files (non-README or large README).
        md_files = list(clone_dir.rglob("*.md"))
        non_readme_md = [f for f in md_files if not f.name.lower().startswith("readme")]
        if non_readme_md:
            from paperverifier.parsers.markdown_parser import MarkdownParser

            return non_readme_md[0], MarkdownParser()

        # Check README files that are large enough to be papers.
        for md in md_files:
            if md.name.lower().startswith("readme"):
                try:
                    content = md.read_text(encoding="utf-8", errors="replace")
                    word_count = len(content.split())
                    if word_count > _README_MIN_WORDS:
                        from paperverifier.parsers.markdown_parser import (
                            MarkdownParser,
                        )

                        logger.info(
                            "readme_as_paper",
                            file=str(md),
                            words=word_count,
                        )
                        return md, MarkdownParser()
                except OSError:
                    continue

        # Strategy 6: .txt files.
        txt_files = list(clone_dir.rglob("*.txt"))
        txt_files = [
            f
            for f in txt_files
            if not f.name.lower().startswith("license") and not f.name.lower().startswith("readme")
        ]
        if txt_files:
            from paperverifier.parsers.text_parser import TextParser

            return txt_files[0], TextParser()

        raise RuntimeError(
            "No paper file found in repository. "
            "Searched for: .tex, .pdf, .md, .txt files. "
            "Consider adding a .paperverifier.yml config to specify the paper file."
        )

    # ------------------------------------------------------------------
    # Config file parsing
    # ------------------------------------------------------------------

    def _check_config(self, clone_dir: Path) -> tuple[Path, BaseParser] | None:
        """Check for a ``.paperverifier.yml`` config file.

        Expected format::

            paper_file: path/to/paper.tex

        Returns:
            ``(paper_path, parser)`` or ``None`` if no config found.
        """
        config_path = clone_dir / ".paperverifier.yml"
        if not config_path.exists():
            return None

        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "config_read_error",
                path=str(config_path),
                error=str(exc),
            )
            return None

        # Simple YAML parsing for the paper_file key.
        match = re.search(r"paper_file\s*:\s*(.+)", content)
        if not match:
            logger.warning("config_missing_paper_file", path=str(config_path))
            return None

        relative_path = match.group(1).strip().strip('"').strip("'")
        paper_path = (clone_dir / relative_path).resolve()

        # Security: ensure the path is within the clone directory.
        try:
            paper_path.relative_to(clone_dir.resolve())
        except ValueError:
            logger.warning(
                "config_path_traversal",
                paper_file=relative_path,
            )
            return None

        if not paper_path.exists():
            logger.warning(
                "config_paper_not_found",
                paper_file=relative_path,
            )
            return None

        parser = self._parser_for_extension(paper_path.suffix.lower())
        if parser is None:
            logger.warning(
                "config_unsupported_extension",
                extension=paper_path.suffix,
            )
            return None

        logger.info("config_paper_file", file=str(paper_path))
        return paper_path, parser

    @staticmethod
    def _parser_for_extension(ext: str) -> BaseParser | None:
        """Return the appropriate parser for a file extension."""
        # Deferred imports to avoid circular dependencies.
        if ext == ".tex":
            from paperverifier.parsers.latex_parser import LaTeXParser

            return LaTeXParser()
        elif ext == ".pdf":
            from paperverifier.parsers.pdf_parser import PDFParser

            return PDFParser()
        elif ext == ".md":
            from paperverifier.parsers.markdown_parser import MarkdownParser

            return MarkdownParser()
        elif ext == ".docx":
            from paperverifier.parsers.docx_parser import DOCXParser

            return DOCXParser()
        elif ext in (".txt", ".text"):
            from paperverifier.parsers.text_parser import TextParser

            return TextParser()
        return None
