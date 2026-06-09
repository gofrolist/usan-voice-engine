"""Shared prompt-value sanitization utility.

This module is deliberately small and has no usan_agent sibling imports, so
both ``check_in`` and ``prompt_vars`` can import it without creating a
circular dependency.  The dependency graph is a clean DAG:

    sanitize  <--  check_in
    sanitize  <--  prompt_vars
"""

import re
from typing import Any

# Control characters and the format-slot braces are stripped from any
# API-supplied string before it reaches an LLM prompt (design spec §3).
# Removing "{" / "}" closes both a prompt-injection vector and an str.format
# KeyError/IndexError on attacker-controlled slots.
_PROMPT_UNSAFE = re.compile(
    # format-slot braces; ASCII control chars; the Unicode line/paragraph separators
    # NEL (U+0085), LS (U+2028), PS (U+2029); and invisible/directional chars
    # (zero-width, bidi overrides) that could smuggle instructions or new lines past
    # the LLM. Separators are listed explicitly so the regex alone suffices and does
    # not silently rely on the later str.split() to drop them.
    r"[{}\x00-\x1f\x7f\x85­​-‏  ‪-‮⁠-⁤﻿]"
)


def sanitize_prompt_value(value: Any, *, max_len: int) -> str:
    """Neutralize an API-supplied string for safe interpolation into LLM instructions.

    Strips format-slot braces and control characters (including newlines),
    collapses surrounding whitespace, and caps the length so a hostile value
    can neither inject new instructions nor introduce ``str.format`` slots.
    """
    text = _PROMPT_UNSAFE.sub(" ", str(value))
    text = " ".join(text.split())
    return text[:max_len].strip()
