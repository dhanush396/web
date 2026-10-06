"""Grid search + Bayesian (TPE) optimisation + walk-forward for a $100 account.

Usage:
    python optimize.py <eurusd_m1.npz> <results_dir> <mode> [options]

modes:
    baseline   original v6.12 settings at several balances
    grid       structured grid search (IS fit, OOS check, rank stability)
    bayes      Optuna TPE over the full mixed space (IS fit, OOS check)
    wfo        rolling walk-forward: re-optimise (TPE) on 3y, trade next 1y
    risk       rolling 12-month start-date ruin analysis + cost stress for
               the candidate parameter sets in <results_dir>/candidates.json
"""
import itertools
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgrid_bt as bt  # noqa: E402
import news_calendar as nc  # noqa: E402

IS_PERIOD = ("2012-01-01", "2017-01-01")
OOS_PERIOD = ("2017-01-01", "2020-06-01")
FULL_PERIOD = ("2012-01-01", "2020-06-01")

# Account models. Money is in ACCOUNT units: USD for "std", US cents (USC) for "cent".
# A $100 cent account = 10,000 USC and 0.01 cent-lot = $0.001/pip (100x finer than a
# standard account), which is the only way a grid ladder fits on $100.
COMMON_COSTS = dict(SpreadPts=12.0, CommPerLotRT=0.0, SlippagePts=2.0, RolloverSpreadMult=4.0)
ACCOUNTS = {
    "std": dict(Balance=100.0, Leverage=500.0, StopOutPct=50.0, SwapLong=-7.0, SwapShort=-1.0, **COMMON_COSTS),
    "cent": dict(Balance=10000.0, Leverage=500.0, StopOutPct=50.0, SwapLong=-7.0, SwapShort=-1.0, **COMMON_COSTS),
    "cent_swapfree": dict(Balance=10000.0, Leverage=500.0, StopOutPct=50.0, SwapLong=0.0, SwapShort=0.0,
                          **COMMON_COSTS),
}
ACCOUNT_NAME = os.environ.get("BG_ACCOUNT", "std")
ACCOUNT_100 = ACCOUNTS[ACCOUNT_NAME]
# Optional hard max-DD constraint (BG_DDCAP=35): any config above it scores as a failure.
DD_CAP = float(os.environ["BG_DDCAP"]) if os.environ.get("BG_DDCAP") else None
RUN_TAG = os.environ.get("BG_TAG", "")
USD_PER_UNIT = 0.01 if ACCOUNT_NAME.startswith("cent") else 1.0
BAL0 = ACCOUNT_100["Balance"]

# server-hour session blocks (NY-close server time)
SESSIONS = {"asia": range(0, 9), "london": range(9, 15), "ny": range(15, 20), "late": range(20, 24)}

_D = None
_NEWS = None


def _init(npz):
    global _D, _NEWS
    _D = bt.Data(npz)
    _NEWS = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)


def sessions_to_hours(flags):
    hrs = [h for name, on in zip(SESSIONS, flags) if on for h in SESSIONS[name]]
    return ",".join(str(h) for h in hrs)


def evaluate(cfg, period, daily=False):
    """cfg: dict of EA inputs (+ 'Hours' string). Returns metrics dict."""
    cfg = dict(cfg)
    hours = cfg.pop("Hours", bt.ORIGINAL_HOURS)
    p = bt.make_params(**{**ACCOUNT_100, **cfg})
    d = _D.slice(*period)
    r = bt.run(d, p, hours=hours, news=_NEWS, daily=daily)
    r["score"] = score(r)
    return r


def score(r):
    """Growth subject to survival: CAGR% minus a steep penalty for MaxDD above 25%.
    Any stop-out / equity <= 20% of start is ruin -> large negative score."""
    if r["ruined"]:
        return -100.0 + r["min_equity"] / BAL0 * 10.0
    trades = r["wins"] + r["losses"]
    if trades < 20 * r["years"]:
        return -50.0
    if DD_CAP is not None and r["max_dd_pct"] > DD_CAP:
        return -50.0 - (r["max_dd_pct"] - DD_CAP)
    return r["cagr_pct"] - 2.0 * max(0.0, r["max_dd_pct"] - 25.0)


def _eval_pair(cfg):
    a = evaluate(cfg, IS_PERIOD)
    b = evaluate(cfg, OOS_PERIOD)
    return cfg, a, b


KEEP = ["final_equity", "cagr_pct", "max_dd_pct", "min_equity", "wins", "losses", "basket_stops",
        "stopouts", "max_layers_used", "grids_hit_max", "swap", "score", "ruined"]


