"""Probability view of the HF grid: sample space, events, conditional probability, independence.

Usage: python prob_edge.py <eurusd_m1.npz> <out_dir> [all_in_cost_pips=0.8]

Part A - the real strategy (bgrid_bt simulator, Vantage Raw ECN costs, balance large enough that
         the account never stops out, so every basket is counted): the sample space of one basket
         Omega = {TP, time stop, session/news close, side stop} with P(event) and the mean $ of each.
Part B - the building block (one 0.01 entry at an M1 close, mid prices): the events
         W = "+a pips before -b pips", L = "-b before +a", T = "neither within H minutes".
         For a driftless price P(W | resolved) = b/(a+b) and E[gross] = 0 whatever a, b, H are
         (optional stopping), so the only thing that can pay the cost c is a conditional
         probability P(W | state) above the random-walk value. Cells of state are scored on
         2012-2016 and the ones that clear break-even are re-measured, untouched, on 2017-2020.05.
         Same-bar W/L touches are ambiguous on M1; they are reported as bounds (all-L .. all-W)
         and split 50/50 for the point estimate.
Part C - independence: consecutive non-overlapping trades, P(L | previous L) vs P(L), and the
         frequency of 5-loss streaks vs the independent-trials value P(L)^5.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgrid_bt as bt  # noqa: E402
import news_calendar as nc  # noqa: E402
import vantage_run as vr  # noqa: E402

PIP = 1e-4
IS = ("2012-01-01", "2017-01-01")
HO = ("2017-01-01", "2020-06-01")
BRACKETS = [(1.0, 2.0, 5), (3.0, 5.0, 5), (1.0, 2.0, 30), (3.0, 5.0, 30), (5.3, 7.5, 45)]
SESS = [(60, 300, "01-05"), (300, 540, "05-09"), (540, 780, "09-13"), (780, 1020, "13-17"),
        (1020, 1200, "17-20"), (1200, 1380, "20-23")]
Z_EDGES = [-np.inf, -2.5, -1.5, -0.5, 0.5, 1.5, 2.5, np.inf]
Z_LAB = ["<-2.5", "-2.5..-1.5", "-1.5..-0.5", "-0.5..0.5", "0.5..1.5", "1.5..2.5", ">2.5"]
TR_EDGES = [0, 1, 2, np.inf]
MIN_N = 1000


def epoch(s):
    return pd.Timestamp(s).value // 10**9


# ---------------------------------------------------------------- Part A
def part_a(npz, cost_note):
    d = bt.Data(npz)
    news = np.array(nc.server_epochs(2011, 2021), dtype=np.float64)
    models = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results",
                                         "vantage_models.json")))["backtest_models"]
    m = next(x for x in models if x["name"] == "vantage_raw_ecn")
    acc = dict(vr.account(m), Balance=1e6)
    rows = []
    for sp, tp in [(20, 1.0), (50, 3.0), (75, 5.3)]:
        for per, (a, b) in (("2012-2016", IS), ("2017-2020.05", HO)):
            c = dict(acc, **vr.CORE, GridSpacingPts=sp, TP_Pips=tp)
            r = bt.run(d.slice(a, b), bt.make_params(**c), news=news)
            n_tp = r["wins"] + r["losses"]
            n_other = r["time_stops"] + r["side_stops"] + r["session_closes"] + r["news_closes"]
            n = n_tp + n_other
            other_pnl = r["stop_losses"]  # time stops, side stops, session and news closes
            # the opening half of the commission is booked at entry, outside tp_profit/stop_losses;
            # spread it evenly over baskets so each event's payoff is all-in (approximation)
            open_comm = (r["final_balance"] - 1e6 - r["tp_profit"] - other_pnl - r["swap"]) / max(n, 1)
            w = r["tp_profit"] / max(n_tp, 1) + open_comm
            lo = other_pnl / max(n_other, 1) + open_comm
            p = n_tp / max(n, 1)
            rows.append(dict(grid_pips=sp / 10, tp_pips=tp, period=per, baskets=int(n),
                             P_tp=p, P_time_or_close=1 - p, avg_tp_usd=w, avg_other_usd=lo,
                             E_check_usd=p * w + (1 - p) * lo,
                             p_breakeven=(-lo / (w - lo)) if w > lo and lo < 0 else np.nan,
                             E_basket_usd=(r["final_balance"] - 1e6) / max(n, 1),
                             net_usd=r["final_balance"] - 1e6, commission_usd=r["commission"],
                             swap_usd=r["swap"]))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- Part B
def features(t, o, h, lo, c):
    s = pd.Series(c)
    r1 = s.diff() / PIP
    sig1 = np.sqrt((r1 ** 2).ewm(halflife=60, min_periods=120).mean())
    z = ((s - s.ewm(span=20, adjust=False).mean()) / PIP / (sig1 * np.sqrt(15))).to_numpy()
    trend = (r1.rolling(60).sum() / (sig1 * np.sqrt(60))).to_numpy()  # signed
    mod = (t % 86400) // 60
    dow = ((t // 86400) + 3) % 7  # 0 = Monday
    return z, sig1.to_numpy(), trend, mod, dow


def first_passage(t, h, lo, c, idx, a, b, hmin, side):
    """outcome: 1 W, 2 L, 3 ambiguous (both in one bar), 0 timeout; dt = exit move in pips (timeout);
    k = bars to resolution."""
    n = len(c)
    out = np.zeros(len(idx), np.int8)
    k_res = np.full(len(idx), hmin, np.int32)
    last = c[idx].copy()
    open_ = np.ones(len(idx), bool)
    c0 = c[idx]
    for k in range(1, hmin + 1):
        j = np.minimum(idx + k, n - 1)
        live = open_ & (t[j] - t[idx] <= 60 * hmin) & (idx + k < n)
        if not live.any():
            break
        up = (h[j] - c0) / PIP
        dn = (c0 - lo[j]) / PIP
        win = (up >= a - 1e-9) if side > 0 else (dn >= a - 1e-9)
        los = (dn >= b - 1e-9) if side > 0 else (up >= b - 1e-9)
        w = live & win & ~los
        l_ = live & los & ~win
        am = live & win & los
        out[w] = 1
        out[l_] = 2
        out[am] = 3
        res = w | l_ | am
        k_res[res] = k
        last[live] = c[j][live]
        open_ &= ~res
    dt = side * (last - c0) / PIP
    dt[out != 0] = 0.0
    return out, dt, k_res


def score(out, dt, a, b, cost):
    n = len(out)
    pw = (out == 1).mean(); pl = (out == 2).mean(); pa = (out == 3).mean(); pt = (out == 0).mean()
    et = dt[out == 0].sum() / max(n, 1)
    g_mid = a * (pw + pa / 2) - b * (pl + pa / 2) + et
    g_lo = a * pw - b * (pl + pa) + et
    g_hi = a * (pw + pa) - b * pl + et
    res = pw + pl + pa
    return dict(n=n, P_W=pw, P_L=pl, P_amb=pa, P_T=pt,
                P_W_given_resolved=(pw + pa / 2) / res if res else np.nan,
                rw_value=b / (a + b), gross_pips=g_mid, gross_lo=g_lo, gross_hi=g_hi,
                net_pips=g_mid - cost, net_lo=g_lo - cost)


def part_b(npz, cost, out_dir):
    z_ = np.load(npz)
    t = z_["t_srv"].astype(np.int64); h = z_["h"]; lo = z_["l"]; c = z_["c"]; o = z_["o"]
    z, sig, trend, mod, dow = features(t, o, h, lo, c)
    news = np.array(nc.server_epochs(2011, 2021), dtype=np.int64)
    pos = np.searchsorted(news, t)
    near = np.zeros(len(t), bool)
    for off in (0, 1):
        q = np.clip(pos - off, 0, len(news) - 1)
        near |= np.abs(news[q] - t) <= 30 * 60
    ok = (mod >= 60) & (mod < 1380) & (dow < 5) & ~near & np.isfinite(z) & np.isfinite(trend)
    is_m = ok & (t >= epoch(IS[0])) & (t < epoch(IS[1]))
    ho_m = ok & (t >= epoch(HO[0])) & (t < epoch(HO[1]))
    vq = np.nanquantile(sig[is_m], [1 / 3, 2 / 3])
    sess_b = np.full(len(t), -1, np.int8)
    for i, (s0, s1, _) in enumerate(SESS):
        sess_b[(mod >= s0) & (mod < s1)] = i
    vol_b = np.digitize(sig, vq).astype(np.int8)

    unc_rows, cell_rows, sel_rows, dep_rows = [], [], [], []
    for a, b, hm in BRACKETS:
        tag = f"TP {a:g} / grid {b:g} / {hm}m"
        per = {}
        for pname, msk in (("2012-2016", is_m), ("2017-2020.05", ho_m)):
            idx = np.flatnonzero(msk)
            outs, dts, ks, zd, tr = [], [], [], [], []
            for side in (1, -1):
                o_, d_, k_ = first_passage(t, h, lo, c, idx, a, b, hm, side)
                outs.append(o_); dts.append(d_); ks.append(k_)
                zd.append(-side * z[idx])          # > 0: the trade fades a stretch
                tr.append(-side * trend[idx])      # > 0: the trade is against the 60-min trend
            per[pname] = dict(idx=np.concatenate([idx, idx]), out=np.concatenate(outs),
                              dt=np.concatenate(dts), k=np.concatenate(ks), zd=np.concatenate(zd),
                              tr=np.concatenate(tr), side=np.concatenate([np.ones(len(idx)), -np.ones(len(idx))]))
            unc_rows.append(dict(bracket=tag, period=pname, **score(per[pname]["out"], per[pname]["dt"], a, b, cost)))

        def cells(P):
            zb = np.digitize(P["zd"], Z_EDGES[1:-1])
            tb = np.digitize(np.abs(P["tr"]), TR_EDGES[1:-1])
            return sess_b[P["idx"]], zb, vol_b[P["idx"]], tb

        ci = cells(per["2012-2016"]); ch = cells(per["2017-2020.05"])
        key_i = ((ci[0] * 7 + ci[1]) * 3 + ci[2]) * 3 + ci[3]
        key_h = ((ch[0] * 7 + ch[1]) * 3 + ch[2]) * 3 + ch[3]
        passing_i, passing_h = [], []
        for key in np.unique(key_i):
            mi = key_i == key
            if mi.sum() < MIN_N:
                continue
            s_i = score(per["2012-2016"]["out"][mi], per["2012-2016"]["dt"][mi], a, b, cost)
            tb_ = key % 3; vb = (key // 3) % 3; zb = (key // 9) % 7; sb = key // 63
            lab = dict(session=SESS[sb][2], zdir=Z_LAB[zb], vol=["low", "mid", "high"][vb],
                       trend_abs=["<1", "1-2", ">2"][tb_])
            mh = key_h == key
            s_h = score(per["2017-2020.05"]["out"][mh], per["2017-2020.05"]["dt"][mh], a, b, cost) if mh.any() else None
            row = dict(bracket=tag, **lab, n_is=s_i["n"], P_W_res_is=s_i["P_W_given_resolved"],
                       gross_is=s_i["gross_pips"], net_is=s_i["net_pips"], net_lo_is=s_i["net_lo"],
                       n_ho=s_h["n"] if s_h else 0, gross_ho=s_h["gross_pips"] if s_h else np.nan,
                       net_ho=s_h["net_pips"] if s_h else np.nan)
            cell_rows.append(row)
            if s_i["net_pips"] > 0:
                passing_i.append(mi); passing_h.append(mh)
        if passing_i:
            mi = np.logical_or.reduce(passing_i); mh = np.logical_or.reduce(passing_h)
            si = score(per["2012-2016"]["out"][mi], per["2012-2016"]["dt"][mi], a, b, cost)
            sh = score(per["2017-2020.05"]["out"][mh], per["2017-2020.05"]["dt"][mh], a, b, cost)
            days_i = (epoch(IS[1]) - epoch(IS[0])) / 86400 * 5 / 7
            days_h = (epoch(HO[1]) - epoch(HO[0])) / 86400 * 5 / 7
            sel_rows.append(dict(bracket=tag, cells_passing_is=len(passing_i),
                                 signals_day_is=si["n"] / days_i, net_is=si["net_pips"], net_lo_is=si["net_lo"],
                                 signals_day_ho=sh["n"] / days_h, gross_ho=sh["gross_pips"], net_ho=sh["net_pips"],
                                 net_lo_ho=sh["net_lo"]))
        else:
            sel_rows.append(dict(bracket=tag, cells_passing_is=0))

        # Part C: independence along a chain of non-overlapping long-and-short alternating trades
        for pname in per:
            P = per[pname]
            n2 = len(P["idx"]) // 2
            res = []
            for side_off in (0, n2):
                i = 0
                ids = P["idx"][side_off:side_off + n2]
                while i < n2:
                    o_ = P["out"][side_off + i]
                    res.append(o_)
                    nxt = ids[i] + P["k"][side_off + i] + 1
                    i = int(np.searchsorted(ids, nxt))
            r = np.array(res)
            lossy = (r == 2) | (r == 3)
            pL = lossy.mean()
            pLL = (lossy[1:] & lossy[:-1]).sum() / max(lossy[:-1].sum(), 1)
            streak = np.convolve(lossy.astype(int), np.ones(5, int), "valid") == 5
            dep_rows.append(dict(bracket=tag, period=pname, trades=len(r), P_L=pL, P_L_given_prev_L=pLL,
                                 P_5_streak_observed=streak.mean(), P_5_streak_if_independent=pL ** 5))
        print("done", tag, flush=True)

    unc = pd.DataFrame(unc_rows); cel = pd.DataFrame(cell_rows); sel = pd.DataFrame(sel_rows)
    dep = pd.DataFrame(dep_rows)
    unc.to_csv(os.path.join(out_dir, "prob_unconditional.csv"), index=False)
    cel.to_csv(os.path.join(out_dir, "prob_conditional_cells.csv"), index=False)
    sel.to_csv(os.path.join(out_dir, "prob_selected_oos.csv"), index=False)
    dep.to_csv(os.path.join(out_dir, "prob_independence.csv"), index=False)
    return unc, cel, sel, dep


def main():
    npz, out = sys.argv[1], sys.argv[2]
    cost = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
    os.makedirs(out, exist_ok=True)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    a = part_a(npz, cost)
    a.to_csv(os.path.join(out, "prob_basket_sample_space.csv"), index=False)
    print("\n=== Part A: basket sample space, Vantage Raw ECN (balance 1e6, fixed 0.01 ladder) ===")
    print(a.round(3).to_string(index=False))
    unc, cel, sel, dep = part_b(npz, cost, out)
    print(f"\n=== Part B: unconditional events (cost {cost} pips all-in) ===")
    print(unc.round(3).to_string(index=False))
    print("\n=== Part B: best conditional cells in 2012-2016 and their 2017-2020.05 value ===")
    top = cel.sort_values("net_is", ascending=False).groupby("bracket").head(5)
    print(top.round(3).to_string(index=False))
    print("\n=== Part B: all cells with net_is > 0, pooled, re-measured out of sample ===")
    print(sel.round(3).to_string(index=False))
    print("\n=== Part C: independence ===")
    print(dep.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
