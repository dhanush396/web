"""Backtest the v7.02 probability exit against the alternatives, $100 on Vantage Raw ECN.

Usage: python prob_exit_eval.py <eurusd_m1.npz> <coefficients.json> <out_dir>

Every variant uses your HF production logic (M1 entries, 0.01 +0.01 ladder, news filter, entries
01:00-23:00) with NO holding-period exit, except the "time stop" reference row:
  tp_stop        : exit only at TP or at the L1-anchored stop (no early exit)
  floor_rw_X     : + exit when the random-walk P(TP first) = bgrid_bt._p_rw (gambler's ruin including the
                   grid's own future adds) < X (zero coefficients)
  floor_model_X  : + exit when the fitted model's P(TP first) < X
  edge_model_X   : + exit when the model's odds are below the random walk's by X logits
  time_300s      : the previous v7 setup (5-minute hold, session-end close) for reference
Each runs on the real cost model and on a zero-cost copy (same path model) that isolates the
gross effect of the exit rule. Periods: 2012-2016 (the model was fitted here) and 2017-2020.05.
"""
import itertools
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
GEOMS = [(20, 1.0), (50, 3.0), (75, 5.3), (150, 8.0)]
STOPS = [10, 20, 40]

_D = None
_NEWS = None


def _init(npz):
    global _D, _NEWS
    _D = bt.Data(npz)
    _NEWS = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)


def accounts():
    models = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                                         "vantage_models.json")))["backtest_models"]
    m = next(x for x in models if x["name"] == "vantage_raw_ecn")
    real = vr.account(m)
    zero = dict(real, SpreadPts=0.0, CommPerLotRT=0.0, SlippagePts=0.0, SwapLong=0.0, SwapShort=0.0,
                RolloverSpreadMult=1.0, UseSpreadProfile=0)
    if os.environ.get("BG_PE_ACCOUNTS", "usd100") == "fixed":
        # fixed 0.01 ladder on a balance the account limits never touch: every config trades the whole
        # period, so $ per basket compares the exit rules on tens of thousands of baskets each
        return {"vantage_fixed_lots": dict(real, Balance=1e6), "zero_cost_fixed_lots": dict(zero, Balance=1e6)}
    return {"vantage_raw_ecn": real, "zero_cost": zero}


def variants(coef):
    beta = {k: coef[k] for k in ("ProbB0", "ProbBZ", "ProbBTrend", "ProbBVol", "ProbBAge", "ProbBLayers")}
    v = {"tp_stop": dict(ProbExitMode=0)}
    for x in (0.25, 0.40):
        v[f"floor_rw_{x:g}"] = dict(ProbExitMode=1, ProbMin=x)
        v[f"floor_model_{x:g}"] = dict(ProbExitMode=1, ProbMin=x, **beta)
    for x in (0.0, 0.25):
        v[f"edge_model_{x:g}"] = dict(ProbExitMode=2, ProbEdge=x, **beta)
    v["time_300s"] = dict(ProbExitMode=0, MaxHoldSec=300, SessCloseAtEnd=1)
    return v


def _job(a):
    acc_name, acc, var_name, var, sp, tp, stop, per, coef = a
    c = dict(acc)
    c.update(vr.CORE)
    c.update(MaxHoldSec=0, SessCloseAtEnd=0, GridSpacingPts=sp, TP_Pips=tp, StopPips=stop, MaxLayers=5,
             MaxBasketRiskPct=5.0, ZEmaBars=coef["ZEmaBars"], ProbVolLongBars=coef["ProbVolLongBars"])
    c.update(var)
    prm = bt.make_params(**c)
    r = bt.run(_D.slice(*PERIODS[per]), prm, news=_NEWS)
    b0 = acc["Balance"]
    # the ladder fit can no longer place even one 0.01 layer: the account has stopped trading
    frozen = int(bt._fit_layers(prm, c["SpreadPts"] * c["Point"] * 0.5 if "Point" in c else c["SpreadPts"] * 1e-5 * 0.5,
                                r["final_equity"]) < 1)
    baskets = r["wins"] + r["losses"] + r["time_stops"] + r["side_stops"] + r["session_closes"] + \
        r["news_closes"] + r["prob_exits"]
    return dict(account=acc_name, variant=var_name, grid=sp / 10, tp=tp, stop=stop, period=per,
                final_usd=r["final_equity"] / b0 * 100.0, cagr=r["cagr_pct"], max_dd=r["max_dd_pct"],
                ruined=int(r["ruined"] or r["killed"]), frozen=frozen, baskets_day=baskets / max(r["years"] * 260, 1e-9),
                p_tp=r["wins"] / max(baskets, 1), prob_exit_share=r["prob_exits"] / max(baskets, 1),
                stop_share=r["side_stops"] / max(baskets, 1), avg_hold_min=r["avg_hold_min"],
                usd_per_basket=(r["final_balance"] - b0) / max(baskets, 1),
                usd_per_year=(r["final_balance"] - b0) / max(r["years"], 1e-9))


def main():
    npz, coef_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    coef = json.load(open(coef_path))
    jobs = []
    for (an, acc), (vn, var), (sp, tp), stop, per in itertools.product(
            accounts().items(), variants(coef).items(), GEOMS, STOPS, PERIODS):
        jobs.append((an, acc, vn, var, sp, tp, stop, per, coef))
    print(f"{len(jobs)} backtests", flush=True)
    with Pool(4, initializer=_init, initargs=(npz,)) as pool:
        rows = pool.map(_job, jobs, chunksize=2)
    df = pd.DataFrame(rows)
    tag = os.environ.get("BG_PE_ACCOUNTS", "usd100")
    df.to_csv(os.path.join(out, f"prob_exit_backtests_{tag}.csv"), index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    for an in df.account.unique():
        d = df[df.account == an]
        piv = d.pivot_table(index="variant", columns="period", values="final_usd",
                            aggfunc=["median", "max"]).round(1)
        print(f"\n=== {an}: $100 ends at, across the {len(GEOMS) * len(STOPS)} grid/TP/stop cells ===")
        print(piv.to_string())
        # best cell per variant chosen on 2012-2016, then its 2017-2020.05 value
        is_ = d[d.period == "2012-2016"].sort_values("final_usd", ascending=False).groupby("variant").head(1)
        sel = is_.merge(d[d.period == "2017-2020.05"], on=["account", "variant", "grid", "tp", "stop"],
                        suffixes=("_is", "_ho"))
        print(sel[["variant", "grid", "tp", "stop", "final_usd_is", "final_usd_ho", "p_tp_is", "p_tp_ho",
                   "prob_exit_share_ho", "baskets_day_ho", "avg_hold_min_ho", "usd_per_basket_ho"]]
              .round(3).to_string(index=False))


if __name__ == "__main__":
    main()
