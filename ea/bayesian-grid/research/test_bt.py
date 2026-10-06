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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
