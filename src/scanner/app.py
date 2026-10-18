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
# The switcher is drawn in the page body, not Streamlit's header navigation: the scanner page pulls its content up
# under the fixed header (padding-top override), which hid the header links on that page.
current = st.navigation(PAGES, position="hidden")
with st.container(horizontal=True, gap="small", key="page_switcher"):
    for page in PAGES:
        st.page_link(page, width="content")
current.run()