def _row(cfg, a, b):
    row = {k: v for k, v in cfg.items()}
    for k in KEEP:
        row["is_" + k] = a[k]
        row["oos_" + k] = b[k]
    return row


# ---------------------------------------------------------------- spaces
CENT_LOTS = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]

LOT_SCHEDULES = {
    "flat": dict(FlatLayers=99, LotIncrement=0.0, LotIncEvery=1),
    "inc_every3": dict(FlatLayers=5, LotIncrement=0.01, LotIncEvery=3),
    "inc_every2": dict(FlatLayers=3, LotIncrement=0.01, LotIncEvery=2),
    "inc_every1": dict(FlatLayers=5, LotIncrement=0.01, LotIncEvery=1),
}

GRID_BASE = dict(BaseLot=0.01, MaxTotalLots=1.0, HaltCooldownMin=1440, NewsFilter=1, NewsBeforeMin=30,
                 NewsAfterMin=30, UseTimeFilter=0, RefCapital=0)
if ACCOUNT_NAME.startswith("cent"):
    GRID_BASE.update(BaseLot=0.10, MaxTotalLots=100.0)


def grid_configs():
    if ACCOUNT_NAME.startswith("cent"):
        space = ([75, 100, 150, 200, 300, 400], [5.3, 10.0, 15.0, 25.0], [3, 5, 8, 12, 18],
                 ["flat", "inc_every3"], [0, 2, 4, 6, 10, 15, 25])
    else:
        space = ([50, 75, 100, 150, 200, 300], [3.0, 5.3, 8.0, 12.0, 18.0], [3, 5, 8, 12, 18],
                 list(LOT_SCHEDULES), [0, 15, 25, 40])
    for sp, tp, ml, ls, bk in itertools.product(*space):
        sched = dict(LOT_SCHEDULES[ls])
        if ACCOUNT_NAME.startswith("cent") and sched["LotIncrement"] > 0:
            sched["LotIncrement"] = GRID_BASE["BaseLot"]  # +1 base lot every N layers
        cfg = dict(GRID_BASE, GridSpacingPts=sp, TP_Pips=tp, MaxLayers=ml, EmergencyCloseDD_Pct=bk, **sched)
        cfg["LotSchedule"] = ls
        yield cfg


def strip(cfg):
    return {k: v for k, v in cfg.items() if k in bt.PI or k == "Hours"}


