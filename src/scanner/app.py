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
st.navigation([
    st.Page(scanner_page.render, title="Scanner", icon=":material/radar:", url_path="scanner", default=True),
    st.Page(markets_page.render, title="Markets", icon=":material/monitoring:", url_path="markets"),
], position="top").run()
