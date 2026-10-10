"""Fit the v7.02 probability-exit model: P(basket reaches its TP before its stop | state).

Usage: python prob_exit_fit.py <eurusd_m1.npz> <out_dir>

Training rows come from the strategy itself: the HF grid is run with NO time stop and NO early exit
(TP or the L1-anchored stop only), on Vantage Raw ECN costs with a balance large enough that the
account never interferes, and every open grid side is recorded at every M1 bar open:
u = pips to TP, v = pips to the stop, layers, basket age. The label is how that basket ended.

Model (logistic, random-walk offset):
    logit P = logit(P_rw) + b0 + bZ*zs + bTrend*ts + bVol*lv + bAge*ln(1+age) + bLayers*(n-1)
P_rw (bgrid_bt._p_rw) is the gambler's-ruin probability for a driftless price INCLUDING the grid's
own future adds (each add re-sets the TP closer), so all-zero coefficients mean "the market is a
random walk"; b0 and bLayers are no longer absorbing the mechanical effect of the ladder.
The coefficients are fitted on 2012-2016 only; 2017-2020.05 checks whether they predict better than
the random walk out of sample. Standard errors are clustered by trading day, pooled across configs
and sides (all rows of one day share the same price path).
"""
import json
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgrid_bt as bt  # noqa: E402
import news_calendar as nc  # noqa: E402
import vantage_run as vr  # noqa: E402

PERIODS = {"2012-2016": ("2012-01-01", "2017-01-01"), "2017-2020.05": ("2017-01-01", "2020-06-01")}
# (grid pts, TP pips, stop pips, max layers): small HF grids up to the original 7.5/5.3 geometry
CONFIGS = [(20, 1.0, 10, 5), (30, 2.0, 15, 5), (50, 3.0, 20, 5), (75, 5.3, 25, 5), (100, 5.0, 30, 4),
           (150, 8.0, 40, 3)]
FEATS = ["b0", "zs", "ts", "lv", "age", "layers"]
EMA_BARS = 90
VOL_LONG = 1440
SUB = 3  # keep every 3rd bar's rows (rows of one basket are highly autocorrelated anyway)

_D = None
_NEWS = None


def _init(npz):
    global _D, _NEWS
    _D = bt.Data(npz)
    _NEWS = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)


def base_cfg():
    models = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                                         "vantage_models.json")))["backtest_models"]
    m = next(x for x in models if x["name"] == "vantage_raw_ecn")
    c = dict(vr.account(m), Balance=1e6)
    c.update(vr.CORE)
    c.update(MaxHoldSec=0, SessCloseAtEnd=0, ZEmaBars=EMA_BARS, ProbVolLongBars=VOL_LONG)
    return c


def collect(job):
    (sp, tp, stop, ml), per = job
    d = _D.slice(*PERIODS[per])
    c = dict(base_cfg(), GridSpacingPts=sp, TP_Pips=tp, StopPips=stop, MaxLayers=ml,
             ProbRecord=2 * len(d.t) + 10)
    r = bt.run(d, bt.make_params(**c), news=_NEWS)
    rec, outc = r["prob_rec"], r["prob_outcome"]
    rec = rec[rec[:, 2].astype(np.int64) % SUB == 0]
    f = bt._prob_features(d, EMA_BARS, VOL_LONG, 1e-4)
    i = rec[:, 2].astype(np.int64)
    side = rec[:, 1]
    bid = rec[:, 0].astype(np.int64)
    y = outc[bid]
    X = np.column_stack([np.ones(len(i)), -side * f[i, 0], side * f[i, 1], f[i, 2], np.log1p(rec[:, 6]),
                         rec[:, 5] - 1.0])
    pf = rec[:, 7]
    ok = (y >= 0) & np.isfinite(X).all(axis=1)
    cfg = f"grid {sp / 10:g} / TP {tp:g} / stop {stop} / L{ml}"
    stats = dict(config=cfg, period=per, baskets=int(r["grids_opened"]), basket_tp_rate=float((outc >= 1).mean()),
                 net_usd_per_basket=(r["final_balance"] - 1e6) / max(r["grids_opened"], 1))
    return dict(cfg=cfg, per=per, X=X[ok], off=np.log(pf[ok] / (1 - pf[ok])), y=y[ok],
                cl=(d.t[i] // 86400).astype(np.int64)[ok], pf=pf[ok], stats=stats)


def fit_logit(X, off, y, cl, iters=30):
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        eta = off + X @ b
        p = 1 / (1 + np.exp(-eta))
        w = p * (1 - p)
        g = X.T @ (y - p)
        H = (X * w[:, None]).T @ X
        step = np.linalg.solve(H + 1e-9 * np.eye(len(b)), g)
        b += step
        if np.abs(step).max() < 1e-9:
            break
    eta = off + X @ b
    p = 1 / (1 + np.exp(-eta))
    H = (X * (p * (1 - p))[:, None]).T @ X
    Hi = np.linalg.inv(H)
    # cluster-robust (sandwich) covariance, clusters = baskets
    u, inv = np.unique(cl, return_inverse=True)
    sc = np.zeros((len(u), X.shape[1]))
    np.add.at(sc, inv, X * (y - p)[:, None])
    V = Hi @ (sc.T @ sc) @ Hi
    return b, np.sqrt(np.diag(V)), len(u)


def scores(X, off, y, b):
    p0 = 1 / (1 + np.exp(-off))
    p1 = 1 / (1 + np.exp(-(off + X @ b)))
    eps = 1e-12

    def ll(p):
        return float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))

    return dict(rows=int(len(y)), tp_rate=float(y.mean()), mean_p_fair=float(p0.mean()),
                mean_p_model=float(p1.mean()), logloss_random_walk=ll(p0), logloss_model=ll(p1),
                brier_random_walk=float(np.mean((p0 - y) ** 2)), brier_model=float(np.mean((p1 - y) ** 2)))


