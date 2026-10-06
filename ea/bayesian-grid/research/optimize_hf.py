"""v7 high-frequency grid: Bayesian (TPE) search, walk-forward and a single holdout test.

Protocol (from the research critique):
  * Every choice is made on 2012-2016 only. 2017-2020.05 is a holdout opened ONCE
    (mode "holdout") for the configs frozen in results/hf_<account>/frozen.json.
  * Fewer than 20 free parameters; risk caps stay at research-derived defaults.
  * Intrabar paths are randomised Brownian bridges (PathPoints=3) because the TP and
    spacing sit inside one M1 bar's range; robustness re-runs other seeds and costs.

Usage:
    BG_HF_ACCOUNT=std_raw python optimize_hf.py <npz> <results_dir> bayes [trials]
    BG_HF_ACCOUNT=std_raw python optimize_hf.py <npz> <results_dir> wfo [trials]
    BG_HF_ACCOUNT=std_raw python optimize_hf.py <npz> <results_dir> freeze   # pick from the IS study
    BG_HF_ACCOUNT=std_raw python optimize_hf.py <npz> <results_dir> robust   # seeds/costs/neighbours on IS
    BG_HF_ACCOUNT=std_raw python optimize_hf.py <npz> <results_dir> holdout  # ONE look at 2017-2020.05
Accounts: std_raw ($100, 0.2-pip raw spread + $7/lot), std ($100, 1.2-pip spread), cent_raw (10,000 USC).
"""
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

IS = ("2012-01-01", "2017-01-01")
HOLDOUT = ("2017-01-01", "2020-06-01")
WFO_FOLDS = [(("2012-01-01", "2014-01-01"), ("2014-01-01", "2015-01-01")),
             (("2013-01-01", "2015-01-01"), ("2015-01-01", "2016-01-01")),
             (("2014-01-01", "2016-01-01"), ("2016-01-01", "2017-01-01"))]

BASE = dict(Leverage=500.0, StopOutPct=50.0, SwapLong=-7.0, SwapShort=-1.0, RolloverSpreadMult=4.0,
            UseSpreadProfile=1, PathPoints=3, PathSeed=11, MarginBufferMult=5.0, MaxTotalLots=0.5)
ACCOUNTS = {
    "std_raw": dict(BASE, Balance=100.0, SpreadPts=2.0, CommPerLotRT=7.0, SlippagePts=2.0),
    "std": dict(BASE, Balance=100.0, SpreadPts=12.0, CommPerLotRT=0.0, SlippagePts=2.0),
    "cent_raw": dict(BASE, Balance=10000.0, SpreadPts=2.0, CommPerLotRT=7.0, SlippagePts=2.0, MaxTotalLots=50.0),
}
ACCOUNT = os.environ.get("BG_HF_ACCOUNT", "std_raw")
ACC = ACCOUNTS[ACCOUNT]
BAL0 = ACC["Balance"]

# fixed, research-derived risk architecture (not optimised)
FIXED = dict(BaseLot=0.01, NewsFilter=1, NewsPauseLayers=1, PeakKillPct=25.0, MaxEquityDD_Pct=0,
             EmergencyCloseDD_Pct=0, HaltCooldownMin=0, RefCapital=0, LotMultiplier=0.0, UseTimeFilter=0,
             SessCloseAtEnd=1, TrendTMax=2.0)
SESSIONS = {  # server time (NY-close, GMT+2/+3) -> minutes
    "asia_0109": (60, 540), "asia_0309": (180, 540), "london_1013": (600, 780),
    "ny_1519": (900, 1140), "lateny_1923": (1140, 1380), "allday_0123": (60, 1380),
}

_D = None
_NEWS = None


def _init(npz):
    global _D, _NEWS
    _D = bt.Data(npz)
    _NEWS = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)


def build(params):
    """Optuna params dict -> simulator inputs."""
    pr = dict(params)
    cfg = dict(FIXED)
    s0, s1 = SESSIONS[pr.pop("Session")]
    cfg.update(SessStartMin=s0, SessEndMin=s1)
    ladder = pr.pop("Ladder")
    if ladder == "flat":
        cfg.update(FlatLayers=99, LotIncrement=0.0, LotIncEvery=1)
    else:  # "step1": 0.01, then +0.01 per layer; "step2": +0.01 every 2 layers
        cfg.update(FlatLayers=1, LotIncrement=0.01 if ACCOUNT != "cent_raw" else cfg["BaseLot"],
                   LotIncEvery=1 if ladder == "step1" else 2)
    cfg.update(pr)
    if ACCOUNT == "cent_raw":
        cfg["BaseLot"] = pr.get("BaseLot", 0.10)
    return cfg