def run_grid(npz, out):
    cfgs = list(grid_configs())
    print(f"grid: {len(cfgs)} configs x (IS {IS_PERIOD}, OOS {OOS_PERIOD})", flush=True)
    t0 = time.time()
    rows = []
    with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
        for i, (cfg, a, b) in enumerate(pool.imap_unordered(_grid_job, cfgs, chunksize=8)):
            rows.append(_row(cfg, a, b))
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(cfgs)}  {time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(rows).sort_values("is_score", ascending=False)
    df.to_csv(os.path.join(out, "grid_search.csv"), index=False)
    summarize(df, out, "grid")


def _grid_job(cfg):
    ls = cfg.get("LotSchedule")
    c, a, b = _eval_pair(strip(cfg))
    c["LotSchedule"] = ls
    return c, a, b


def summarize(df, out, tag):
    from scipy.stats import spearmanr  # noqa: WPS433
    alive = df[df.is_ruined == 0]
    rho = spearmanr(df.is_score, df.oos_score).correlation
    top = df.head(20)
    lines = [
        f"[{tag}] configs={len(df)}  IS-survivors={len(alive)} ({len(alive) / len(df):.0%})"
        f"  OOS-survivors={int((df.oos_ruined == 0).sum())}",
        f"[{tag}] Spearman(IS score, OOS score) = {rho:.3f}",
        f"[{tag}] top-20 by IS: OOS ruined={int(top.oos_ruined.sum())}/20, "
        f"median OOS CAGR={top.oos_cagr_pct.median():.1f}%  median OOS DD={top.oos_max_dd_pct.median():.1f}%",
        f"[{tag}] configs positive in BOTH IS and OOS without ruin: "
        f"{int(((df.is_cagr_pct > 0) & (df.oos_cagr_pct > 0) & (df.is_ruined == 0) & (df.oos_ruined == 0)).sum())}",
    ]
    txt = "\n".join(lines)
    print(txt)
    with open(os.path.join(out, f"{tag}_summary.txt"), "w") as f:
        f.write(txt + "\n")
    cols = [c for c in df.columns if not c.startswith(("is_", "oos_"))] + [
        "is_cagr_pct", "is_max_dd_pct", "is_basket_stops", "is_score",
        "oos_cagr_pct", "oos_max_dd_pct", "oos_basket_stops", "oos_ruined", "oos_score"]
    print(df[cols].head(15).to_string(index=False))


# ---------------------------------------------------------------- bayes
def suggest(trial):
    sched = trial.suggest_categorical("LotSchedule", list(LOT_SCHEDULES))
    base = 0.01
    if ACCOUNT_NAME.startswith("cent"):
        base = trial.suggest_categorical("BaseLot", CENT_LOTS)
    lots = dict(LOT_SCHEDULES[sched])
    if lots["LotIncrement"] > 0:
        lots["LotIncrement"] = base
    cfg = dict(
        BaseLot=base,
        GridSpacingPts=trial.suggest_int("GridSpacingPts", 40, 500, step=10),
        TP_Pips=trial.suggest_float("TP_Pips", 2.0, 60.0, step=0.5),
        MaxLayers=trial.suggest_int("MaxLayers", 2, 18),
        EmergencyCloseDD_Pct=trial.suggest_categorical("EmergencyCloseDD_Pct", [0, 10, 15, 20, 25, 30, 40, 50]),
        MaxEquityDD_Pct=trial.suggest_categorical("MaxEquityDD_Pct", [0, 5, 10, 20]),
        HaltCooldownMin=trial.suggest_categorical("HaltCooldownMin", [60, 240, 1440, 4320]),
        MaxTotalLots=1.0,
        RefCapital=0,
        NewsFilter=trial.suggest_categorical("NewsFilter", [0, 1]),
        NewsBeforeMin=trial.suggest_categorical("NewsBeforeMin", [15, 30, 60, 120]),
        NewsAfterMin=trial.suggest_categorical("NewsAfterMin", [15, 30, 60, 120]),
        NewsPauseLayers=trial.suggest_categorical("NewsPauseLayers", [0, 1]),
        NewsCloseMode=trial.suggest_categorical("NewsCloseMode", [0, 1, 2]),
        NewsCloseMin=15,
        CloseOnFriday=trial.suggest_categorical("CloseOnFriday", [0, 1]),
        FridayCloseHour=trial.suggest_categorical("FridayCloseHour", [18, 20, 22]),
        MaxSpreadPts=trial.suggest_categorical("MaxSpreadPts", [0, 25]),
        **lots,
    )
    if ACCOUNT_NAME.startswith("cent"):
        cfg["MaxTotalLots"] = 100.0
    tf = trial.suggest_categorical("TimeFilter", ["off", "original", "sessions"])
    if tf == "off":
        cfg["UseTimeFilter"] = 0
    else:
        cfg["UseTimeFilter"] = 1
        if tf == "original":
            cfg["Hours"] = bt.ORIGINAL_HOURS
        else:
            flags = [trial.suggest_categorical(f"S_{s}", [0, 1]) for s in SESSIONS]
            if not any(flags):
                flags[0] = 1
            cfg["Hours"] = sessions_to_hours(flags)
    return cfg


def _bayes_eval(args):
    cfg, period = args
    return evaluate(cfg, period)


def run_bayes(npz, out, n_trials=2400, period=IS_PERIOD, tag="bayes", seed=7, verbose=True):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True, group=True, n_startup_trials=200,
                                         constant_liar=True)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    batch = os.cpu_count()
    t0 = time.time()
    with Pool(batch, initializer=_init, initargs=(npz,)) as pool:
        done = 0
        while done < n_trials:
            trials = [study.ask() for _ in range(batch)]
            cfgs = [suggest(tr) for tr in trials]
            res = pool.map(_bayes_eval, [(c, period) for c in cfgs])
            for tr, r in zip(trials, res):
                for k in ("cagr_pct", "max_dd_pct", "ruined", "basket_stops", "final_equity"):
                    tr.set_user_attr(k, float(r[k]))
                study.tell(tr, r["score"])
            done += batch
            if verbose and done % (batch * 50) == 0:
                b = study.best_trial
                print(f"  [{tag}] {done}/{n_trials} {time.time() - t0:.0f}s best={b.value:.2f} "
                      f"cagr={b.user_attrs['cagr_pct']:.1f}% dd={b.user_attrs['max_dd_pct']:.1f}%", flush=True)
    return study


