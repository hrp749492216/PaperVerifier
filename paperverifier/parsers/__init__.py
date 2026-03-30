"""Document parser pipeline.

Exports the :class:`InputRouter` for automatic format detection and all
individual parser classes for direct use.

Quick start::

    from paperverifier.parsers import InputRouter

    router = InputRouter()
    doc = await router.parse("paper.pdf")
    doc = await router.parse("https://arxiv.org/abs/2301.12345")
    doc = await router.parse("https://github.com/user/paper-repo")
"""

from __future__ import annotations

from paperverifier.parsers.base import BaseParser
from paperverifier.parsers.docx_parser import DOCXParser
from paperverifier.parsers.github_parser import GitHubParser
from paperverifier.parsers.latex_parser import LaTeXParser
from paperverifier.parsers.markdown_parser import MarkdownParser
from paperverifier.parsers.pdf_parser import PDFParser
from paperverifier.parsers.router import InputRouter
from paperverifier.parsers.text_parser import TextParser
from paperverifier.parsers.url_parser import URLParser

__all__ = [
    "BaseParser",
    "DOCXParser",
    "GitHubParser",
    "InputRouter",
    "LaTeXParser",
    "MarkdownParser",
    "PDFParser",
    "TextParser",
    "URLParser",
]
