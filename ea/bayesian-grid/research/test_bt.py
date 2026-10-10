"""Hand-computed sanity tests for the backtester (run: python test_bt.py)."""
import numpy as np

import bgrid_bt as bt

MON = 1_420_416_000.0  # 2015-01-05 00:00 (a Monday), naive server seconds


class Synth:
    def __init__(self, bars):
        a = np.array(bars, dtype=float)
        self.t = MON + 60.0 * np.arange(len(a))
        self.o, self.h, self.l, self.c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]


ZERO_COST = dict(SpreadPts=0, SlippagePts=0, CommPerLotRT=0, SwapLong=0, SwapShort=0,
                 RolloverSpreadMult=1, Balance=1000)


def test_basic_cycle():
    # open both at 1.10000; down 30 pips (sell TP, 3 buy adds); back up to buy TP
    bars = [
        (1.10000, 1.10000, 1.10000, 1.10000),
        (1.10000, 1.10000, 1.09950, 1.09950),  # sell TP @1.09950 -> +$0.50
        (1.09950, 1.09950, 1.09700, 1.09700),  # buy adds @1.0990, 1.0980, 1.0970
        (1.09700, 1.09900, 1.09700, 1.09900),  # buy TP @ wavg 1.0985 + 5 pips = 1.0990
    ]
    p = bt.make_params(BaseLot=0.01, FlatLayers=5, LotIncrement=0, GridSpacingPts=100, TP_Pips=5,
                       MaxLayers=5, **ZERO_COST)
    r = bt.run(Synth(bars), p, news=None)
    assert r["wins"] == 2, r
    assert r["layers_added"] == 3, r
    assert abs(r["final_balance"] - 1002.50) < 1e-6, r["final_balance"]


def test_lot_ladder():
    p = bt.make_params(BaseLot=0.08, FlatLayers=5, LotIncrement=0.07, LotIncEvery=1)
    lots = [bt._lot_size(k, p) for k in range(1, 19)]
    assert lots[:5] == [0.08] * 5 and abs(lots[5] - 0.15) < 1e-9 and abs(lots[17] - 0.99) < 1e-9, lots
    p = bt.make_params(BaseLot=0.01, FlatLayers=4, LotIncrement=0.01, LotIncEvery=3)
    lots = [bt._lot_size(k, p) for k in range(1, 12)]
    assert lots == [0.01] * 4 + [0.02] * 3 + [0.03] * 3 + [0.04], lots


def test_basket_stop_exact():
    # buy grid only goes down, basket stop at 1% of 1000 = $10 float loss
    bars = [(1.10000, 1.10000, 1.10000, 1.10000)] + [
        (1.10000 - 0.001 * k, 1.10000 - 0.001 * k, 1.09900 - 0.001 * k, 1.09900 - 0.001 * k)
        for k in range(30)
    ]
    p = bt.make_params(BaseLot=0.01, FlatLayers=50, LotIncrement=0, GridSpacingPts=100, TP_Pips=5,
                       MaxLayers=3, EmergencyCloseDD_Pct=1.0, HaltCooldownMin=0, **ZERO_COST)
    r = bt.run(Synth(bars), p)
    assert r["basket_stops"] == 1, r
    # sell made +$0.50 at its TP; buy loss stopped at exactly -$10 float
    assert abs(r["final_balance"] - (1000 + 0.5 - 10.0)) < 1e-6, r["final_balance"]


def test_stopout():
    bars = [(1.10000, 1.10000, 1.10000, 1.10000)] + [
        (1.10000 - 0.002 * k, 1.10000 - 0.002 * k, 1.09800 - 0.002 * k, 1.09800 - 0.002 * k)
        for k in range(200)
    ]
    p = bt.make_params(BaseLot=0.01, FlatLayers=50, LotIncrement=0, GridSpacingPts=100, TP_Pips=5,
                       MaxLayers=10, **dict(ZERO_COST, Balance=30))
    r = bt.run(Synth(bars), p)
    assert r["stopouts"] == 1 and r["ruined"] == 1, r


