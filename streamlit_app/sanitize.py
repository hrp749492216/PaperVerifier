"""Output sanitization for rendering LLM-generated content in Streamlit.

Strips potentially dangerous HTML/script content from LLM responses
before rendering via ``st.markdown``.
"""

from __future__ import annotations

import re


def sanitize_markdown(text: str) -> str:
    """Remove dangerous HTML from text that will be rendered as Markdown.

    Strips ``<script>``, ``<iframe>``, ``<object>``, ``<embed>``, ``<form>``,
    ``<input>`` tags and ``javascript:`` URLs that could cause XSS when
    rendered by Streamlit's ``st.markdown(unsafe_allow_html=True)`` or
    if Streamlit ever relaxes its HTML sanitization.
    """
    # Remove script tags and content
    text = re.sub(r"<script[\s>].*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    # Remove dangerous tags (self-closing or open)
    text = re.sub(
        r"<\s*/?\s*(script|iframe|object|embed|form|input|link|meta)\b[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Remove javascript: URLs
    text = re.sub(r"javascript\s*:", "javascript&#58;", text, flags=re.IGNORECASE)
    # Remove on* event handlers in any remaining HTML
    text = re.sub(r"\bon\w+\s*=", "", text, flags=re.IGNORECASE)
    return text
