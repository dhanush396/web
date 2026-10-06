"""Charts + candidate table for the README.

Usage: BG_ACCOUNT=cent_swapfree python report.py <eurusd_m1.npz> <results_dir> <candidates.json> <img_dir>
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimize as opt  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # first three slots: validated all-pairs
LABELS = {"A_recommended": "A — recommended preset", "B_slow_swapfree_only": "B — slow grid (swap-free only)",
          "v612_live_settings": "v6.12 live settings (BaseLot 0.08, no stop)"}


def style(ax, title, ylabel):
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel, color=INK2, fontsize=9)


def scatter(results_dir, img_dir):
    df = pd.read_csv(os.path.join(results_dir, "grid_search.csv"))
    ok = (df.is_ruined == 0) & (df.oos_ruined == 0)
    fig, ax = plt.subplots(figsize=(8, 5.2), dpi=130)
    style(ax, f"In-sample vs out-of-sample CAGR — {len(df):,} grid-search configs ({opt.ACCOUNT_NAME})",
          "OOS CAGR 2017–2020.05 (%)")
    x, y = df.is_cagr_pct.clip(-100, 60), df.oos_cagr_pct.clip(-100, 60)
    ax.scatter(x[~ok], y[~ok], s=14, color=SERIES[1], alpha=0.55, linewidths=0, label="ruined in IS or OOS")
    ax.scatter(x[ok], y[ok], s=14, color=SERIES[0], alpha=0.8, linewidths=0, label="survived both")
    ax.axhline(0, color=INK2, linewidth=1)
    ax.axvline(0, color=INK2, linewidth=1)
    ax.set_xlabel("IS CAGR 2012–2016 (%)", color=INK2, fontsize=9)
    rho = df.is_score.corr(df.oos_score, method="spearman")
    ax.text(0.99, 0.98, f"Spearman(IS, OOS score) = {rho:.2f}\nconfigs positive in both & never ruined: "
            f"{int((ok & (df.is_cagr_pct > 0) & (df.oos_cagr_pct > 0)).sum())}",
            transform=ax.transAxes, ha="right", va="top", color=INK2, fontsize=9,
            bbox=dict(facecolor=SURFACE, edgecolor="none", pad=3))
    leg = ax.legend(loc="upper left", frameon=False, fontsize=9)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.tight_layout()
    path = os.path.join(img_dir, "is_vs_oos_scatter.png")
    fig.savefig(path, facecolor=SURFACE)
    print("wrote", path)


def equity(npz, cands, img_dir):
    opt._init(npz)
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=130)
    style(ax, "Equity of a $100 account (daily, mark-to-market), 2012 – 2020.05", "equity (USD)")
    rows = []
    for k, (name, cfg) in enumerate(cands.items()):
        r = opt.evaluate(cfg, opt.FULL_PERIOD, daily=True)
        e = pd.Series(r["daily_equity"]) / opt.BAL0 * 100.0
        day0 = int(opt._D.slice(*opt.FULL_PERIOD).t[0] // 86400)
        e.index = pd.to_datetime((day0 + np.arange(len(e))) * 86400, unit="s")
        e = e.ffill().dropna()
        ax.plot(e.index, e.values, color=SERIES[k % 3], linewidth=2, label=LABELS.get(name, name))
        ax.text(e.index[-1], e.values[-1], f"  ${e.values[-1]:.0f}", color=INK, fontsize=9, va="center")
        a = opt.evaluate(cfg, opt.IS_PERIOD)
        b = opt.evaluate(cfg, opt.OOS_PERIOD)
        rows.append(dict(candidate=name,
                         full_final_usd=round(r["final_equity"] / opt.BAL0 * 100, 2),
                         full_cagr=round(r["cagr_pct"], 1), full_maxdd=round(r["max_dd_pct"], 1),
                         is_cagr=round(a["cagr_pct"], 1), is_maxdd=round(a["max_dd_pct"], 1),
                         oos_cagr=round(b["cagr_pct"], 1), oos_maxdd=round(b["max_dd_pct"], 1),
                         basket_stops=int(r["basket_stops"]), grids_won=int(r["wins"]),
                         worst_float_usd=round(r["worst_float"] / opt.BAL0 * 100, 2),
                         ruined=int(r["ruined"])))
    ax.axvline(pd.Timestamp("2017-01-01"), color=INK2, linewidth=1, linestyle="--")
    ax.text(pd.Timestamp("2017-01-15"), ax.get_ylim()[1], "out-of-sample →", color=INK2, fontsize=9, va="top")
    ax.axhline(100, color=GRID, linewidth=1)
    leg = ax.legend(loc="upper left", frameon=False, fontsize=9)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.tight_layout()
    path = os.path.join(img_dir, "equity_candidates.png")
    fig.savefig(path, facecolor=SURFACE)
    print("wrote", path)
    tab = pd.DataFrame(rows)
    print(tab.to_string(index=False))
    return tab


if __name__ == "__main__":
    npz, results_dir, cand_path, img_dir = sys.argv[1:5]
    os.makedirs(img_dir, exist_ok=True)
    acc_dir = os.path.join(results_dir, opt.ACCOUNT_NAME)
    scatter(acc_dir, img_dir)
    cands = json.load(open(cand_path))
    tab = equity(npz, cands, img_dir)
    tab.to_csv(os.path.join(acc_dir, "candidates_table.csv"), index=False)
