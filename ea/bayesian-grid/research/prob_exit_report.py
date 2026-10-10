"""Print every number quoted in HF_GRID_RESEARCH.md section 10 straight from the result files.

Usage: python prob_exit_report.py [results/prob_exit] [fit log]
"""
import os
import re
import sys

import numpy as np
import pandas as pd

R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "prob_exit")
LOG = sys.argv[2] if len(sys.argv) > 2 else None
HO, IS = "2017-2020.05", "2012-2016"
COLS = ["tp_stop", "edge_model_0.25", "floor_model_0.25", "floor_model_0.4", "edge_model_0", "time_300s"]


def main():
    coef = pd.read_csv(os.path.join(R, "prob_exit_coefficients.csv"))
    print("coefficients:")
    for _, r in coef.iterrows():
        print(f"  {r.feature:7s} {r.coef:+.3f}  t {r.t:.1f}")
    if LOG and os.path.exists(LOG):
        m = re.search(r"intercept ([-\d.]+) \(se [\d.]+\), slope on logit\(P_fair\) ([-\d.]+)", open(LOG).read())
        if m:
            print(f"calibration of P_rw alone: a = {float(m.group(1)):.3f}, c = {float(m.group(2)):.3f}")
    ev = pd.read_csv(os.path.join(R, "prob_exit_model_eval.csv"))
    a = ev[ev.config == "ALL"].set_index("period")
    print(f"log-loss gain over P_rw: IS {a.loc[IS, 'logloss_gain_pct']:.4f}%  HO {a.loc[HO, 'logloss_gain_pct']:.4f}%")
    print(f"decision rows IS {int(a.loc[IS, 'rows']):,}; baskets IS {int(ev[ev.period == IS].baskets.sum()):,}")
    cal = pd.read_csv(os.path.join(R, "prob_exit_holdout_calibration.csv"))
    print("holdout calibration deciles (model / random walk / observed):")
    for _, r in cal.iterrows():
        print(f"  {r.p_model:.3f} / {r.p_random_walk:.3f} / {r.observed:.3f}")

    d = pd.read_csv(os.path.join(R, "prob_exit_backtests_fixed.csv"))
    v = d[d.account == "vantage_fixed_lots"]
    z = d[d.account == "zero_cost_fixed_lots"]
    print("\n$ per year, Vantage, stop 40:")
    t = v[v.stop == 40].pivot_table(index=["grid", "tp", "period"], columns="variant", values="usd_per_year")[COLS]
    print(t.round(0).to_string())
    h = v[v.period == HO].groupby("variant")[["p_tp", "avg_hold_min", "usd_per_basket", "prob_exit_share", "stop_share"]].mean()
    print("\n12-cell means, Vantage, 2017-20:")
    print(h.round(3).to_string())
    tp = v[v.variant == "tp_stop"]
    print(f"\nhold-to-TP p_tp range over all cells/periods: {tp.p_tp.min():.3f} .. {tp.p_tp.max():.3f}")
    for name, acc in (("Vantage", v), ("zero cost", z)):
        p = acc.pivot_table(index=["grid", "tp", "stop", "period"], columns="variant", values="usd_per_year")
        out = {}
        for c in p.columns:
            if c == "tp_stop":
                continue
            dlt = p[c] - p["tp_stop"]
            out[c] = f"{int((dlt > 1e-6).sum())} more / {int((dlt < -1e-6).sum())} less"
        print(f"{name}: vs hold-to-TP across {len(p)} cell x period: {out}")
    pv = v.pivot_table(index=["grid", "tp", "stop", "period"], columns="variant", values="usd_per_year")
    dl = pv["edge_model_0.25"] - pv["tp_stop"]
    print(f"EDGE 0.25 - hold-to-TP $/yr: more in {int((dl > 0).sum())} (by {dl[dl > 0].min():.0f}..{dl[dl > 0].max():.0f}),"
          f" less in {int((dl < 0).sum())} (by {(-dl[dl < 0]).min():.0f}..{(-dl[dl < 0]).max():.0f})")
    zh = z[z.period == HO].groupby("variant").usd_per_basket.mean()
    vh = v[v.period == HO].groupby("variant").usd_per_basket.mean()
    pz = z.pivot_table(index=["grid", "tp", "stop", "period"], columns="variant", values="usd_per_basket")
    print(f"zero cost $/grid 2017-20: hold {zh['tp_stop']:.4f}, FLOOR 0.25 {zh['floor_model_0.25']:.4f}; "
          f"FLOOR 0.25 lower in {int((pz['floor_model_0.25'] < pz['tp_stop']).sum())} of {len(pz)}; "
          f"share of the real-cost shortfall that is gross: "
          f"{(zh['tp_stop'] - zh['floor_model_0.25']) / (vh['tp_stop'] - vh['floor_model_0.25']):.0%}")
    pre = v[(v.grid == 15) & (v.tp == 8) & (v.stop == 40) & (v.variant == "edge_model_0.25") & (v.period == HO)].iloc[0]
    print(f"preset cell 15/8/40 EDGE 0.25, 2017-20: TP {pre.p_tp:.3f}, prob exit {pre.prob_exit_share:.4f}, "
          f"stop {pre.stop_share:.4f}, $/grid {pre.usd_per_basket:.3f}, $/yr {pre.usd_per_year:.0f}")

    u = pd.read_csv(os.path.join(R, "prob_exit_backtests_usd100.csv"))
    u = u[u.account == "vantage_raw_ecn"]
    print(f"\n$100 Vantage: {len(u)} runs, above $100: {int((u.final_usd > 100).sum())}, frozen: {int(u.frozen.sum())}")
    print(u.groupby("stop").final_usd.agg(["min", "max"]).round(1).to_string())


if __name__ == "__main__":
    main()
