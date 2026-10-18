"""Streamlit entry point: a navigation shell with two pages.

- Scanner (scanner_page.py): the technical opportunity scan.
- Markets (markets/page.py): trends, levels and breakouts, relative rotation and rolling PCA on the lists in
  config/markets.yaml, refreshed separately from scans.

Run from the repository root:  uv run streamlit run src/scanner/app.py
"""

from __future__ import annotations

import streamlit as st

from scanner import scanner_page
from scanner.markets import page as markets_page

st.set_page_config(page_title="Trading workflow", page_icon=":material/candlestick_chart:", layout="wide",
                   initial_sidebar_state="collapsed")
PAGES = [
    st.Page(scanner_page.render, title="Scanner", icon=":material/radar:", url_path="scanner", default=True),
    st.Page(markets_page.render, title="Markets", icon=":material/monitoring:", url_path="markets"),
]
# One top padding for every page, so switching pages never moves the layout. Streamlit's default (6rem) leaves a gap;
# anything under the fixed header's height puts the first row (the page switcher) behind it, and the deployed
# header is opaque, so the switcher disappears. Pages read the height from the variable (e.g. for sticky offsets).
LAYOUT_CSS = """<style>
:root { --app-header-height: 3.75rem; }
[data-testid="stMainBlockContainer"] { padding-top: calc(var(--app-header-height) + 0.75rem); }
</style>"""
st.html(LAYOUT_CSS)
# The switcher is drawn in the page body, not in Streamlit's header navigation, so it sits just below the header.
current = st.navigation(PAGES, position="hidden")
with st.container(horizontal=True, gap="small", key="page_switcher"):
    for page in PAGES:
        st.page_link(page, width="content")
current.run()