def test_news_blocks_open_and_pauses_layers():
    flat = (1.10000, 1.10000, 1.10000, 1.10000)
    # 60 flat minutes, event at minute 30 -> window [0, 60] blocks the M15 opens at 0/15/30/45
    bars = [flat] * 60 + [flat] * 5
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, NewsFilter=1,
                       NewsBeforeMin=30, NewsAfterMin=30, **ZERO_COST)
    news = [MON + 30 * 60]
    r = bt.run(Synth(bars), p, news=news)
    assert r["grids_opened"] == 0, r
    p_off = bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, NewsFilter=0, **ZERO_COST)
    assert bt.run(Synth(bars), p_off, news=news)["grids_opened"] == 2

    # grid opened at 00:00, event at 01:00 with a 15-min window: a 30-pip drop at 00:50
    # adds layers only when pause is off
    drop = [(1.10000 - 0.001 * k, 1.10000 - 0.001 * k, 1.09900 - 0.001 * k, 1.09900 - 0.001 * k) for k in range(3)]
    bars = [flat] * 50 + drop + [(1.09700, 1.09700, 1.09700, 1.09700)] * 5
    news = [MON + 60 * 60]
    kw = dict(BaseLot=0.01, GridSpacingPts=100, TP_Pips=50, MaxLayers=5, NewsFilter=1, NewsBeforeMin=15,
              NewsAfterMin=15, **ZERO_COST)
    paused = bt.run(Synth(bars), bt.make_params(NewsPauseLayers=1, **kw), news=news)
    active = bt.run(Synth(bars), bt.make_params(NewsPauseLayers=0, **kw), news=news)
    assert paused["layers_added"] == 0 and active["layers_added"] == 3, (paused, active)


def test_max_hold_time_stop():
    flat = (1.10000, 1.10000, 1.10000, 1.10000)
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, MaxHoldMin=45, **ZERO_COST)
    r = bt.run(Synth([flat] * 50), p)
    # 00:00 pair stopped at 00:45. Like the EA (LatchSide blocks the entry block on that tick and the
    # M15 bar's entry chance is used up), the sides re-open only on the next M15 bar (01:00, not in the data)
    assert r["time_stops"] == 2 and r["grids_opened"] == 2, r
    assert abs(r["max_hold_min"] - 45.0) < 1e-9, r


def test_geometric_ladder_from_001():
    p = bt.make_params(BaseLot=0.01, FlatLayers=1, LotMultiplier=1.5)
    lots = [bt._lot_size(k, p) for k in range(1, 7)]
    assert lots == [0.01, 0.02, 0.02, 0.03, 0.05, 0.08], lots
    p = bt.make_params(BaseLot=0.01, FlatLayers=1, LotIncrement=0.01, LotIncEvery=1)
    assert [bt._lot_size(k, p) for k in range(1, 5)] == [0.01, 0.02, 0.03, 0.04]


def test_session_window_and_entry_tf():
    flat = (1.10000, 1.10000, 1.10000, 1.10000)
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, EntryTFMin=1,
                       SessStartMin=30, SessEndMin=40, SessCloseAtEnd=1, **ZERO_COST)
    r = bt.run(Synth([flat] * 60), p)
    assert r["grids_opened"] == 2 and r["session_closes"] == 1, r  # opened 00:30, flattened at 00:40
    # entry on M1: a TP hit is re-armed on the very next minute (M15 would wait for 00:15)
    up = [flat, (1.1, 1.1006, 1.1, 1.1006), (1.1006, 1.1006, 1.1006, 1.1006)]
    r1 = bt.run(Synth(up), bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5,
                                          EntryTFMin=1, **ZERO_COST))
    r15 = bt.run(Synth(up), bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5,
                                           EntryTFMin=15, **ZERO_COST))
    assert r1["grids_opened"] == 3 and r15["grids_opened"] == 2, (r1, r15)