def bayes_main(npz, out, n_trials):
    study = run_bayes(npz, out, n_trials=n_trials)
    df = study.trials_dataframe()
    df.to_csv(os.path.join(out, "bayes_trials.csv"), index=False)
    # OOS-check the top 40 distinct IS trials
    top = sorted([t for t in study.trials if t.value is not None], key=lambda t: -t.value)[:40]
    cfgs = [cfg_from_params(t.params) for t in top]
    _init(npz)
    rows = []
    with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
        for cfg, a, b in pool.map(_eval_pair, cfgs):
            rows.append(_row(cfg, a, b))
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(out, "bayes_top40.csv"), index=False)
    # parameter importance
    try:
        import optuna
        imp = optuna.importance.get_param_importances(study)
        with open(os.path.join(out, "bayes_importance.json"), "w") as f:
            json.dump(imp, f, indent=1)
        print("importance:", {k: round(v, 3) for k, v in list(imp.items())[:10]})
    except Exception as e:  # pragma: no cover
        print("importance failed", e)
    summarize(res, out, "bayes")


def cfg_from_params(params):
    """Rebuild an EA cfg from an optuna params dict (same logic as suggest())."""
    pr = dict(params)
    sched = pr.pop("LotSchedule")
    tf = pr.pop("TimeFilter")
    flags = [pr.pop(f"S_{s}", 0) for s in SESSIONS]
    base = pr.pop("BaseLot", 0.01)
    lots = dict(LOT_SCHEDULES[sched])
    if lots["LotIncrement"] > 0:
        lots["LotIncrement"] = base
    cfg = dict(BaseLot=base, MaxTotalLots=100.0 if ACCOUNT_NAME.startswith("cent") else 1.0, RefCapital=0,
               NewsCloseMin=15, **lots)
    cfg.update(pr)
    if tf == "off":
        cfg["UseTimeFilter"] = 0
    else:
        cfg["UseTimeFilter"] = 1
        if tf == "original":
            cfg["Hours"] = bt.ORIGINAL_HOURS
        else:
            if not any(flags):
                flags[0] = 1
            cfg["Hours"] = sessions_to_hours(flags)
    return cfg


# ---------------------------------------------------------------- walk-forward
WFO_FOLDS = [
    (("2012-01-01", "2015-01-01"), ("2015-01-01", "2016-01-01")),
    (("2013-01-01", "2016-01-01"), ("2016-01-01", "2017-01-01")),
    (("2014-01-01", "2017-01-01"), ("2017-01-01", "2018-01-01")),
    (("2015-01-01", "2018-01-01"), ("2018-01-01", "2019-01-01")),
    (("2016-01-01", "2019-01-01"), ("2019-01-01", "2020-01-01")),
    (("2017-01-01", "2020-01-01"), ("2020-01-01", "2020-06-01")),
]


