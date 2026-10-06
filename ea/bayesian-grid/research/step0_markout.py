"""Step 0 of the v7 HF-grid plan: is there a gross 5-30 minute reversal on EURUSD
that is bigger than the round-trip cost? If not, no grid geometry can make money.

Measured on mid prices (Oanda M1), IN-SAMPLE YEARS ONLY by default (2012-2016) so
2017-2020.05 stays an untouched holdout.

Two statistics, both sign-adjusted so "positive = the grid / reversion side wins":
  A. Grid add markout: after price has moved >= s pips against a position within
     the last k minutes (what triggers a grid layer), the mean move over the next
     h minutes (entry at the next bar's open).
  B. Stretch trigger (design module M12): z = (close - EMA90) / sigma15 beyond
     +/-ZEntry with a reclaim bar and the trend veto, markout over h minutes.
Both are reported by server-hour session block and by year, next to the all-in
round-trip cost of a raw-ECN and a standard account for that hour.

Usage: python step0_markout.py <eurusd_m1.npz> <out_dir> [first_year] [last_year]
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bgrid_bt import SPREAD_PROFILE  # noqa: E402

PIP = 1e-4
SESS = {"00-03 rollover/Asia": range(0, 3), "03-09 Asia": range(3, 9), "09-12 London": range(9, 12),
        "12-15 London/ECB": range(12, 15), "15-19 NY overlap": range(15, 19), "19-24 late NY": range(19, 24)}


def costs(hour):
    """All-in round trip in pips: spread (hour profile) + commission + 0.3 pip slippage."""
    prof = SPREAD_PROFILE[hour]
    return {"raw": 0.2 * prof + 0.7 + 0.3, "std": 1.2 * prof + 0.3}


def load(npz, y0, y1):
    z = np.load(npz)
    df = pd.DataFrame({"t": pd.to_datetime(z["t_srv"], unit="s"), "o": z["o"], "h": z["h"], "l": z["l"],
                       "c": z["c"]}).set_index("t")
    df = df[(df.index.year >= y0) & (df.index.year <= y1)]
    # regular 1-minute grid inside trading time; forward-fill short gaps only
    full = pd.date_range(df.index[0], df.index[-1], freq="1min")
    df = df.reindex(full)
    gap = df.c.isna()
    df = df.ffill(limit=5)
    df["valid"] = ~df.c.isna() & ~gap.rolling(30, min_periods=1).max().astype(bool)
    return df


def forward(df, h):
    """Move from the NEXT bar's open to the close h minutes later, in pips."""
    entry = df.o.shift(-1)
    exitp = df.c.shift(-h)
    return (exitp - entry) / PIP


def stat_A(df, s_list=(3, 5, 8), k=15, h_list=(5, 15, 30)):
    rows = []
    past = {}
    for kk in (k,):
        hi = df.h.rolling(kk).max()
        lo = df.l.rolling(kk).min()
        past[kk] = ((df.c - hi) / PIP, (df.c - lo) / PIP)  # drawdown from recent high / rally from low
    fwd = {h: forward(df, h) for h in h_list}
    hour = df.index.hour
    year = df.index.year
    for s in s_list:
        dn, up = past[k]
        # fire only on the first bar of an episode (no overlapping duplicates)
        long_sig = (dn <= -s) & ~((dn <= -s).shift(1, fill_value=False)) & df.valid
        short_sig = (up >= s) & ~((up >= s).shift(1, fill_value=False)) & df.valid
        for h in h_list:
            m = pd.concat([fwd[h][long_sig], -fwd[h][short_sig]])
            idx = m.index
            rows.append(pd.DataFrame({"stat": "A", "s": s, "h": h, "markout": m.values,
                                      "hour": idx.hour, "year": idx.year}))
    return pd.concat(rows, ignore_index=True)


def stat_B(df, zentry=1.5, ema=90, h_list=(5, 15, 30)):
    r1 = df.c.diff() / PIP
    sig1 = np.sqrt((r1 ** 2).ewm(halflife=60, min_periods=120).mean())
    sig15 = sig1 * np.sqrt(15)
    zz = (df.c - df.c.ewm(span=ema, adjust=False).mean()) / PIP / sig15
    trend = r1.rolling(60).sum().abs() / (sig1 * np.sqrt(60))
    reclaim_up = (df.c > df.o) & (df.c > df.l.shift(1))
    reclaim_dn = (df.c < df.o) & (df.c < df.h.shift(1))
    ok = df.valid & (trend < 2.0)
    long_sig = ok & (zz <= -zentry) & reclaim_up
    short_sig = ok & (zz >= zentry) & reclaim_dn
    rows = []
    for h in h_list:
        f = forward(df, h)
        m = pd.concat([f[long_sig], -f[short_sig]])
        rows.append(pd.DataFrame({"stat": "B", "s": zentry, "h": h, "markout": m.values,
                                  "hour": m.index.hour, "year": m.index.year}))
    return pd.concat(rows, ignore_index=True)


def summarize(x):
    sess = np.empty(len(x), dtype=object)
    for name, hrs in SESS.items():
        sess[np.isin(x.hour, list(hrs))] = name
    x = x.assign(session=sess)
    x["cost_raw"] = [costs(hh)["raw"] for hh in x.hour]
    x["cost_std"] = [costs(hh)["std"] for hh in x.hour]
    x = x.dropna(subset=["markout"])
    g = x.groupby(["stat", "s", "h", "session"])
    out = g.agg(n=("markout", "size"), mean=("markout", "mean"), se=("markout", lambda v: v.std() / np.sqrt(len(v))),
                cost_raw=("cost_raw", "mean"), cost_std=("cost_std", "mean")).reset_index()
    out["edge_to_raw_cost"] = out["mean"] / out["cost_raw"]
    yr = x.groupby(["stat", "s", "h", "session", "year"]).agg(mean=("markout", "mean"), cost=("cost_raw", "mean"))
    yr["pass2x"] = yr["mean"] >= 2 * yr["cost"]
    years_pass = yr.groupby(level=[0, 1, 2, 3]).pass2x.sum().rename("years_mean>=2x_raw_cost")
    n_years = yr.groupby(level=[0, 1, 2, 3]).size().rename("years")
    out = out.merge(years_pass.reset_index(), on=["stat", "s", "h", "session"]).merge(
        n_years.reset_index(), on=["stat", "s", "h", "session"])
    return out


def main():
    npz, out = sys.argv[1], sys.argv[2]
    y0 = int(sys.argv[3]) if len(sys.argv) > 3 else 2012
    y1 = int(sys.argv[4]) if len(sys.argv) > 4 else 2016
    os.makedirs(out, exist_ok=True)
    df = load(npz, y0, y1)
    res = summarize(pd.concat([stat_A(df), stat_B(df)], ignore_index=True))
    path = os.path.join(out, f"step0_markout_{y0}_{y1}.csv")
    res.to_csv(path, index=False)
    pd.set_option("display.width", 220)
    print(res.round(3).to_string(index=False))
    best = res.sort_values("edge_to_raw_cost", ascending=False).head(5)
    print("\nbest 5 cells by gross edge / raw all-in cost:")
    print(best[["stat", "s", "h", "session", "n", "mean", "se", "cost_raw", "edge_to_raw_cost",
                "years_mean>=2x_raw_cost", "years"]].round(3).to_string(index=False))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