def test_daily_loss_limit():
    bars = [(1.10000, 1.10000, 1.10000, 1.10000)] + [
        (1.10000 - 0.001 * k, 1.10000 - 0.001 * k, 1.09900 - 0.001 * k, 1.09900 - 0.001 * k) for k in range(20)]
    bars += [bars[-1]] * 30  # quiet tape after the drop: only the daily stop keeps the EA flat
    kw = dict(BaseLot=0.01, FlatLayers=50, GridSpacingPts=100, TP_Pips=5, MaxLayers=3, EntryTFMin=1, **ZERO_COST)
    r = bt.run(Synth(bars), bt.make_params(DailyLossPct=1.0, **kw))
    free = bt.run(Synth(bars), bt.make_params(DailyLossPct=0.0, **kw))
    assert r["daily_stops"] == 1, r
    assert r["grids_opened"] + 30 <= free["grids_opened"], (r, free)  # no re-opens after the stop


def test_random_path_stays_in_bar_and_is_seeded():
    import numpy as np
    wp = np.zeros(4 + 3 * 5)
    wt = np.zeros(4 + 3 * 5)
    np.random.seed(3)
    k = bt._build_path(wp, wt, 1.1001, 1.1004, 1.0998, 1.1002, 5)
    pts = wp[:k]
    assert k == 4 + 3 * 5 and pts[0] == 1.1001 and pts[-1] == 1.1002
    assert pts.max() == 1.1004 and pts.min() == 1.0998
    assert np.all(np.diff(wt[:k]) > 0) and wt[0] == 0.0 and wt[k - 1] < 1.0  # time moves forward


def test_v7_hold_in_seconds():
    flat = (1.10000, 1.10000, 1.10000, 1.10000)
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, MaxHoldSec=30, **ZERO_COST)
    r = bt.run(Synth([flat] * 3), p)
    # 4-point path waypoints sit at 0/20/40/59 s: the 30 s limit fires at the 40 s waypoint
    assert r["time_stops"] == 2 and 30 <= r["max_hold_sec"] <= 40 + 1e-6, r


def test_v7_l1_anchored_stop():
    bars = [(1.10000, 1.10000, 1.10000, 1.10000), (1.10000, 1.10000, 1.09700, 1.09700)]
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=5, MaxLayers=5, StopPips=10, **ZERO_COST)
    r = bt.run(Synth(bars), p)
    assert r["side_stops"] == 1 and r["wins"] == 1, r
    assert abs(r["final_balance"] - (1000 + 0.5 - 1.0)) < 1e-6, r["final_balance"]


def test_v7_ladder_fit_to_budget():
    p = bt.make_params(BaseLot=0.01, FlatLayers=99, GridSpacingPts=30, MaxLayers=10, StopPips=10, **ZERO_COST)
    assert bt._fit_layers(p, 0.0, 1000.0) == 4  # layers 5+ would sit below the stop
    p = bt.make_params(BaseLot=0.01, FlatLayers=99, GridSpacingPts=30, MaxLayers=10, StopPips=10,
                       MaxBasketRiskPct=0.2, **ZERO_COST)
    assert bt._fit_layers(p, 0.0, 1000.0) == 2  # $1.0 + $0.7 fits $2.0; + $0.4 does not


def test_v7_add_pacing():
    bars = [(1.10000, 1.10000, 1.10000, 1.10000), (1.10000, 1.10000, 1.09650, 1.09650)]
    kw = dict(BaseLot=0.01, FlatLayers=99, GridSpacingPts=100, TP_Pips=50, MaxLayers=10, **ZERO_COST)
    free = bt.run(Synth(bars), bt.make_params(**kw))
    paced = bt.run(Synth(bars), bt.make_params(AddMinSec=60, **kw))
    assert free["layers_added"] == 3 and paced["layers_added"] == 1, (free, paced)


