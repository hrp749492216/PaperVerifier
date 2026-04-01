"""Robust JSON parsing fallback chain for LLM outputs.

LLM responses frequently arrive wrapped in markdown code fences, with
trailing commas, single-quoted keys, or truncated at a token limit.  This
module provides :func:`parse_llm_json`, a multi-strategy parser that
gracefully handles all of these quirks.

Addresses issue H15 (LLM JSON parsing failures).
"""

from __future__ import annotations

import json
import re
from typing import Any

import json5
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JSONParseError(Exception):
    """Raised when all JSON parsing strategies have been exhausted."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_llm_json(
    raw: str,
    *,
    expect_array: bool = True,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Parse JSON from LLM output using a multi-layer fallback chain.

    Attempts each strategy in order, returning the first successful result:

    1. **Direct parse** -- ``json.loads(raw)``.
    2. **Strip code fences** -- remove ````` ```json ... ``` ````` wrappers and
       retry with ``json.loads``.
    3. **Regex extraction** -- find the largest ``[...]`` or ``{...}`` block
       in the text and retry with ``json.loads``.
    4. **json5** -- handles trailing commas, single quotes, unquoted keys,
       and other JS-style extensions.
    5. **Truncation repair** -- attempt to close unmatched brackets / braces
       and retry with ``json5.loads``.

    When *expect_array* is ``True`` and the parsed result is a ``dict``,
    it is automatically wrapped in a list.

    Args:
        raw: The raw string output from an LLM.
        expect_array: If ``True``, ensure the return type is a ``list``.

    Returns:
        The parsed JSON as a ``list[dict]`` or ``dict``.

    Raises:
        JSONParseError: If all strategies fail, with details of each
            attempt.
    """
    if not raw or not isinstance(raw, str):
        raise JSONParseError("Input is empty or not a string.")

    errors: list[str] = []

    # --- Strategy 1: direct parse ---
    result = _try_parse(raw, "json.loads(raw)", errors)
    if result is not None:
        return _maybe_wrap(result, expect_array)

    # --- Strategy 2: strip markdown code fences ---
    stripped = _strip_code_fences(raw)
    if stripped != raw:
        result = _try_parse(stripped, "strip_code_fences + json.loads", errors)
        if result is not None:
            return _maybe_wrap(result, expect_array)

    # --- Strategy 3: truncation repair on stripped text ---
    # Try this early so truncated arrays/objects are fixed before we fall
    # back to extracting a smaller (but complete) sub-block.
    fixed_stripped = _fix_truncated_json(stripped)
    if fixed_stripped != stripped:
        result = _try_parse(fixed_stripped, "fix_truncated + json.loads", errors)
        if result is not None:
            logger.info("json_parsed_after_repair", strategy="fix_truncated(stripped)")
            return _maybe_wrap(result, expect_array)

    # --- Strategy 4: regex extract largest JSON block ---
    extracted = _extract_json_block(stripped)
    if extracted and extracted != stripped:
        result = _try_parse(extracted, "regex_extract + json.loads", errors)
        if result is not None:
            return _maybe_wrap(result, expect_array)

    # --- Strategy 5: json5 (lenient parser) ---
    # Try json5 on the best candidates: stripped text, extracted block,
    # and truncation-repaired versions.
    for candidate, label in [
        (stripped, "json5.loads(stripped)"),
        (extracted, "json5.loads(extracted)"),
        (fixed_stripped, "json5.loads(fixed_stripped)"),
    ]:
        if candidate:
            result = _try_json5(candidate, label, errors)
            if result is not None:
                return _maybe_wrap(result, expect_array)

    # --- Strategy 6: truncation repair on extracted block + json5 ---
    if extracted and extracted != stripped:
        fixed_extracted = _fix_truncated_json(extracted)
        if fixed_extracted != extracted:
            result = _try_json5(fixed_extracted, "fix_truncated(extracted) + json5", errors)
            if result is not None:
                logger.info("json_parsed_after_repair", strategy="fix_truncated(extracted)")
                return _maybe_wrap(result, expect_array)

    # All strategies exhausted.
    preview = raw[:200] + ("..." if len(raw) > 200 else "")
    error_detail = "; ".join(errors)
    logger.error(
        "json_parse_failed",
        input_preview=preview,
        strategies_tried=len(errors),
    )
    raise JSONParseError(
        f"Failed to parse JSON after {len(errors)} attempts. "
        f"Errors: [{error_detail}]. "
        f"Input preview: {preview}"
    )


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fence wrappers.

    Handles both ````` ```json\\n...\\n``` ````` and bare ````` ```\\n...\\n``` `````.
    If multiple fenced blocks exist, the first one is used.
    """
    pattern = r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_block(text: str) -> str:
    """Extract the largest JSON array or object from *text* using bracket matching.

    Scans for the outermost ``[...]`` or ``{...}`` block, handling nested
    brackets and quoted strings correctly.  Returns the extracted
    substring, or the original *text* if no block is found.
    """
    best_start = -1
    best_end = -1
    best_len = 0

    for open_char, close_char in [("[", "]"), ("{", "}")]:
        start = text.find(open_char)
        if start == -1:
            continue

        depth = 0
        in_string = False
        escape_next = False
        end = -1

        for i in range(start, len(text)):
            ch = text[i]

            if escape_next:
                escape_next = False
                continue

            if ch == "\\":
                if in_string:
                    escape_next = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end != -1 and (end - start) > best_len:
            best_start = start
            best_end = end
            best_len = end - start

    if best_start != -1:
        return text[best_start:best_end]

    return text


def _fix_truncated_json(text: str) -> str:
    """Attempt to fix truncated JSON by closing unclosed brackets and braces.

    This handles the common case where an LLM response is cut off mid-output
    due to a token limit, leaving open ``[`` or ``{`` without their matching
    closers.
    """
    text = text.rstrip()

    # Remove a trailing comma which is common before truncation.
    if text.endswith(","):
        text = text[:-1]

    # Count unmatched brackets/braces, respecting strings.
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue

        if ch == "\\":
            if in_string:
                escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()

    if not stack:
        return text

    # If we're inside a string at EOF, close it.
    if in_string:
        text += '"'

    # Append closing characters in reverse order.
    closers = {"[": "]", "{": "}"}
    for bracket in reversed(stack):
        text += closers[bracket]

    return text


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _try_parse(text: str, strategy: str, errors: list[str]) -> Any | None:
    """Attempt ``json.loads`` and record failures."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        errors.append(f"{strategy}: {exc}")
        return None


def _try_json5(text: str, strategy: str, errors: list[str]) -> Any | None:
    """Attempt ``json5.loads`` and record failures."""
    try:
        return json5.loads(text)
    except (ValueError, TypeError, OverflowError) as exc:
        errors.append(f"{strategy}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{strategy}: {type(exc).__name__}: {exc}")
        return None


def _maybe_wrap(
    result: Any,
    expect_array: bool,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Wrap *result* in a list if *expect_array* is True and result is a dict."""
    if expect_array and isinstance(result, dict):
        return [result]
    return result