def wfo_main(npz, out, n_trials):
    _init(npz)
    rows = []
    for k, (tr, te) in enumerate(WFO_FOLDS):
        study = run_bayes(npz, out, n_trials=n_trials, period=tr, tag=f"wfo{k}", seed=100 + k, verbose=False)
        best = study.best_trial
        cfg = cfg_from_params(best.params)
        r_is = evaluate(cfg, tr)
        r_oos = evaluate(cfg, te)
        row = dict(fold=k, train=f"{tr[0]}..{tr[1]}", test=f"{te[0]}..{te[1]}",
                   is_cagr=r_is["cagr_pct"], is_dd=r_is["max_dd_pct"], is_score=r_is["score"],
                   oos_ret_pct=(r_oos["final_equity"] / BAL0 - 1) * 100, oos_cagr=r_oos["cagr_pct"],
                   oos_dd=r_oos["max_dd_pct"], oos_ruined=r_oos["ruined"], oos_baskets=r_oos["basket_stops"],
                   params=json.dumps(cfg))
        rows.append(row)
        print(f"  fold {k}: IS cagr {row['is_cagr']:.1f}% dd {row['is_dd']:.1f}% | OOS ret "
              f"{row['oos_ret_pct']:.1f}% dd {row['oos_dd']:.1f}% ruined={row['oos_ruined']}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(out, "walk_forward.csv"), index=False)
    eff = df.oos_cagr.mean() / df.is_cagr.mean() if df.is_cagr.mean() != 0 else float("nan")
    comp = np.prod(1 + df.oos_ret_pct / 100.0)
    txt = (f"[wfo] folds={len(df)} mean IS CAGR={df.is_cagr.mean():.1f}%  mean OOS CAGR={df.oos_cagr.mean():.1f}%"
           f"  WF-efficiency={eff:.2f}  OOS folds ruined={int(df.oos_ruined.sum())}"
           f"  stitched OOS growth of $100 -> ${100 * comp:.2f}  [account={ACCOUNT_NAME}]")
    print(txt)
    with open(os.path.join(out, "wfo_summary.txt"), "w") as f:
        f.write(txt + "\n")


# ---------------------------------------------------------------- risk
def _risk_job(args):
    name, cfg, start, end, override = args
    c = dict(cfg)
    c.update(override)
    r = evaluate(c, (start, end))
    return name, start, r["final_equity"], r["max_dd_pct"], r["ruined"], r["basket_stops"]


def risk_main(npz, out):
    with open(os.path.join(out, "candidates.json")) as f:
        cands = json.load(f)
    starts = pd.date_range("2012-01-01", "2019-05-01", freq="MS")
    jobs = []
    for name, cfg in cands.items():
        for s in starts:
            e = s + pd.DateOffset(months=12)
            jobs.append((name, cfg, str(s.date()), str(e.date()), {}))
    with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
        res = pool.map(_risk_job, jobs, chunksize=4)
    df = pd.DataFrame(res, columns=["cand", "start", "final_equity", "max_dd_pct", "ruined", "baskets"])
    df["final_equity"] = df["final_equity"] / BAL0 * 100.0  # express as "$100 start"
    df.to_csv(os.path.join(out, "rolling_12m.csv"), index=False)
    g = df.groupby("cand").agg(
        windows=("start", "count"),
        p_ruin=("ruined", "mean"),
        p_loss=("final_equity", lambda x: (x < 100).mean()),
        p_down_30=("final_equity", lambda x: (x < 70).mean()),
        median_12m_ret=("final_equity", lambda x: x.median() - 100),
        p10_12m_ret=("final_equity", lambda x: x.quantile(0.10) - 100),
        p90_12m_ret=("final_equity", lambda x: x.quantile(0.90) - 100),
        worst_12m=("final_equity", lambda x: x.min() - 100),
        median_dd=("max_dd_pct", "median"),
        worst_dd=("max_dd_pct", "max"),
    )
    print(g.round(3).to_string())
    g.to_csv(os.path.join(out, "rolling_12m_summary.csv"))
    # cost stress on the full period
    stresses = {
        f"base ({ACCOUNT_NAME}: 1.2p spread, 0.2p slip)": {},
        "spread 1.6p": dict(SpreadPts=16),
        "spread 2.0p + slip 0.5p": dict(SpreadPts=20, SlippagePts=5),
        "raw 0.2p + $7/lot": dict(SpreadPts=2, CommPerLotRT=7),
        "swap -7/-1 per lot-night": dict(SwapLong=-7.0, SwapShort=-1.0),
        "swap-free": dict(SwapLong=0.0, SwapShort=0.0),
    }
    jobs = [(f"{n}|{s}", cfg, FULL_PERIOD[0], FULL_PERIOD[1], ov) for n, cfg in cands.items()
            for s, ov in stresses.items()]
    with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
        res = pool.map(_risk_job, jobs)
    st = pd.DataFrame(res, columns=["case", "start", "final_equity", "max_dd_pct", "ruined", "baskets"])
    st["final_equity"] = st["final_equity"] / BAL0 * 100.0
    st[["cand", "stress"]] = st.case.str.split("|", expand=True)
    st = st.drop(columns=["case", "start"])
    st.to_csv(os.path.join(out, "cost_stress.csv"), index=False)
    print(st.pivot(index="stress", columns="cand", values="final_equity").round(1).to_string())


def baseline_main(npz, out):
    _init(npz)
    rows = []
    orig = dict(BaseLot=0.08, FlatLayers=5, LotIncrement=0.07, GridSpacingPts=75, TP_Pips=5.3, MaxLayers=18,
                MaxTotalLots=8.0, UseTimeFilter=0)
    for bal in [100, 1000, 10000, 100000]:
        for nf in [0, 1]:
            r = evaluate(dict(orig, Balance=bal, NewsFilter=nf), FULL_PERIOD)
            rows.append(dict(balance=bal, news=nf, final_equity=r["final_equity"], max_dd_pct=r["max_dd_pct"],
                             stopouts=r["stopouts"], ruined=r["ruined"], wins=r["wins"],
                             worst_float=r["worst_float"]))
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(out, "baseline_original.csv"), index=False)
    print(df.round(2).to_string(index=False))


if __name__ == "__main__":
    npz, out, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    out = os.path.join(out, ACCOUNT_NAME + (f"_{RUN_TAG}" if RUN_TAG else ""))
    os.makedirs(out, exist_ok=True)
    n = int(sys.argv[4]) if len(sys.argv) > 4 else None
    if mode == "baseline":
        baseline_main(npz, out)
    elif mode == "grid":
        run_grid(npz, out)
    elif mode == "bayes":
        bayes_main(npz, out, n or 2400)
    elif mode == "wfo":
        wfo_main(npz, out, n or 800)
    elif mode == "risk":
        risk_main(npz, out)
    else:
        raise SystemExit(f"unknown mode {mode}")