def test_v7_peak_kill_latch():
    bars = [(1.10000, 1.10000, 1.10000, 1.10000)] + [
        (1.10000 - 0.001 * k, 1.10000 - 0.001 * k, 1.09900 - 0.001 * k, 1.09900 - 0.001 * k) for k in range(10)]
    bars += [bars[-1]] * 40
    p = bt.make_params(BaseLot=0.01, FlatLayers=99, GridSpacingPts=100, TP_Pips=5, MaxLayers=5, EntryTFMin=1,
                       PeakKillPct=0.5, **ZERO_COST)
    r = bt.run(Synth(bars), p)
    assert r["killed"] == 1 and r["final_balance"] == r["final_equity"], r


def test_v7_stretch_signal_has_no_lookahead():
    import numpy as np
    rng = np.random.default_rng(0)
    c = 1.1 + np.cumsum(rng.normal(0, 0.0001, 3000))
    o = np.concatenate([[c[0]], c[:-1]])
    bars = [(o[i], max(o[i], c[i]) + 0.00005, min(o[i], c[i]) - 0.00005, c[i]) for i in range(3000)]
    d1 = Synth(bars)
    s1 = bt.Data.stretch_signal(_as_data(d1), 1.5, 90, 2.0, 0.0001)
    bars2 = list(bars)
    bars2[2000] = (o[2000], o[2000] + 0.01, o[2000] - 0.01, o[2000] - 0.009)  # violent bar 2000
    s2 = bt.Data.stretch_signal(_as_data(Synth(bars2)), 1.5, 90, 2.0, 0.0001)
    assert np.array_equal(s1[:2001], s2[:2001])  # signals up to bar 2000's open cannot see bar 2000
    assert (s1 != 0).sum() > 0


def test_review_hold_deadline_inside_leg_blocks_late_tp():
    # L1 at 00:00:00; bar 1 rises to the BUY TP only at ~00:01:40 (O->L->H->C waypoints 0/20/40/59 s).
    # MaxHoldSec=90 expires at 00:01:30, mid-leg, so the TP must NOT be credited.
    bars = [(1.10000, 1.10000, 1.10000, 1.10000), (1.10000, 1.10060, 1.10000, 1.10060)]
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=5, MaxLayers=1, MaxHoldSec=90, **ZERO_COST)
    r = bt.run(Synth(bars), p)
    assert r["wins"] == 0 and r["time_stops"] == 2, r
    assert r["max_hold_sec"] <= 90 + 1e-6, r


def test_review_kill_latch_sees_realised_loss_while_flat():
    # a 10-minute data gap jumps price 20 pips through the BUY stop: the $2 loss is realised at once
    # (no floating crossing to catch), so only the flat-account check can fire the 0.15% kill latch
    bars = [(1.10000, 1.10000, 1.10000, 1.10000)] + [(1.09800, 1.09800, 1.09800, 1.09800)] * 30
    sy = Synth(bars)
    sy.t = sy.t.copy()
    sy.t[1:] += 540.0
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=50, MaxLayers=1, StopPips=10, EntryTFMin=1,
                       PeakKillPct=0.15, MaxTotalLots=0.01, **ZERO_COST)  # lots cap: BUY grid only
    r = bt.run(sy, p)
    assert r["side_stops"] == 1 and r["killed"] == 1, r
    assert r["grids_opened"] == 1 and abs(r["final_balance"] - 998.0) < 1e-6, r  # nothing re-opens


def test_review_kill_fills_at_exact_crossing():
    # BUY grid only matters: SELL TP at -5 pips pays +$0.50, then the kill (0.5% of the $1000 peak = $995)
    # must close at exactly the crossing price, not at the bar low.
    bars = [(1.10000, 1.10000, 1.10000, 1.10000), (1.10000, 1.10000, 1.09000, 1.09000)]
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=5000, TP_Pips=5, MaxLayers=1, PeakKillPct=0.5, **ZERO_COST)
    r = bt.run(Synth(bars), p)
    assert r["killed"] == 1 and abs(r["final_balance"] - 995.0) < 1e-6, r


