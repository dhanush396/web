"""Parameter-neighbourhood robustness: perturb each input one step down/up and
re-run IS, OOS and full period. A real edge sits on a plateau; an overfit
point falls off a cliff one step away.

Usage: BG_ACCOUNT=cent_swapfree python robust.py <eurusd_m1.npz> <candidates.json> <name> <out.csv>
"""
import json
import os
import sys
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimize as opt  # noqa: E402

STEPS = {
    "GridSpacingPts": 20, "TP_Pips": 3.0, "MaxLayers": 2, "EmergencyCloseDD_Pct": 5,
    "MaxEquityDD_Pct": 2.5, "NewsAfterMin": 30, "NewsBeforeMin": 15, "BaseLot": 0.01,
}


def _job(args):
    tag, cfg = args
    a = opt.evaluate(cfg, opt.IS_PERIOD)
    b = opt.evaluate(cfg, opt.OOS_PERIOD)
    f = opt.evaluate(cfg, opt.FULL_PERIOD)
    return dict(variant=tag, is_cagr=a["cagr_pct"], is_dd=a["max_dd_pct"], oos_cagr=b["cagr_pct"],
                oos_dd=b["max_dd_pct"], full_cagr=f["cagr_pct"], full_dd=f["max_dd_pct"],
                ruined=max(a["ruined"], b["ruined"], f["ruined"]))


def main():
    npz, cand_path, name, out = sys.argv[1:5]
    base = json.load(open(cand_path))[name]
    jobs = [("base", base)]
    for k, st in STEPS.items():
        if k not in base:
            continue
        for sgn in (-1, 1):
            c = dict(base)
            v = base[k] + sgn * st
            if v <= 0 and k not in ("MaxEquityDD_Pct",):
                continue
            c[k] = round(v, 2)
            jobs.append((f"{k} {'-' if sgn < 0 else '+'}{st}", c))
    for k in ("NewsPauseLayers", "UseTimeFilter", "NewsFilter"):
        if k in base:
            c = dict(base)
            c[k] = 1 - int(base[k])
            jobs.append((f"{k} -> {c[k]}", c))
    with Pool(os.cpu_count(), initializer=opt._init, initargs=(npz,)) as pool:
        rows = pool.map(_job, jobs)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    print(df.round(1).to_string(index=False))
    nb = df[df.variant != "base"]
    print(f"\nneighbours: {len(nb)}  OOS>0: {(nb.oos_cagr > 0).mean():.0%}  full>0: {(nb.full_cagr > 0).mean():.0%}"
          f"  ruined: {int(nb.ruined.sum())}  median OOS CAGR {nb.oos_cagr.median():.1f}%")


if __name__ == "__main__":
    main()
