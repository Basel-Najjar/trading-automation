"""Streamlit helpers shared by the Scanner and Markets pages."""

from __future__ import annotations

import streamlit as st

from scanner.config import Issue, error_issues

ROW_PX, HEADER_PX = 35, 38  # st.dataframe row and header heights


def issues_md(issues: list[Issue]) -> str:
    return "\n".join(f"- {i}" for i in issues)


def show_errors(title: str, issues: list[Issue]) -> bool:
    """Show the error-level issues under `title`; True if there were any."""
    errors = error_issues(issues)
    if errors:
        st.error(f"{title}\n\n" + issues_md(errors))
    return bool(errors)


def table_height(n: int, max_rows: int) -> int:
    """Pixel height of a dataframe showing `n` rows, scrolling beyond `max_rows`."""
    return min(n, max_rows) * ROW_PX + HEADER_PX


def picked_rows(key: str) -> list[int]:
    """Row indices selected in the dataframe with widget `key` (empty if none)."""
    return ((st.session_state.get(key) or {}).get("selection") or {}).get("rows") or []