def test_review_spread_change_crosses_stop():
    import numpy as np
    flat = (1.10000, 1.10000, 1.10000, 1.10000)
    sy = Synth([flat, flat])
    sy.spread = np.array([0.0, 40.0])  # bar 1: 4-pip spread, mid unchanged
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=50, MaxLayers=1, StopPips=1, **ZERO_COST)
    r = bt.run(sy, p)
    assert r["side_stops"] == 2, r  # BUY bid and SELL ask both crossed their 1-pip stops


def _noise_bars(n, px=1.10000, amp=0.00001):
    """n quiet warm-up bars alternating +-amp around px (features need sigma > 0)."""
    bars = []
    prev = px
    for k in range(n):
        c = px + (amp if k % 2 == 0 else -amp)
        bars.append((prev, max(prev, c), min(prev, c), c))
        prev = c
    return bars


def test_v702_prob_floor_exit_at_bar_open():
    # both grids open at 1.10000 (TP 10, stop 10 pips). After a 5-pip drop the BUY grid is u=15 pips
    # from TP and v=5 pips from its stop: P_fair = 5/20 = 0.25 < ProbMin 0.30 -> exit at the next open.
    # The SELL grid (P_fair = 0.75) is kept. Zero coefficients: P = P_fair exactly.
    bars = _noise_bars(200)
    bars[0] = (1.10000, 1.10000, 1.10000, 1.10000)
    bars += [(1.10000, 1.10000, 1.09950, 1.09950)] + [(1.09950, 1.09950, 1.09950, 1.09950)] * 4
    kw = dict(BaseLot=0.01, GridSpacingPts=500, TP_Pips=10, MaxLayers=1, StopPips=10, ProbExitMode=1,
              ProbMin=0.30, **ZERO_COST)
    r = bt.run(_as_data(Synth(bars)), bt.make_params(**kw))
    assert r["prob_exits"] == 1 and r["side_stops"] == 0, r
    assert abs(r["final_balance"] - (1000 - 0.50)) < 1e-6, r["final_balance"]
    # a positive intercept (the model says the odds are better than a random walk) keeps the grid
    r = bt.run(_as_data(Synth(bars)), bt.make_params(**dict(kw, ProbB0=2.0)))
    assert r["prob_exits"] == 0, r
    # EDGE: a negative intercept beyond ProbEdge exits as soon as the features are valid
    r = bt.run(_as_data(Synth(bars)), bt.make_params(**dict(kw, ProbExitMode=2, ProbB0=-0.5, ProbEdge=0.25)))
    assert r["prob_exits"] >= 2, r


def test_v702_prob_features_have_no_lookahead():
    rng = np.random.default_rng(1)
    c = 1.1 + np.cumsum(rng.normal(0, 0.0001, 3000))
    o = np.concatenate([[c[0]], c[:-1]])
    bars = [(o[i], max(o[i], c[i]) + 0.00005, min(o[i], c[i]) - 0.00005, c[i]) for i in range(3000)]
    f1 = bt._prob_features(_as_data(Synth(bars)), 90, 240, 0.0001)
    bars2 = list(bars)
    bars2[2000] = (o[2000], o[2000] + 0.01, o[2000] - 0.01, o[2000] - 0.009)
    f2 = bt._prob_features(_as_data(Synth(bars2)), 90, 240, 0.0001)
    assert np.allclose(f1[:2001], f2[:2001], equal_nan=True)  # row i is known at bar i's open
    assert not np.allclose(f1[2001], f2[2001])
    assert np.isfinite(f1[500]).all()


