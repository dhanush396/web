"""Backtest the HF grid under a broker's published account conditions (here: Vantage Markets).

Usage: python vantage_run.py <eurusd_m1.npz> <models.json> <out_dir>

models.json: {"backtest_models": [{name, account, balance_units, spread_pips_typical,
              commission_round_turn_per_lot, leverage, stop_out_pct, min_lot, swap_long, swap_short}, ...]}
(as produced by the vantage-conditions research workflow).

Runs, for every account model, on EURUSD 2012-2016 (fit period) and 2017-2020.05 (holdout):
  1. the user's production logic in HF mode over grid size x TP (6 x 6),
  2. the two recommended cells (grid 2/TP 1, grid 5/TP 3) under 3 path seeds and 2 slippage levels,
  3. the best realistic config from the in-sample TPE study (results/hf_std_raw/frozen.json).
"""
import itertools
import json
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BG_HF_ACCOUNT", "std_raw")
import bgrid_bt as bt  # noqa: E402
import news_calendar as nc  # noqa: E402
import optimize_hf as ohf  # noqa: E402

PERIODS = {"2012-2016": ("2012-01-01", "2017-01-01"), "2017-2020.05": ("2017-01-01", "2020-06-01")}
CORE = dict(BaseLot=0.01, FlatLayers=1, LotIncrement=0.01, LotIncEvery=1, MaxLayers=5, MaxTotalLots=0.5,
            EntryMode=0, EntryTFMin=1, MaxHoldSec=300, NewsFilter=1, NewsBeforeMin=30, NewsAfterMin=30,
            NewsPauseLayers=1, SessStartMin=60, SessEndMin=1380, SessCloseAtEnd=1, StopPips=0.0,
            MaxBasketRiskPct=0.0, PeakKillPct=0.0, DailyLossPct=0.0, UseTimeFilter=0)
SP = [20, 30, 50, 75, 100, 150]
TP = [1.0, 2.0, 3.0, 5.0, 8.0, 12.0]

_D = None
_NEWS = None


def _init(npz):
    global _D, _NEWS
    _D = bt.Data(npz)
    _NEWS = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)


def account(m):
    """Broker model -> simulator inputs (same structure as optimize_hf.BASE)."""
    cent = m["balance_units"] >= 1000
    return dict(ohf.BASE, Balance=float(m["balance_units"]), SpreadPts=10.0 * m["spread_pips_typical"],
                CommPerLotRT=float(m["commission_round_turn_per_lot"]), SlippagePts=2.0,
                Leverage=float(m["leverage"]), StopOutPct=float(m["stop_out_pct"]),
                SwapLong=float(m["swap_long"]), SwapShort=float(m["swap_short"]),
                VolMin=float(m["min_lot"]), VolStep=0.01, MaxTotalLots=50.0 if cent else 0.5)


def run_one(acc, cfg, period, overrides=None):
    c = dict(acc)
    c.update(cfg)
    if overrides:
        c.update(overrides)
    r = bt.run(_D.slice(*PERIODS[period]), bt.make_params(**c), news=_NEWS)
    b0 = acc["Balance"]
    return dict(final_usd=r["final_equity"] / b0 * 100.0, ret_yr=r["cagr_pct"], max_dd=r["max_dd_pct"],
                ruined=r["ruined"] or r["killed"],
                trades_day=(r["wins"] + r["losses"] + r["time_stops"] + r["side_stops"]) / max(r["years"] * 260, 1e-9),
                commission_usd=r["commission"] / b0 * 100.0)


def _job(a):
    kind, mname, acc, label, cfg, period, ov = a
    out = run_one(acc, cfg, period, ov)
    return dict(kind=kind, model=mname, case=label, period=period, **out)


def main():
    npz, models_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    models = json.load(open(models_path))["backtest_models"]
    frozen = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "hf_std_raw",
                                         "frozen.json")))
    best_tpe = ohf.build(frozen["is_rank1"])
    jobs = []
    for m in models:
        acc = account(m)
        cent = m["balance_units"] >= 1000
        core = dict(CORE, MaxTotalLots=50.0 if cent else 0.5)
        if cent:
            core.update(BaseLot=0.01, LotIncrement=0.01)
        for per in PERIODS:
            for sp, tp in itertools.product(SP, TP):
                jobs.append(("sweep", m["name"], acc, f"grid {sp / 10:g}p / TP {tp:g}p",
                             dict(core, GridSpacingPts=sp, TP_Pips=tp), per, None))
            for sp, tp in [(20, 1.0), (50, 3.0)]:
                for seed in (11, 12, 13):
                    for slip in (2.0, 5.0):
                        jobs.append(("robust", m["name"], acc, f"grid {sp / 10:g}p / TP {tp:g}p | seed {seed} | slip {slip / 10:g}p",
                                     dict(core, GridSpacingPts=sp, TP_Pips=tp), per, dict(PathSeed=seed, SlippagePts=slip)))
            jobs.append(("tpe_best", m["name"], acc, "TPE in-sample best (late-NY stretch)", best_tpe, per, None))
    print(f"{len(jobs)} backtests", flush=True)
    with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
        rows = pool.map(_job, jobs, chunksize=2)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(out, "vantage_backtests.csv"), index=False)
    pd.set_option("display.width", 200)
    for m in models:
        d = df[(df.model == m["name"]) & (df.kind == "sweep")]
        for per in PERIODS:
            t = d[d.period == per].copy()
            t["grid"] = t.case.str.extract(r"grid ([\d.]+)p")[0].astype(float)
            t["tp"] = t.case.str.extract(r"TP ([\d.]+)p")[0].astype(float)
            print(f"\n=== {m['name']} ({m['account']}) {per}: $100 ends at (rows grid pips, cols TP pips) ===")
            print(t.pivot(index="grid", columns="tp", values="final_usd").round(0).to_string())
        r = df[(df.model == m["name"]) & (df.kind != "sweep")]
        print(r[["case", "period", "final_usd", "ret_yr", "max_dd", "ruined", "trades_day"]].round(1).to_string(index=False))


if __name__ == "__main__":
    main()