def evaluate(cfg, period, overrides=None, daily=False):
    c = dict(ACC)
    c.update(cfg)
    if overrides:
        c.update(overrides)
    p = bt.make_params(**c)
    r = bt.run(_D.slice(*period), p, news=_NEWS, daily=daily)
    r["score"] = score(r)
    r["trades_per_day"] = (r["wins"] + r["losses"] + r["time_stops"] + r["side_stops"]) / max(r["years"] * 260, 1e-9)
    return r


def score(r):
    """Growth subject to survival: a kill-latch or stop-out is ruin; DD above 20% is penalised."""
    if r["ruined"] or r["killed"]:
        return -100.0 + r["final_equity"] / BAL0 * 10.0
    if r["wins"] + r["losses"] + r["time_stops"] < 50 * r["years"]:
        return -50.0
    return r["cagr_pct"] - 2.0 * max(0.0, r["max_dd_pct"] - 20.0)


def suggest(trial):
    p = dict(
        EntryMode=trial.suggest_categorical("EntryMode", [0, 1]),
        EntryTFMin=trial.suggest_categorical("EntryTFMin", [1, 5]),
        GridSpacingPts=trial.suggest_int("GridSpacingPts", 10, 150, step=5),
        TP_Pips=trial.suggest_float("TP_Pips", 0.5, 15.0, step=0.5),
        MaxLayers=trial.suggest_int("MaxLayers", 1, 6),
        Ladder=trial.suggest_categorical("Ladder", ["flat", "step1", "step2"]),
        MaxHoldSec=trial.suggest_categorical("MaxHoldSec", [30, 60, 120, 300, 600, 900, 1800]),
        StopPips=trial.suggest_float("StopPips", 2.0, 30.0, step=1.0),
        MaxBasketRiskPct=trial.suggest_categorical("MaxBasketRiskPct", [1.0, 2.0, 3.0, 5.0]),
        AddMinSec=trial.suggest_categorical("AddMinSec", [0, 15, 30, 60]),
        Session=trial.suggest_categorical("Session", list(SESSIONS)),
        ZEntry=trial.suggest_float("ZEntry", 1.0, 3.0, step=0.25),
        ZEmaBars=trial.suggest_categorical("ZEmaBars", [30, 60, 90, 120]),
        NewsBeforeMin=trial.suggest_categorical("NewsBeforeMin", [15, 30, 60]),
        NewsAfterMin=trial.suggest_categorical("NewsAfterMin", [15, 30, 60]),
        NewsCloseMode=trial.suggest_categorical("NewsCloseMode", [0, 2]),
        DailyLossPct=trial.suggest_categorical("DailyLossPct", [3.0, 5.0, 8.0]),
        MaxSpreadPts=trial.suggest_categorical("MaxSpreadPts", [0, 10] if "raw" in ACCOUNT else [0, 25]),
    )
    if ACCOUNT == "cent_raw":
        p["BaseLot"] = trial.suggest_categorical("BaseLot", [0.05, 0.10, 0.20, 0.30])
    return p


def _job(args):
    params, period = args
    return evaluate(build(params), period)


def run_tpe(npz, n_trials, period, seed, tag, verbose=True):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(
        seed=seed, multivariate=True, group=True, n_startup_trials=120, constant_liar=True))
    batch = os.cpu_count()
    t0 = time.time()
    with Pool(batch, initializer=_init, initargs=(npz,)) as pool:
        done = 0
        while done < n_trials:
            trials = [study.ask() for _ in range(batch)]
            params = [suggest(t) for t in trials]
            res = pool.map(_job, [(pp, period) for pp in params])
            for t, pp, r in zip(trials, params, res):
                for k in ("cagr_pct", "max_dd_pct", "ruined", "killed", "final_equity", "trades_per_day",
                          "avg_hold_sec", "time_stops", "side_stops", "wins"):
                    t.set_user_attr(k, float(r[k]))
                t.set_user_attr("params", json.dumps(pp))
                study.tell(t, r["score"])
            done += batch
            if verbose and done % (batch * 25) == 0:
                b = study.best_trial
                print(f"  [{tag}] {done}/{n_trials} {time.time() - t0:.0f}s best={b.value:.2f} "
                      f"cagr={b.user_attrs['cagr_pct']:.1f}% dd={b.user_attrs['max_dd_pct']:.1f}% "
                      f"trades/day={b.user_attrs['trades_per_day']:.1f}", flush=True)
    return study


def outdir(base):
    d = os.path.join(base, f"hf_{ACCOUNT}")
    os.makedirs(d, exist_ok=True)
    return d


