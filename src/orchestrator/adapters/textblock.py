# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Managed text-block helpers for Markdown, TOML, and plain-text files."""

from __future__ import annotations

import re

from ..branding import MANAGED_BLOCK_TAG


def _marker_lines(
    *,
    comment_prefix: str,
    marker_suffix: str,
    tag: str,
) -> tuple[str, str]:
    suffix = f" {marker_suffix}" if marker_suffix else ""
    return (
        f"{comment_prefix} >>> {tag} managed block >>>{suffix}",
        f"{comment_prefix} <<< {tag} managed block <<<{suffix}",
    )


def _block_pattern(
    *,
    comment_prefix: str,
    marker_suffix: str,
    tag: str,
) -> re.Pattern[str]:
    start, end = _marker_lines(
        comment_prefix=comment_prefix,
        marker_suffix=marker_suffix,
        tag=tag,
    )
    return re.compile(
        rf"^[ \t]*{re.escape(start)}[ \t]*\r?\n"
        rf".*?"
        rf"^[ \t]*{re.escape(end)}[ \t]*(?:\r?\n|$)",
        re.DOTALL | re.MULTILINE,
    )


def _assert_balanced_markers(existing_text: str, start: str, end: str) -> None:
    """Refuse to edit a file whose marker lines are missing or duplicated.

    A lone start marker (no matching end) or more than one occurrence of
    either marker means the file was hand-edited or corrupted in a way that
    makes the "owned block" boundary ambiguous; guessing would risk silently
    swallowing or duplicating unrelated user content.
    """

    start_count = existing_text.count(start)
    end_count = existing_text.count(end)
    if start_count != end_count or start_count > 1:
        raise ValueError(
            "Managed block markers are missing or duplicated "
            f"(found {start_count} start marker(s), {end_count} end "
            "marker(s)); refusing to guess which region is owned."
        )


def render_block(
    content: str,
    comment_prefix: str = "#",
    tag: str = MANAGED_BLOCK_TAG,
    *,
    marker_suffix: str = "",
) -> str:
    """Wrap content in a stable, explicitly-owned managed block.

    Use ``comment_prefix="<!--", marker_suffix="-->"`` for Markdown files
    and the defaults for TOML or hash-commented plain text.
    """

    start, end = _marker_lines(
        comment_prefix=comment_prefix,
        marker_suffix=marker_suffix,
        tag=tag,
    )
    body = content.rstrip("\r\n")
    return f"{start}\n{body}\n{end}\n"


def upsert_block(
    existing_text: str,
    content: str,
    *,
    comment_prefix: str = "#",
    tag: str = MANAGED_BLOCK_TAG,
    marker_suffix: str = "",
) -> tuple[str, bool]:
    """Replace an owned block, or append a new one without touching other text."""

    first_newline = re.search(r"\r?\n", existing_text)
    newline = first_newline.group() if first_newline else "\n"
    rendered = render_block(
        content,
        comment_prefix=comment_prefix,
        marker_suffix=marker_suffix,
        tag=tag,
    )
    # Templates use LF; the destination owns its existing newline convention.
    # Only our replacement block is normalized; unrelated bytes stay intact.
    rendered = rendered.replace("\r\n", "\n").replace("\n", newline)
    start, end = _marker_lines(comment_prefix=comment_prefix, marker_suffix=marker_suffix, tag=tag)
    _assert_balanced_markers(existing_text, start, end)
    pattern = _block_pattern(
        comment_prefix=comment_prefix,
        marker_suffix=marker_suffix,
        tag=tag,
    )
    if pattern.search(existing_text):
        # A replacement function, not a raw string, avoids re.sub's backslash
        # escape/backreference processing of `rendered` (which may contain
        # arbitrary content, including literal backslashes in Windows paths).
        new_text = pattern.sub(lambda _match: rendered, existing_text, count=1)
    elif not existing_text:
        new_text = rendered
    elif re.search(r"(?:\r?\n){2}$", existing_text):
        new_text = f"{existing_text}{rendered}"
    elif existing_text.endswith(("\n", "\r")):
        new_text = f"{existing_text}{newline}{rendered}"
    else:
        new_text = f"{existing_text}{newline}{newline}{rendered}"
    return new_text, new_text != existing_text


def remove_block(
    existing_text: str,
    *,
    comment_prefix: str = "#",
    tag: str = MANAGED_BLOCK_TAG,
    marker_suffix: str = "",
) -> tuple[str, bool]:
    """Remove one owned block, including its marker lines, when present."""

    start, end = _marker_lines(comment_prefix=comment_prefix, marker_suffix=marker_suffix, tag=tag)
    _assert_balanced_markers(existing_text, start, end)
    pattern = _block_pattern(
        comment_prefix=comment_prefix,
        marker_suffix=marker_suffix,
        tag=tag,
    )
    new_text = pattern.sub("", existing_text, count=1)
    return new_text, new_text != existing_text
