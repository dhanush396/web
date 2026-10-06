"""Pick the configuration at the centre of the best plateau, not the single best point.

For the top-K Bayesian trials (ranked by the worse of their IS and OOS scores) every
one-step neighbour is re-run on the full 2012-2020.05 period; the winner maximises the
MEDIAN neighbour score and has no ruined neighbour (except the deliberate news-filter-off
ablation, which is reported separately).

NOTE: this step uses OOS data for selection, so its numbers are no longer out-of-sample.
The clean estimate of what an optimiser like this delivers is walk_forward.csv.

Usage: BG_ACCOUNT=cent_swapfree python select_plateau.py <npz> <top40.csv>[,<top40.csv>...] <out_candidates.json> [K]
"""
import json
import math
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimize as opt  # noqa: E402
from robust import STEPS  # noqa: E402


def row_to_cfg(row, cols):
    cfg = {}
    for c in cols:
        v = row[c]
        if isinstance(v, float) and math.isnan(v):
            continue
        cfg[c] = v.item() if hasattr(v, "item") else v
    return cfg


def neighbours(base):
    out = [base]
    for k, st in STEPS.items():
        if k not in base:
            continue
        for sgn in (-1, 1):
            v = round(base[k] + sgn * st, 2)
            if v <= 0 and k != "MaxEquityDD_Pct":
                continue
            c = dict(base)
            c[k] = v
            out.append(c)
    return out


def _full(cfg):
    r = opt.evaluate(cfg, opt.FULL_PERIOD)
    return r["score"], r["cagr_pct"], r["max_dd_pct"], r["ruined"]


def main():
    npz = sys.argv[1]
    files = sys.argv[2].split(",")
    out = sys.argv[3]
    k = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df["minscore"] = df[["is_score", "oos_score"]].min(axis=1)
    cols = [c for c in df.columns if not c.startswith(("is_", "oos_")) and c != "minscore"]
    df = df.sort_values("minscore", ascending=False).drop_duplicates(
        subset=["GridSpacingPts", "TP_Pips", "MaxLayers", "EmergencyCloseDD_Pct", "BaseLot"]).head(k)
    bases = [row_to_cfg(r, cols) for _, r in df.iterrows()]
    jobs, owner = [], []
    for i, b in enumerate(bases):
        for c in neighbours(b):
            jobs.append(c)
            owner.append(i)
    with Pool(os.cpu_count(), initializer=opt._init, initargs=(npz,)) as pool:
        res = pool.map(_full, jobs)
    rows = []
    for i, b in enumerate(bases):
        rs = [r for o, r in zip(owner, res) if o == i]
        sc = np.array([r[0] for r in rs])
        rows.append(dict(idx=i, spacing=b["GridSpacingPts"], tp=b["TP_Pips"], layers=b["MaxLayers"],
                         basket=b["EmergencyCloseDD_Pct"], lot=b["BaseLot"], base_score=rs[0][0],
                         base_cagr=rs[0][1], base_dd=rs[0][2], med_nb_score=float(np.median(sc)),
                         med_nb_cagr=float(np.median([r[1] for r in rs])),
                         med_nb_dd=float(np.median([r[2] for r in rs])),
                         worst_nb_dd=float(max(r[2] for r in rs)), nb_ruined=int(sum(r[3] for r in rs)),
                         n=len(rs)))
    tab = pd.DataFrame(rows).sort_values(["nb_ruined", "med_nb_score"], ascending=[True, False])
    print(tab.round(1).to_string(index=False))
    best = bases[int(tab.iloc[0]["idx"])]
    tab.to_csv(out.replace(".json", "_plateau.csv"), index=False)
    json.dump({"recommended": best}, open(out, "w"), indent=1)
    print("recommended:", json.dumps(best))


if __name__ == "__main__":
    main()