def main():
    npz, base, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else None
    out = outdir(base)
    if mode == "bayes":
        st = run_tpe(npz, n or 800, IS, 21, "is")
        rows = [dict(value=t.value, **t.user_attrs) for t in st.trials if t.value is not None]
        df = pd.DataFrame(rows).sort_values("value", ascending=False)
        df.to_csv(os.path.join(out, "is_trials.csv"), index=False)
        try:
            import optuna
            imp = optuna.importance.get_param_importances(st)
            json.dump(imp, open(os.path.join(out, "is_importance.json"), "w"), indent=1)
            print("importance:", {k: round(v, 3) for k, v in list(imp.items())[:10]})
        except Exception as e:  # pragma: no cover
            print("importance failed:", e)
        alive = df[(df.ruined == 0) & (df.killed == 0)]
        print(f"[is] trials={len(df)} survivors={len(alive)} positive={int((alive.cagr_pct > 0).sum())}")
        print(df.head(10)[["value", "cagr_pct", "max_dd_pct", "trades_per_day", "avg_hold_sec", "params"]]
              .to_string(index=False))
    elif mode == "wfo":
        _init(npz)
        rows = []
        for k, (tr, te) in enumerate(WFO_FOLDS):
            st = run_tpe(npz, n or 400, tr, 100 + k, f"wfo{k}", verbose=False)
            best = json.loads(st.best_trial.user_attrs["params"])
            a = evaluate(build(best), tr)
            b = evaluate(build(best), te)
            rows.append(dict(fold=k, train=f"{tr[0]}..{tr[1]}", test=f"{te[0]}..{te[1]}", is_cagr=a["cagr_pct"],
                             is_dd=a["max_dd_pct"], oos_ret_pct=(b["final_equity"] / BAL0 - 1) * 100,
                             oos_dd=b["max_dd_pct"], oos_killed=b["killed"], oos_trades_day=b["trades_per_day"],
                             params=json.dumps(best)))
            print(f"  fold {k}: IS {a['cagr_pct']:.1f}%/dd {a['max_dd_pct']:.1f}% -> OOS {rows[-1]['oos_ret_pct']:.1f}% "
                  f"dd {b['max_dd_pct']:.1f}% killed={b['killed']}", flush=True)
        df = pd.DataFrame(rows)
        df.to_csv(os.path.join(out, "walk_forward.csv"), index=False)
        comp = np.prod(1 + df.oos_ret_pct / 100)
        print(f"[wfo] mean IS CAGR {df.is_cagr.mean():.1f}%  OOS years {list(df.oos_ret_pct.round(1))}  "
              f"stitched $100 -> ${100 * comp:.2f}")
    elif mode == "freeze":
        df = pd.read_csv(os.path.join(out, "is_trials.csv"))
        alive = df[(df.ruined == 0) & (df.killed == 0)].head(int(n or 5))
        frozen = {f"is_rank{i + 1}": json.loads(p) for i, p in enumerate(alive.params)}
        json.dump(frozen, open(os.path.join(out, "frozen.json"), "w"), indent=1)
        print(f"froze {len(frozen)} configs from the in-sample study -> frozen.json")
    elif mode == "robust":
        frozen = json.load(open(os.path.join(out, "frozen.json")))
        jobs = []
        cases = {"base": {}, "seed 12": dict(PathSeed=12), "seed 13": dict(PathSeed=13), "seed 14": dict(PathSeed=14),
                 "4-point OHLC path": dict(PathPoints=0), "slippage x2": dict(SlippagePts=4.0),
                 "spread x1.5": dict(SpreadPts=ACC["SpreadPts"] * 1.5), "commission $10": dict(CommPerLotRT=10.0)}
        for name, pr in frozen.items():
            for cname, ov in cases.items():
                jobs.append((name, cname, pr, ov))
        with Pool(os.cpu_count(), initializer=_init, initargs=(npz,)) as pool:
            res = pool.map(_robust_job, jobs)
        df = pd.DataFrame(res)
        df.to_csv(os.path.join(out, "robust_is.csv"), index=False)
        print(df.pivot(index="case", columns="cfg", values="final_usd").round(1).to_string())
    elif mode == "holdout":
        frozen = json.load(open(os.path.join(out, "frozen.json")))
        _init(npz)
        rows = []
        for name, pr in frozen.items():
            a = evaluate(build(pr), IS)
            b = evaluate(build(pr), HOLDOUT)
            rows.append(dict(cfg=name, is_cagr=a["cagr_pct"], is_dd=a["max_dd_pct"], hold_cagr=b["cagr_pct"],
                             hold_dd=b["max_dd_pct"], hold_final_usd=b["final_equity"] / BAL0 * 100,
                             hold_killed=b["killed"], hold_trades_day=b["trades_per_day"],
                             hold_avg_hold_sec=b["avg_hold_sec"]))
        df = pd.DataFrame(rows)
        df.to_csv(os.path.join(out, "holdout.csv"), index=False)
        print(df.round(2).to_string(index=False))
    else:
        raise SystemExit(f"unknown mode {mode}")


def _robust_job(args):
    name, cname, pr, ov = args
    r = evaluate(build(pr), IS, overrides=ov)
    return dict(cfg=name, case=cname, final_usd=r["final_equity"] / BAL0 * 100, cagr=r["cagr_pct"],
                dd=r["max_dd_pct"], killed=r["killed"], trades_day=r["trades_per_day"])


if __name__ == "__main__":
    main()
