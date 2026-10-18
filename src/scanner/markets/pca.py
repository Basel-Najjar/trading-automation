"""Rolling PCA of returns: how much of the co-movement one factor explains, and how correlations shift.

Returns are log returns (a ratio's return is the difference of its legs' log returns). Each rolling window of W
bars is standardised, so the PCA is of the correlation matrix (`numpy.linalg.eigh`). PC1's share of variance
(and the absorption ratio of the first k components) rises when the series move together.

Eigenvector signs are arbitrary, so each window's loadings are flipped to agree with the previous window's
(positive dot product), so that a sign flip is not mistaken for a shift. PC1 starts with a positive loading sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def log_returns(closes: pd.DataFrame) -> pd.DataFrame:
    return np.log(closes).diff().iloc[1:]


def _corr(x: np.ndarray) -> np.ndarray:
    c = np.corrcoef(x, rowvar=False)
    return np.atleast_2d(c)


def _eig(corr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Eigenvalues (descending) and eigenvectors (columns) of a correlation matrix."""
    w, v = np.linalg.eigh(corr)
    order = np.argsort(w)[::-1]
    return np.clip(w[order], 0, None), v[:, order]


def _align(v: np.ndarray, ref: np.ndarray | None) -> np.ndarray:
    v = v.copy()
    for j in range(v.shape[1]):
        if ref is not None:
            if float(v[:, j] @ ref[:, j]) < 0:
                v[:, j] *= -1
        elif v[:, j].sum() < 0:
            v[:, j] *= -1
    return v


@dataclass
class PcaResult:
    members: list[str]
    window: int
    components: int
    dates: list[pd.Timestamp] = field(default_factory=list)
    explained: pd.DataFrame = field(default_factory=pd.DataFrame)  # date × PC1..k share of variance
    avg_corr: pd.Series = field(default_factory=pd.Series)  # mean off-diagonal correlation per window
    loadings_now: pd.DataFrame = field(default_factory=pd.DataFrame)  # member × PC1..k
    loadings_prev: pd.DataFrame | None = None  # one window earlier (aligned signs)
    corr_now: pd.DataFrame = field(default_factory=pd.DataFrame)
    corr_prev: pd.DataFrame | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def corr_change(self) -> pd.DataFrame | None:
        return None if self.corr_prev is None else self.corr_now - self.corr_prev

    def pair_shifts(self, n: int = 10) -> list[dict]:
        """The `n` pairs whose correlation moved most between the previous and the current window."""
        if self.corr_prev is None:
            return []
        out = []
        m = self.members
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                now, prev = float(self.corr_now.iat[i, j]), float(self.corr_prev.iat[i, j])
                out.append({"Pair": f"{m[i]} · {m[j]}", "Now": now, "Before": prev, "Change": now - prev})
        out.sort(key=lambda r: -abs(r["Change"]))
        return out[:n]


def compute_pca(closes: pd.DataFrame, window: int, components: int, min_coverage: float = 0.8) -> PcaResult:
    """Rolling-window PCA over `closes` (columns = series). A series with fewer than `min_coverage` of the
    common history is dropped (it would shrink every window to its own span)."""
    rets = log_returns(closes)
    skipped: list[tuple[str, str]] = []
    if len(rets):
        cover = rets.notna().mean()
        for col in cover.index[cover < min_coverage]:
            skipped.append((col, f"covers only {cover[col]:.0%} of the history"))
        rets = rets[[c for c in rets.columns if cover[c] >= min_coverage]]
    rets = rets.dropna()
    flat = [c for c in rets.columns if float(rets[c].tail(window).std()) == 0.0]
    for c in flat:
        skipped.append((c, "no price changes in the latest window"))
    rets = rets.drop(columns=flat)
    members = list(rets.columns)
    k = min(components, len(members))
    res = PcaResult(members, window, k, skipped=skipped)
    if len(members) < 2 or len(rets) < window:
        res.skipped.append(("PCA", f"needs ≥ 2 series with ≥ {window} common bars ({len(rets)} available)"))
        return res

    x = rets.to_numpy()
    names = [f"PC{i + 1}" for i in range(k)]
    expl, avg, dates = [], [], []
    vecs: list[np.ndarray] = []
    ref = None
    n = len(members)
    for end in range(window, len(x) + 1):
        win = x[end - window:end]
        if np.any(win.std(axis=0) == 0):  # a flat window has no correlation; carry the gap as NaN
            expl.append([np.nan] * k)
            avg.append(np.nan)
            vecs.append(np.full((n, k), np.nan))
            dates.append(rets.index[end - 1])
            continue
        c = _corr(win)
        w, v = _eig(c)
        v = _align(v[:, :k], ref)
        ref = v
        expl.append(list(w[:k] / w.sum()))
        avg.append(float((c.sum() - n) / (n * (n - 1))))
        vecs.append(v)
        dates.append(rets.index[end - 1])

    res.dates = dates
    res.explained = pd.DataFrame(expl, index=pd.DatetimeIndex(dates), columns=names)
    res.avg_corr = pd.Series(avg, index=pd.DatetimeIndex(dates), name="avg_corr")
    res.loadings_now = pd.DataFrame(vecs[-1], index=members, columns=names)
    res.corr_now = pd.DataFrame(_corr(x[-window:]), index=members, columns=members)
    if len(vecs) > window:
        res.loadings_prev = pd.DataFrame(vecs[-1 - window], index=members, columns=names)
        prev = x[-2 * window:-window]
        res.corr_prev = pd.DataFrame(_corr(prev), index=members, columns=members)
    return res