def main():
    npz, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    jobs = [(c, per) for per in PERIODS for c in CONFIGS]
    with Pool(4, initializer=_init, initargs=(npz,)) as pool:
        res = pool.map(collect, jobs, chunksize=1)
    by = {per: [r for r in res if r["per"] == per] for per in PERIODS}

    def cat(rs, k):
        return np.concatenate([r[k] for r in rs])

    tr = by["2012-2016"]
    X, off, y, cl = cat(tr, "X"), cat(tr, "off"), cat(tr, "y"), cat(tr, "cl")
    b, se, ncl = fit_logit(X, off, y, cl)
    coef = pd.DataFrame(dict(feature=FEATS, coef=b, se_clustered=se, t=b / se))
    print(f"fit on 2012-2016: {len(y):,} rows, {ncl:,} trading-day clusters")
    print(coef.round(4).to_string(index=False))
    # the random-walk offset's own slope: logit P = a + c*logit(P_fair); c = 1 means calibrated shape
    Xc = np.column_stack([np.ones(len(y)), off])
    bc, sec, _ = fit_logit(Xc, np.zeros(len(y)), y, cl)
    print(f"calibration of the random-walk probability alone: intercept {bc[0]:.3f} (se {sec[0]:.3f}), "
          f"slope on logit(P_fair) {bc[1]:.3f} (se {sec[1]:.3f})")
    rows = []
    for per in PERIODS:
        for r in by[per]:
            rows.append(dict(config=r["cfg"], period=per, **scores(r["X"], r["off"], r["y"], b), **{
                k: v for k, v in r["stats"].items() if k not in ("config", "period")}))
        rs = by[per]
        rows.append(dict(config="ALL", period=per, **scores(cat(rs, "X"), cat(rs, "off"), cat(rs, "y"), b)))
    ev = pd.DataFrame(rows)
    ev["logloss_gain_pct"] = (1 - ev.logloss_model / ev.logloss_random_walk) * 100
    pd.set_option("display.width", 250)
    print(ev.round(4).to_string(index=False))
    # calibration deciles on the holdout
    ho = by["2017-2020.05"]
    Xh, offh, yh = cat(ho, "X"), cat(ho, "off"), cat(ho, "y")
    ph = 1 / (1 + np.exp(-(offh + Xh @ b)))
    p0 = 1 / (1 + np.exp(-offh))
    dec = pd.DataFrame(dict(p_model=ph, p_rw=p0, y=yh))
    dec["bin"] = pd.qcut(dec.p_model, 10, labels=False, duplicates="drop")
    cal = dec.groupby("bin").agg(p_model=("p_model", "mean"), p_random_walk=("p_rw", "mean"),
                                 observed=("y", "mean"), rows=("y", "size")).reset_index()
    print("holdout calibration (deciles of the model's P):")
    print(cal.round(4).to_string(index=False))
    coef.to_csv(os.path.join(out, "prob_exit_coefficients.csv"), index=False)
    ev.to_csv(os.path.join(out, "prob_exit_model_eval.csv"), index=False)
    cal.to_csv(os.path.join(out, "prob_exit_holdout_calibration.csv"), index=False)
    json.dump(dict(zip(["ProbB0", "ProbBZ", "ProbBTrend", "ProbBVol", "ProbBAge", "ProbBLayers"],
                       [round(float(x), 4) for x in b])) | dict(ZEmaBars=EMA_BARS, ProbVolLongBars=VOL_LONG),
              open(os.path.join(out, "prob_exit_coefficients.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
