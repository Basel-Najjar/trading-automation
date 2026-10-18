"""Markets page: trends/levels/breakouts, relative rotation and rolling PCA on configurable tickers and ratios.

Independent of the scanner's universe and runs: its lists live in `config/markets.yaml` and its prices refresh only
from its own page. It reuses the scanner's data layer (shared Parquet cache), analysis engine and charts.
"""