def test_v702_training_labels():
    # BUY basket reaches its TP (label 1); the SELL basket hits its 10-pip stop (label 0)
    bars = [(1.10000, 1.10000, 1.10000, 1.10000), (1.10000, 1.10100, 1.10000, 1.10100)]
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=5, MaxLayers=1, StopPips=10,
                       ProbRecord=100, **ZERO_COST)
    r = bt.run(_as_data(Synth(bars)), p)
    assert r["wins"] == 1 and r["side_stops"] == 1, r
    assert list(r["prob_outcome"]) == [1.0, 0.0], r["prob_outcome"]
    rec = r["prob_rec"]
    assert rec.shape[0] == 2 and list(rec[:, 0]) == [0.0, 1.0], rec
    assert abs(rec[0, 3] - 5) < 1e-6 and abs(rec[0, 4] - 10) < 1e-6 and abs(rec[0, 7] - 2 / 3) < 1e-9, rec[0]


def test_v702_random_walk_probability_counts_future_adds():
    # BUY L1 at 1.10000, TP 5 pips, next add 10 pips lower, stop 30 pips below L1, 2 layers (flat 0.01).
    # Stage 1: (1.10000-1.09900)/(1.10050-1.09900) = 2/3. After the add at 1.09900 the TP drops to the new
    # average 1.09950 + 5 pips = 1.10000: (1.09900-1.09700)/(1.10000-1.09700) = 2/3. P = 2/3 + 1/3*2/3 = 8/9.
    p = bt.make_params(BaseLot=0.01, FlatLayers=5, LotIncrement=0, GridSpacingPts=100, TP_Pips=5, StopPips=30,
                       MaxLayers=2, **ZERO_COST)
    S = np.zeros(bt.NSTATE)
    S[bt.N0] = 1; S[bt.L0] = 0.01; S[bt.PV0] = 1.1 * 0.01; S[bt.LAST0] = 1.1; S[bt.TP0] = 1.1005; S[bt.FIRST0] = 1.1
    S[bt.EFFML0] = 2
    S[bt.N1] = 1; S[bt.L1] = 0.01; S[bt.PV1] = 1.1 * 0.01; S[bt.LAST1] = 1.1; S[bt.TP1] = 1.0995; S[bt.FIRST1] = 1.1
    S[bt.EFFML1] = 2
    assert abs(bt._p_rw(S, 0, 1.1, 0.0, p) - 8 / 9) < 1e-9, bt._p_rw(S, 0, 1.1, 0.0, p)
    assert abs(bt._p_rw(S, 1, 1.1, 0.0, p) - 8 / 9) < 1e-9, bt._p_rw(S, 1, 1.1, 0.0, p)
    S[bt.EFFML0] = 1  # ladder full: plain gambler's ruin between the TP and the stop, 30/35
    assert abs(bt._p_rw(S, 0, 1.1, 0.0, p) - 30 / 35) < 1e-9


def test_v702_prob_exit_latches_side_for_the_bar():
    # M1 entries: the BUY grid prob-exits at bar 201's open; like the EA (LatchSide), it re-opens on bar 202
    bars = _noise_bars(200)
    bars[0] = (1.10000, 1.10000, 1.10000, 1.10000)
    bars += [(1.10000, 1.10000, 1.09950, 1.09950)] + [(1.09950, 1.09950, 1.09950, 1.09950)] * 4
    p = bt.make_params(BaseLot=0.01, GridSpacingPts=500, TP_Pips=10, MaxLayers=1, StopPips=10, ProbExitMode=1,
                       ProbMin=0.30, EntryTFMin=1, ProbRecord=1000, **ZERO_COST)
    r = bt.run(_as_data(Synth(bars)), p)
    assert r["prob_exits"] == 1, r
    rec = r["prob_rec"]
    new_buy = rec[(rec[:, 1] == 1) & (rec[:, 0] > 1)]
    assert int(new_buy[0, 2]) == 203 and abs(new_buy[0, 6] - 1.0) < 1e-9, new_buy[:2]


def _as_data(sy):
    import numpy as np
    d = bt.Data.__new__(bt.Data)
    d.t, d.o, d.h, d.l, d.c = sy.t, sy.o, sy.h, sy.l, sy.c
    d.spread = np.zeros(0)
    d._sig = {}
    return d


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
