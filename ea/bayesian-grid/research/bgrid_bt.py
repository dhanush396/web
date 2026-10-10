"""Event-driven backtester that replicates BayesianGrid EA v6.12/v6.13 on M1 OHLC.

Fidelity notes (what is modelled exactly vs approximated):
  * New grids open ONLY on the first tick of a new M15 bar, both sides
    independently, behind the same gates as the EA (time filter, margin
    buffer, DD halt, latch, spread, + news filter in v6.13).
  * Layer adds are evaluated "every tick": inside each M1 bar the path
    O->L->H->C (bull bar) or O->H->L->C (bear bar) is walked continuously, so
    every grid level crossed is filled at the level (+ slippage). Real ticks
    can jump levels during spikes; slippage_pts covers part of that.
  * All positions of a side share TP = wavg +/- TP (exactly what SetTP does),
    so a side always closes as one basket at its TP.
  * Weekend / data gaps are treated as jumps: at most one layer per side is
    added at the gap price and TP / stops fill at the gap price.
  * Basket stop (EmergencyCloseDD_Pct) and broker stop-out are solved
    exactly on the linear float-P/L path between events.
  * Costs: bid/ask = mid -/+ spread/2 (spread widened around 00:00 server
    rollover), commission per lot round-turn, swap per lot per night with
    triple swap on Wednesday night.
  * Margin: hedged accounts, margin = larger leg (MT5 default) or sum.
"""
import numpy as np
from numba import njit

# ---------------------------------------------------------------- params
PARAM_NAMES = [
    "BaseLot", "FlatLayers", "LotIncrement", "LotIncEvery", "GridSpacingPts", "TP_Pips",
    "MaxLayers", "MaxTotalLots", "MarginBufferMult", "MaxSpreadPts", "MaxEquityDD_Pct",
    "EmergencyCloseDD_Pct", "RefCapital", "HaltCooldownMin", "CloseOnFriday", "FridayCloseHour",
    "UseTimeFilter", "BlockFriday", "NewsFilter", "NewsBeforeMin", "NewsAfterMin",
    "NewsPauseLayers", "NewsCloseMode", "NewsCloseMin",
    # broker / account model
    "Balance", "Leverage", "StopOutPct", "ContractSize", "CommPerLotRT", "SwapLong", "SwapShort",
    "SlippagePts", "SpreadPts", "RolloverSpreadMult", "VolMin", "VolStep", "HedgeMarginSum", "Point",
    # v6.14 high-frequency scalper inputs (indices 38..) + simulation controls
    "MaxHoldMin", "EntryTFMin", "LotMultiplier", "SessStartMin", "SessEndMin", "SessCloseAtEnd",
    "DailyLossPct", "PathPoints", "PathSeed", "UseSpreadProfile",
    # v7 HF-grid modules (indices 48..)
    "MaxHoldSec", "NoAddAfterSec", "StopPips", "MaxBasketRiskPct", "AddMinSec", "PeakKillPct",
    "EntryMode", "ZEntry", "ZEmaBars", "TrendTMax",
    "SpreadScale", "MktSlip",
    # v7.02 probability exit (indices 60..): exit a side early only when P(TP before its stop) is down
    "ProbExitMode", "ProbMin", "ProbEdge", "ProbB0", "ProbBZ", "ProbBTrend", "ProbBVol", "ProbBAge",
    "ProbBLayers", "ProbVolLongBars", "ProbRecord",
]
PI = {n: i for i, n in enumerate(PARAM_NAMES)}

# EA v6.12 defaults as shipped (live account) + a typical broker model
EA_DEFAULTS = dict(
    BaseLot=0.08, FlatLayers=5, LotIncrement=0.07, LotIncEvery=1, GridSpacingPts=75, TP_Pips=5.3,
    MaxLayers=18, MaxTotalLots=8.0, MarginBufferMult=5.0, MaxSpreadPts=0, MaxEquityDD_Pct=0.0,
    EmergencyCloseDD_Pct=0.0, RefCapital=0.0, HaltCooldownMin=0, CloseOnFriday=0, FridayCloseHour=20,
    UseTimeFilter=0, BlockFriday=0, NewsFilter=0, NewsBeforeMin=30, NewsAfterMin=30,
    NewsPauseLayers=0, NewsCloseMode=0, NewsCloseMin=15,
    Balance=100.0, Leverage=500.0, StopOutPct=50.0, ContractSize=100000.0, CommPerLotRT=0.0,
    SwapLong=-7.0, SwapShort=-1.0, SlippagePts=2.0, SpreadPts=12.0, RolloverSpreadMult=4.0,
    VolMin=0.01, VolStep=0.01, HedgeMarginSum=0, Point=0.00001,
    MaxHoldMin=0, EntryTFMin=15, LotMultiplier=0.0, SessStartMin=-1, SessEndMin=-1, SessCloseAtEnd=0,
    DailyLossPct=0.0, PathPoints=0, PathSeed=1, UseSpreadProfile=0,
    MaxHoldSec=0, NoAddAfterSec=0, StopPips=0.0, MaxBasketRiskPct=0.0, AddMinSec=0, PeakKillPct=0.0,
    EntryMode=0, ZEntry=1.5, ZEmaBars=90, TrendTMax=2.0,
    SpreadScale=1.0, MktSlip=0,
    ProbExitMode=0, ProbMin=0.30, ProbEdge=0.0, ProbB0=0.0, ProbBZ=0.0, ProbBTrend=0.0, ProbBVol=0.0,
    ProbBAge=0.0, ProbBLayers=0.0, ProbVolLongBars=1440, ProbRecord=0,
)

# EURUSD spread multiplier by SERVER hour (NY-close time): thin after rollover and late
# Asia, tightest London/NY. Applied on top of SpreadPts when UseSpreadProfile=1.
SPREAD_PROFILE = np.array([2.0, 1.5, 1.5, 1.3, 1.3, 1.3, 1.3, 1.2, 1.1, 1.0, 1.0, 1.0,
                           1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0])

ORIGINAL_HOURS = "2,4,11,13,14,15,16,17,18,21,22"


def make_params(**kw):
    d = dict(EA_DEFAULTS)
    for k, v in kw.items():
        if k not in PI:
            raise KeyError(k)
        d[k] = v
    return np.array([float(d[n]) for n in PARAM_NAMES])


def hour_map(hours: str):
    m = np.zeros(24, dtype=np.bool_)
    for s in hours.split(","):
        s = s.strip()
        if s:
            h = int(s)
            if 0 <= h < 24:
                m[h] = True
    return m


# ---------------------------------------------------------------- state layout
BAL, N0, N1, PV0, PV1, L0, L1, LAST0, LAST1, TP0, TP1, SW0, SW1 = range(13)
HALT, HALT_UNTIL, PEAK, MAXDD_PCT, MAXDD_ABS, MIN_EQ = range(13, 19)
WINS, LOSSES, BASKETS, STOPOUTS, FRICLOSE, NEWSCLOSE = range(19, 25)
COMM, SWAP, LAYERS, MAXN, HITMAX, WORSTFLOAT, ADDBLOCK, TPPROFIT, STOPLOSS, GRIDS = range(25, 35)
MINFREE_RATIO, SIDEWORST0, SIDEWORST1, WORSTSIDE = range(35, 39)
START0, START1, DAYBAL, DAYHALT, TIMESTOPS, SESSCLOSE, DAYSTOPS, HOLDSUM, HOLDN, MAXHOLD = range(39, 49)
FIRST0, FIRST1, EFFML0, EFFML1, LASTADD0, LASTADD1, SIDESTOPS, KILL, EQPEAK = range(49, 58)
BID0, BID1, CLSB0, CLSB1, CLST0, CLST1, PROBEXITS, RECN = range(58, 66)
NSTATE = 66

STAT_NAMES = [
    "final_balance", "final_equity", "max_dd_pct", "max_dd_abs", "min_equity", "wins", "losses",
    "basket_stops", "stopouts", "friday_closes", "news_closes", "commission", "swap",
    "layers_added", "max_layers_used", "grids_hit_max", "worst_float", "adds_blocked",
    "tp_profit", "stop_losses", "grids_opened", "worst_side_float",
    "time_stops", "session_closes", "daily_stops", "avg_hold_min", "max_hold_min",
    "side_stops", "killed", "avg_hold_sec", "max_hold_sec", "prob_exits",
]


@njit(cache=True)
def _lot_size(layer, p):
    base = p[0]
    flat = p[1]
    inc = p[2]
    every = p[3] if p[3] >= 1 else 1.0
    if layer <= flat:
        lot = base
    elif p[40] > 0:
        lot = base * p[40] ** (layer - flat)  # geometric: 0.01 x mult^k
    else:
        k = np.ceil((layer - flat) / every)
        lot = base + k * inc
    step = p[35]
    if step > 0:
        lot = np.floor(lot / step + 0.5) * step  # MathRound (half away from zero), as in the EA
    if lot < p[34]:
        lot = p[34]
    return np.round(lot, 2)


@njit(cache=True)
def _lin(S, hs, cs):
    """float(m) = A*m + B for the combined EA basket (mid price m)."""
    a = cs * (S[L0] - S[L1])
    b = cs * (-S[L0] * hs - S[PV0] + S[PV1] - S[L1] * hs) + S[SW0] + S[SW1]
    return a, b


@njit(cache=True)
def _side_float(S, side, m, hs, cs):
    if side == 0:
        if S[N0] == 0:
            return 0.0
        return cs * (S[L0] * (m - hs) - S[PV0]) + S[SW0]
    if S[N1] == 0:
        return 0.0
    return cs * (S[PV1] - S[L1] * (m + hs)) + S[SW1]


@njit(cache=True)
def _margin(S, m, p):
    lots = (S[L0] + S[L1]) if p[36] > 0 else max(S[L0], S[L1])
    return lots * p[27] * m / p[25]


@njit(cache=True)
def _track(S, m, hs, cs):
    a, b = _lin(S, hs, cs)
    fl = a * m + b
    eq = S[BAL] + fl
    if eq > S[PEAK]:
        S[PEAK] = eq
    dd = S[PEAK] - eq
    if dd > S[MAXDD_ABS]:
        S[MAXDD_ABS] = dd
    if S[PEAK] > 0 and dd / S[PEAK] * 100.0 > S[MAXDD_PCT]:
        S[MAXDD_PCT] = dd / S[PEAK] * 100.0
    if eq < S[MIN_EQ]:
        S[MIN_EQ] = eq
    if fl < S[WORSTFLOAT]:
        S[WORSTFLOAT] = fl
    for sd in range(2):
        sf = _side_float(S, sd, m, hs, cs)
        if sf < S[WORSTSIDE]:
            S[WORSTSIDE] = sf


@njit(cache=True)
def _close_side(S, side, m, hs, p, t):
    cs = p[27]
    half = p[28] * 0.5
    n = S[N0] if side == 0 else S[N1]
    if n > 0:
        hold = (t - (S[START0] if side == 0 else S[START1])) / 60.0
        S[HOLDSUM] += hold
        S[HOLDN] += 1
        if hold > S[MAXHOLD]:
            S[MAXHOLD] = hold
    if n > 0:  # training label: did this basket close at (or through) its TP?
        S[CLSB0 + side] = S[BID0 + side]
        if side == 0:
            S[CLST0] = 1.0 if m - hs >= S[TP0] - 1e-9 else 0.0
        else:
            S[CLST1] = 1.0 if m + hs <= S[TP1] + 1e-9 else 0.0
    if side == 0:
        if S[N0] == 0:
            return 0.0
        pnl = cs * (S[L0] * (m - hs) - S[PV0]) + S[SW0] - half * S[L0]
        S[COMM] += half * S[L0]
        S[N0] = 0; S[PV0] = 0; S[L0] = 0; S[LAST0] = 0; S[TP0] = 0; S[SW0] = 0
    else:
        if S[N1] == 0:
            return 0.0
        pnl = cs * (S[PV1] - S[L1] * (m + hs)) + S[SW1] - half * S[L1]
        S[COMM] += half * S[L1]
        S[N1] = 0; S[PV1] = 0; S[L1] = 0; S[LAST1] = 0; S[TP1] = 0; S[SW1] = 0
    S[BAL] += pnl
    return pnl


@njit(cache=True)
def _set_tp(S, side, p):
    point = p[37]
    tpd = p[5] * 10.0 * point
    if side == 0:
        w = S[PV0] / S[L0]
        S[TP0] = np.round((w + tpd) / point) * point
    else:
        w = S[PV1] / S[L1]
        S[TP1] = np.round((w - tpd) / point) * point


@njit(cache=True)
def _open(S, side, fill, lot, p, t):
    half = p[28] * 0.5
    if (S[N0] if side == 0 else S[N1]) == 0:
        if side == 0:
            S[START0] = t
            S[FIRST0] = fill
        else:
            S[START1] = t
            S[FIRST1] = fill
    if side == 0:
        S[LASTADD0] = t
    else:
        S[LASTADD1] = t
    S[BAL] -= half * lot
    S[COMM] += half * lot
    if side == 0:
        S[N0] += 1; S[PV0] += fill * lot; S[L0] += lot; S[LAST0] = fill
        n = S[N0]
    else:
        S[N1] += 1; S[PV1] += fill * lot; S[L1] += lot; S[LAST1] = fill
        n = S[N1]
    if n > S[MAXN]:
        S[MAXN] = n


@njit(cache=True)
def _mkt(p):
    """Adverse slippage for market orders (L1 opens and market exits) when MktSlip=1."""
    return p[31] * p[37] if p[59] > 0 else 0.0


@njit(cache=True)
def _close_mkt(S, side, m, hs, p, t):
    """Market exit of one side (TP exits never slip)."""
    sl = _mkt(p)
    return _close_side(S, side, (m - sl) if side == 0 else (m + sl), hs, p, t)


@njit(cache=True)
def _try_add(S, side, m, hs, p, t):
    """Add next layer at mid m (fill at ask/bid + slippage). Returns False if blocked."""
    n = S[N0] if side == 0 else S[N1]
    lot = _lot_size(int(n) + 1, p)
    if p[9] > 0 and 2.0 * hs / p[37] > p[9] + 1e-9:  # v7: spread guard applies to adds too
        S[ADDBLOCK] += 1
        return False
    if S[L0] + S[L1] + lot > p[7] + 1e-9:
        S[ADDBLOCK] += 1
        return False
    cs = p[27]
    a, b = _lin(S, hs, cs)
    eq = S[BAL] + a * m + b
    free = eq - _margin(S, m, p)
    if free < lot * cs * m / p[25]:
        S[ADDBLOCK] += 1
        return False
    slip = p[31] * p[37]
    fill = (m + hs + slip) if side == 0 else (m - hs - slip)
    _open(S, side, fill, lot, p, t)
    S[LAYERS] += 1
    if (S[N0] if side == 0 else S[N1]) >= (S[EFFML0] if side == 0 else S[EFFML1]):
        S[HITMAX] += 1
    _set_tp(S, side, p)
    return True


@njit(cache=True)
def _loss_close(S, m, hs, p, t, kind):
    """Account-level flatten. kind 0 = broker stop-out, 1 = basket (EmergencyCloseDD),
    2 = peak-equity kill latch, 3 = daily loss limit."""
    for sd in range(2):
        S[STOPLOSS] += _close_mkt(S, sd, m, hs, p, t)
    if kind == 0:
        S[STOPOUTS] += 1
    elif kind == 1:
        S[BASKETS] += 1
        S[HALT] = 1
        cd = p[13]
        S[HALT_UNTIL] = t + cd * 60.0 if cd > 0 else 1e18
    elif kind == 2:
        S[KILL] = 1
    else:
        S[DAYHALT] = 1
        S[DAYSTOPS] += 1


@njit(cache=True)
def _thresholds(S, m, p, refcap):
    """Float-loss thresholds in $ (positive): stop-out, basket, kill latch, daily loss.
    The combined float is linear in price between events, so each crossing is exact."""
    x0 = 1e18
    mg = _margin(S, m, p)
    if mg > 0:
        x0 = S[BAL] - p[26] / 100.0 * mg
    x1 = 1e18
    if p[11] > 0 and S[HALT] == 0:
        x1 = p[11] / 100.0 * refcap
    x2 = 1e18
    if p[53] > 0 and S[KILL] == 0:
        x2 = S[BAL] - S[PEAK] * (1.0 - p[53] / 100.0)
    x3 = 1e18
    if p[44] > 0 and S[DAYHALT] == 0 and S[DAYBAL] > 0:
        x3 = S[BAL] - S[DAYBAL] * (1.0 - p[44] / 100.0)
    return x0, x1, x2, x3


@njit(cache=True)
def _add_level(S, side, lv, a, span, t0, t1, p):
    """Apply the time gates (AddMinSec pacing, NoAddAfterSec cutoff) to an add trigger
    at price lv inside a segment: returns the price where the add can happen, or NaN."""
    tl = t0 if span == 0 else t0 + (t1 - t0) * (lv - a) / span
    start = S[START0] if side == 0 else S[START1]
    last = S[LASTADD0] if side == 0 else S[LASTADD1]
    ta = tl
    if p[52] > 0 and last + p[52] > ta:
        ta = last + p[52]
    if p[49] > 0 and ta >= start + p[49]:
        return np.nan
    if ta > t1 + 1e-9:
        return np.nan
    if ta > tl and t1 > t0:
        return a + span * (ta - t0) / (t1 - t0)
    return lv


@njit(cache=True)
def _segment(S, a, bnd, hs, p, t0, t1, allow0, allow1, refcap, hold_sec):
    """Walk mid price continuously from a (time t0) to bnd (time t1), processing every
    event in path order. Event time is interpolated linearly along the leg."""
    cs = p[27]
    step = p[4] * p[37]
    stop_d = p[50] * 10.0 * p[37]
    slip = p[31] * p[37]
    cur = a
    down = bnd < a
    span = bnd - a
    ok0 = allow0
    ok1 = allow1
    for _ in range(10000):
        te = t0 if span == 0 else t0 + (t1 - t0) * (cur - a) / span
        A, B = _lin(S, hs, cs)
        x0, x1, x2, x3 = _thresholds(S, cur, p, refcap)
        fl = A * cur + B
        if S[N0] + S[N1] > 0:
            if fl <= -x0:
                _loss_close(S, cur, hs, p, te, 0)
                continue
            if fl <= -x1:
                _loss_close(S, cur, hs, p, te, 1)
                continue
            if fl <= -x2:
                _loss_close(S, cur, hs, p, te, 2)
                continue
            if fl <= -x3:
                _loss_close(S, cur, hs, p, te, 3)
                continue
        # levels already crossed at cur (a spread change, or the start of a leg)
        if stop_d > 0 and S[N0] > 0 and cur <= S[FIRST0] - stop_d + hs + 1e-12:
            _track(S, cur, hs, cs)
            S[STOPLOSS] += _close_side(S, 0, cur - slip, hs, p, te)
            S[SIDESTOPS] += 1
            continue
        if stop_d > 0 and S[N1] > 0 and cur >= S[FIRST1] + stop_d - hs - 1e-12:
            _track(S, cur, hs, cs)
            S[STOPLOSS] += _close_side(S, 1, cur + slip, hs, p, te)
            S[SIDESTOPS] += 1
            continue
        if S[N0] > 0 and cur - hs >= S[TP0] - 1e-9:
            pnl = _close_side(S, 0, cur, hs, p, te)
            S[TPPROFIT] += pnl
            if pnl > 0:
                S[WINS] += 1
            else:
                S[LOSSES] += 1
            _track(S, cur, hs, cs)
            continue
        if S[N1] > 0 and cur + hs <= S[TP1] + 1e-9:
            pnl = _close_side(S, 1, cur, hs, p, te)
            S[TPPROFIT] += pnl
            if pnl > 0:
                S[WINS] += 1
            else:
                S[LOSSES] += 1
            _track(S, cur, hs, cs)
            continue
        if hold_sec > 0:
            if S[N0] > 0 and te - S[START0] >= hold_sec - 1e-6:
                S[STOPLOSS] += _close_mkt(S, 0, cur, hs, p, te)
                S[TIMESTOPS] += 1
                continue
            if S[N1] > 0 and te - S[START1] >= hold_sec - 1e-6:
                S[STOPLOSS] += _close_mkt(S, 1, cur, hs, p, te)
                S[TIMESTOPS] += 1
                continue
        if span == 0:
            _track(S, bnd, hs, cs)
            return
        sgn = -1.0 if down else 1.0
        best = 1e18          # distance travelled from cur to the event (smaller = earlier)
        ev = 0
        lvb = cur
        # --- add trigger of the side being moved against
        sd = 0 if down else 1
        okk = ok0 if down else ok1
        nn = S[N0] if down else S[N1]
        if okk and nn > 0 and nn < (S[EFFML0] if down else S[EFFML1]):
            lv = (S[LAST0] - step - hs) if down else (S[LAST1] + step + hs)
            if sgn * (lv - cur) < 0:
                lv = cur
            lv = _add_level(S, sd, lv, a, span, t0, t1, p)
            if not np.isnan(lv):
                d = sgn * (lv - cur)
                if d <= sgn * (bnd - cur) + 1e-9 and d < best:
                    best = d; ev = 1; lvb = lv
        # --- TP of the side moving in its favour
        if down and S[N1] > 0:
            lv = S[TP1] - hs
            d = cur - lv
            if d >= 0 and d <= cur - bnd + 1e-9 and d < best:
                best = d; ev = 2; lvb = lv
        if (not down) and S[N0] > 0:
            lv = S[TP0] + hs
            d = lv - cur
            if d >= 0 and d <= bnd - cur + 1e-9 and d < best:
                best = d; ev = 2; lvb = lv
        # --- L1-anchored stop of the side moved against
        if stop_d > 0:
            if down and S[N0] > 0:
                lv = S[FIRST0] - stop_d + hs
                d = cur - lv
                if d >= 0 and d <= cur - bnd + 1e-9 and d < best:
                    best = d; ev = 5; lvb = lv
            if (not down) and S[N1] > 0:
                lv = S[FIRST1] + stop_d - hs
                d = lv - cur
                if d >= 0 and d <= bnd - cur + 1e-9 and d < best:
                    best = d; ev = 5; lvb = lv
        # --- account thresholds (exact crossing of a linear float)
        if S[N0] + S[N1] > 0 and ((down and A > 0) or ((not down) and A < 0)):
            for k in range(4):
                x = x0 if k == 0 else (x1 if k == 1 else (x2 if k == 2 else x3))
                if x < 1e17:
                    ms = (-x - B) / A
                    d = sgn * (ms - cur)
                    if d >= 0 and d <= sgn * (bnd - cur) + 1e-9 and d < best:
                        best = d; lvb = ms
                        ev = 3 if k == 0 else (4 if k == 1 else (6 if k == 2 else 7))
        # --- holding-time deadlines inside the leg
        if hold_sec > 0 and t1 > t0:
            for k in range(2):
                if (S[N0] if k == 0 else S[N1]) > 0:
                    tl = (S[START0] if k == 0 else S[START1]) + hold_sec
                    if tl > te and tl <= t1:
                        ml = a + span * (tl - t0) / (t1 - t0)
                        d = sgn * (ml - cur)
                        if d >= 0 and d < best:
                            best = d; lvb = ml; ev = 8 + k
        if ev == 0:
            _track(S, bnd, hs, cs)
            return
        cur = lvb
        te = t0 + (t1 - t0) * (cur - a) / span
        _track(S, cur, hs, cs)
        if ev == 1:
            if not _try_add(S, sd, cur, hs, p, te):
                if sd == 0:
                    ok0 = False
                else:
                    ok1 = False
        elif ev == 2:
            side = 1 if down else 0
            pnl = _close_side(S, side, cur, hs, p, te)
            S[TPPROFIT] += pnl
            if pnl > 0:
                S[WINS] += 1
            else:
                S[LOSSES] += 1
        elif ev == 5:
            side = 0 if down else 1
            S[STOPLOSS] += _close_side(S, side, (cur - slip) if side == 0 else (cur + slip), hs, p, te)
            S[SIDESTOPS] += 1
        elif ev == 8 or ev == 9:
            S[STOPLOSS] += _close_mkt(S, ev - 8, cur, hs, p, te)
            S[TIMESTOPS] += 1
        else:
            kind = 0 if ev == 3 else (1 if ev == 4 else (2 if ev == 6 else 3))
            _loss_close(S, cur, hs, p, te, kind)
        _track(S, cur, hs, cs)


@njit(cache=True)
def _jump(S, m, hs, p, t, allow0, allow1, refcap, hold_sec):
    """Price gaps to m (weekend / data hole): fills happen AT the gap price."""
    cs = p[27]
    step = p[4] * p[37]
    if S[N0] > 0 and m - hs >= S[TP0] - 1e-9:
        pnl = _close_side(S, 0, m, hs, p, t)
        S[TPPROFIT] += pnl
        if pnl > 0:
            S[WINS] += 1
        else:
            S[LOSSES] += 1
    if S[N1] > 0 and m + hs <= S[TP1] + 1e-9:
        pnl = _close_side(S, 1, m, hs, p, t)
        S[TPPROFIT] += pnl
        if pnl > 0:
            S[WINS] += 1
        else:
            S[LOSSES] += 1
    stop_d = p[50] * 10.0 * p[37]
    if stop_d > 0:
        if S[N0] > 0 and m - hs <= S[FIRST0] - stop_d:
            S[STOPLOSS] += _close_side(S, 0, m - p[31] * p[37], hs, p, t)
            S[SIDESTOPS] += 1
        if S[N1] > 0 and m + hs >= S[FIRST1] + stop_d:
            S[STOPLOSS] += _close_side(S, 1, m + p[31] * p[37], hs, p, t)
            S[SIDESTOPS] += 1
    if hold_sec > 0:
        _time_stops(S, m, hs, p, t, hold_sec)
    _track(S, m, hs, cs)
    if S[N0] + S[N1] > 0:
        A, B = _lin(S, hs, cs)
        x0, x1, x2, x3 = _thresholds(S, m, p, refcap)
        fl = A * m + B
        if fl <= -x0:
            _loss_close(S, m, hs, p, t, 0)
        elif fl <= -x1:
            _loss_close(S, m, hs, p, t, 1)
        elif fl <= -x2:
            _loss_close(S, m, hs, p, t, 2)
        elif fl <= -x3:
            _loss_close(S, m, hs, p, t, 3)
    if allow0 and _adds_ok(S, 0, p, t) and S[N0] > 0 and S[N0] < S[EFFML0] and m + hs <= S[LAST0] - step:
        _try_add(S, 0, m, hs, p, t)
    if allow1 and _adds_ok(S, 1, p, t) and S[N1] > 0 and S[N1] < S[EFFML1] and m - hs >= S[LAST1] + step:
        _try_add(S, 1, m, hs, p, t)


@njit(cache=True)
def _adds_ok(S, side, p, t):
    """v7 add gating: no adds after NoAddAfterSec of basket age, at most one add per AddMinSec."""
    start = S[START0] if side == 0 else S[START1]
    last = S[LASTADD0] if side == 0 else S[LASTADD1]
    if p[49] > 0 and t - start >= p[49]:
        return False
    if p[52] > 0 and t - last < p[52]:
        return False
    return True


@njit(cache=True)
def _fit_layers(p, hs, eq):
    """v7 pre-trade ladder fit: the deepest ladder whose loss at the L1-anchored stop
    (incl. stop slippage, add-fill slippage drift and commission) fits MaxBasketRiskPct
    of equity. A layer counts only if its trigger is reachable before the stop."""
    maxl = int(p[6])
    stop_d = p[50] * 10.0 * p[37]
    if stop_d <= 0:
        return maxl
    step = p[4] * p[37]
    slip = p[31] * p[37]
    budget = p[51] / 100.0 * eq if p[51] > 0 else 1e18
    worst = 0.0
    n = 0
    for k in range(1, maxl + 1):
        dist = stop_d - (k - 1) * (step - slip)  # layer k fill (ask) to the stop (bid)
        if k > 1 and dist <= 2.0 * hs + slip:
            break
        if dist <= 0:
            break
        lot = _lot_size(k, p)
        add = lot * p[27] * (dist + slip) + lot * p[28]
        if worst + add > budget:
            break
        worst += add
        n = k
    return n


@njit(cache=True)
def _flush_labels(S, outc):
    """Store pending basket labels; returns a bit mask of the sides whose basket ended by a non-TP
    exit since the last flush. Called just before the entry block, that is exactly the EA's latch:
    a side closed by a stop / time / probability exit on this tick (g_sideFlat) cannot re-open on it."""
    mask = 0
    for sd in range(2):
        if S[CLSB0 + sd] >= 0:
            if outc.shape[0] > 0:
                outc[int(S[CLSB0 + sd])] = S[CLST0 + sd]
            if S[CLST0 + sd] < 0.5:
                mask |= 1 << sd
            S[CLSB0 + sd] = -1.0
    return mask


@njit(cache=True)
def _p_rw(S, sd, m, hs, p):
    """Random-walk probability that side sd reaches its TP before its L1-anchored stop, INCLUDING
    the grid's own future adds: between the current TP (above, for a BUY grid) and the next add
    trigger (below) a driftless price hits the TP first with probability (x-A)/(TP-A); reaching A adds
    the next layer (fill at the trigger + slippage), which re-sets the TP from the new average, and
    so on until the ladder (EFFML, MaxTotalLots) is full, after which the stop is the lower barrier.
    Spread is held at its current value."""
    point = p[37]
    step = p[4] * point
    stop_d = p[50] * 10.0 * point
    slip = p[31] * point
    tpd = p[5] * 10.0 * point
    if sd == 0:
        n = S[N0]; lots = S[L0]; pv = S[PV0]; last = S[LAST0]; tp = S[TP0]; other = S[L1]
        x = m - hs
        bar = S[FIRST0] - stop_d
    else:
        n = S[N1]; lots = S[L1]; pv = S[PV1]; last = S[LAST1]; tp = S[TP1]; other = S[L0]
        x = m + hs
        bar = S[FIRST1] + stop_d
    effml = S[EFFML0 + sd]
    res = 0.0
    reach = 1.0
    for _ in range(64):
        if (sd == 0 and x >= tp) or (sd == 1 and x <= tp):
            res += reach
            break
        lot = _lot_size(int(n) + 1, p)
        can_add = n < effml and other + lots + lot <= p[7] + 1e-9
        if sd == 0:
            a = last - step - 2.0 * hs  # bid when the ask reaches the next add level
            if not (can_add and a > bar):
                a = bar
                can_add = False
            f = 0.0 if x <= a else (x - a) / (tp - a)
        else:
            a = last + step + 2.0 * hs  # ask when the bid reaches the next add level
            if not (can_add and a < bar):
                a = bar
                can_add = False
            f = 0.0 if x >= a else (a - x) / (a - tp)
        res += reach * f
        reach *= 1.0 - f
        if not can_add or reach <= 1e-12:
            break
        fill = (last - step + slip) if sd == 0 else (last + step - slip)
        pv += fill * lot
        lots += lot
        last = fill
        n += 1
        w = pv / lots
        tp = np.round(((w + tpd) if sd == 0 else (w - tpd)) / point) * point
        x = a
    return res


@njit(cache=True)
def _prob_exits(S, i, m, hs, p, ti, pf, rec):
    """v7.02 probability exit, evaluated at the open of each M1 bar (the EA: first tick of the bar).
    P_fair = _p_rw: the random-walk probability of reaching the TP before the basket stop, with the
    grid's own future adds; the model adds conditional terms in logit space from CLOSED-bar features.
    FLOOR exits when P < ProbMin, EDGE when logit P - logit P_fair < -ProbEdge."""
    stop_d = p[50] * 10.0 * p[37]
    pip = 10.0 * p[37]
    mode = int(p[60])
    for sd in range(2):
        nn = S[N0] if sd == 0 else S[N1]
        if nn <= 0 or stop_d <= 0:
            continue
        if sd == 0:
            x = m - hs
            u = S[TP0] - x
            v = x - (S[FIRST0] - stop_d)
            sgn = 1.0
            start = S[START0]
        else:
            x = m + hs
            u = x - S[TP1]
            v = (S[FIRST1] + stop_d) - x
            sgn = -1.0
            start = S[START1]
        if u <= 0 or v <= 0:
            continue
        pfair = _p_rw(S, sd, m, hs, p)
        if pfair < 1e-6:
            pfair = 1e-6
        if pfair > 1 - 1e-6:
            pfair = 1 - 1e-6
        age = (ti - start) / 60.0
        if rec.shape[0] > 0 and S[RECN] < rec.shape[0]:
            r = int(S[RECN])
            rec[r, 0] = S[BID0 + sd]; rec[r, 1] = sgn; rec[r, 2] = i; rec[r, 3] = u / pip
            rec[r, 4] = v / pip; rec[r, 5] = nn; rec[r, 6] = age; rec[r, 7] = pfair
            rec[r, 8] = S[EFFML0 + sd]
            S[RECN] += 1
        if mode <= 0 or pf.shape[0] == 0:
            continue
        z = pf[i, 0]; tr = pf[i, 1]; lv = pf[i, 2]
        if np.isnan(z) or np.isnan(tr) or np.isnan(lv):
            continue
        lf = np.log(pfair / (1.0 - pfair))
        lg = (lf + p[63] + p[64] * (-sgn * z) + p[65] * (sgn * tr) + p[66] * lv
              + p[67] * np.log(1.0 + age) + p[68] * (nn - 1.0))
        pr = 1.0 / (1.0 + np.exp(-lg))
        ex = False
        if (mode == 1 or mode == 3) and pr < p[61]:
            ex = True
        if (mode == 2 or mode == 3) and lg - lf < -p[62]:
            ex = True
        if ex:
            S[STOPLOSS] += _close_mkt(S, sd, m, hs, p, ti)
            S[PROBEXITS] += 1


@njit(cache=True)
def run_core(t, o, h, l, c, p, hmap, news, rec_daily, sig, sprd, pf, rec, outc):
    n = t.shape[0]
    S = np.zeros(NSTATE)
    S[CLSB0] = -1.0
    S[CLSB1] = -1.0
    S[BAL] = p[24]
    S[PEAK] = p[24]
    S[MIN_EQ] = p[24]
    point = p[37]
    cs = p[27]
    ndays = int((t[n - 1] - t[0]) // 86400) + 2
    d_eq = np.full(ndays if rec_daily else 1, np.nan)
    d_bal = np.full(ndays if rec_daily else 1, np.nan)
    day0 = t[0] // 86400
    last_day = day0
    last_m15 = -1
    prev_c = o[0]
    prev_t = t[0] - 60
    nidx = 0
    nnews = news.shape[0]
    before = p[19] * 60.0
    if p[22] > 0 and p[23] > p[19]:
        before = p[23] * 60.0  # same as EA: never re-open between pre-close and the event
    after = p[20] * 60.0
    close_before = p[23] * 60.0
    npts = int(min(max(p[45], 0), 10))
    wp = np.zeros(4 + 3 * npts)
    wt = np.zeros(4 + 3 * npts)  # time of each waypoint as a fraction of the minute
    if npts > 0:
        np.random.seed(int(p[46]))
    tf_sec = max(p[39], 1.0) * 60.0
    hold_sec = p[48] if p[48] > 0 else p[38] * 60.0  # v7 seconds clock, else v6.14 minutes
    use_sig = p[54] > 0 and sig.shape[0] == n
    use_sprd = sprd.shape[0] == n
    S[EQPEAK] = p[24]
    sess_on = p[41] >= 0 and p[42] >= 0 and p[41] != p[42]
    S[DAYBAL] = p[24]
    hs = p[32] * point * 0.5
    mslip = _mkt(p)
    for i in range(n):
        ti = t[i]
        mod = (ti % 86400) // 60
        if use_sprd:
            spr_pts = sprd[i] * p[58]  # broker bar spread from an MT5 export (x SpreadScale)
        else:
            spr_pts = p[32]
            if p[47] > 0:
                spr_pts = spr_pts * SPREAD_PROFILE[int(mod // 60)]
            if mod >= 1435 or mod < 15:
                spr_pts = spr_pts * p[33]
        hs = spr_pts * point * 0.5
        day = ti // 86400
        refcap = p[12] if p[12] > 0 else (S[BAL] if p[12] < 0 else p[24])
        if refcap <= 0:
            refcap = 1.0
        # ---- rollover swaps + daily record
        if day != last_day:
            if rec_daily:
                a_, b_ = _lin(S, hs, cs)
                k = int(last_day - day0)
                d_eq[k] = S[BAL] + a_ * prev_c + b_
                d_bal[k] = S[BAL]
            for d in range(last_day + 1, day + 1):
                dow = (d + 4) % 7  # 0=Sun
                if dow >= 2:  # Tue..Sat midnights = Mon..Fri nights
                    mult = 3.0 if dow == 4 else 1.0
                    if S[N0] > 0:
                        sw = p[29] * S[L0] * mult
                        S[SW0] += sw; S[SWAP] += sw
                    if S[N1] > 0:
                        sw = p[30] * S[L1] * mult
                        S[SW1] += sw; S[SWAP] += sw
            last_day = day
            S[DAYBAL] = S[BAL]
            S[DAYHALT] = 0
        dow = (day + 4) % 7
        hour = int(mod // 60)
        # ---- halt cooldown
        if S[HALT] > 0 and ti >= S[HALT_UNTIL]:
            S[HALT] = 0
        # ---- news window
        in_news = False
        pre_news = False
        if p[18] > 0 and nnews > 0:
            while nidx < nnews and news[nidx] + after < ti:
                nidx += 1
            if nidx < nnews:
                ev = news[nidx]
                if ti >= ev - before and ti <= ev + after:
                    in_news = True
                if p[22] > 0 and ti >= ev - close_before and ti < ev:
                    pre_news = True
        allow = not (in_news and p[21] > 0)
        # ---- move from previous close to this open
        # (adds are deferred until after the bar-open exit checks, as in the EA's OnTick order:
        #  V7GridExits -> account guards -> TryAddLayer -> entries)
        if ti - prev_t > 180:
            _jump(S, o[i], hs, p, ti, False, False, refcap, hold_sec)
        else:
            _segment(S, prev_c, o[i], hs, p, ti, ti, False, False, refcap, hold_sec)
        # ---- Friday flatten (EA returns early: no adds, no opens)
        if p[14] > 0 and dow == 5 and hour >= p[15]:
            if S[N0] + S[N1] > 0:
                for sd in range(2):
                    S[STOPLOSS] += _close_mkt(S, sd, o[i], hs, p, ti)
                S[FRICLOSE] += 1
            if npts > 0:
                _build_path(wp, wt, o[i], h[i], l[i], c[i], npts)  # keep the RNG stream independent of Friday settings
            _flush_labels(S, outc)  # Friday flatten is not a latch: the next session may re-open
            prev_c = c[i]
            prev_t = ti
            continue
        # ---- max holding time: close a side whose FIRST position is >= the hold limit old
        if hold_sec > 0:
            _time_stops(S, o[i], hs, p, ti, hold_sec)
        # ---- v7.02 probability exit (and the training recorder) at the bar open
        if p[60] > 0 or rec.shape[0] > 0:
            _prob_exits(S, i, o[i], hs, p, ti, pf, rec)
        # ---- v7 peak-equity kill latch + daily loss limit (also re-checked at every waypoint)
        _acct_guards(S, o[i], hs, p, ti)
        if S[KILL] > 0 and S[N0] + S[N1] == 0:
            prev_c = c[i]
            break  # latched and flat: nothing can happen any more, equity is frozen
        # ---- trading session window (server minutes, may wrap midnight)
        in_sess = True
        if sess_on:
            if p[41] < p[42]:
                in_sess = mod >= p[41] and mod < p[42]
            else:
                in_sess = mod >= p[41] or mod < p[42]
            if not in_sess and p[43] > 0 and S[N0] + S[N1] > 0:
                for sd in range(2):
                    S[STOPLOSS] += _close_mkt(S, sd, o[i], hs, p, ti)
                S[SESSCLOSE] += 1
        # ---- news pre-close
        if pre_news and S[N0] + S[N1] > 0:
            closed = False
            for sd in range(2):
                if p[22] >= 2 or _side_float(S, sd, o[i], hs, cs) >= 0:
                    if (S[N0] if sd == 0 else S[N1]) > 0:
                        S[STOPLOSS] += _close_mkt(S, sd, o[i], hs, p, ti)
                        closed = True
            if closed:
                S[NEWSCLOSE] += 1
        # ---- new entry-timeframe bar (M15 in v6.12; M1/M5 for the scalper): open idle grids
        # ---- deferred layer add at the open (at most one per side, at the open price)
        step_px = p[4] * point
        if allow and S[N0] > 0 and S[N0] < S[EFFML0] and _adds_ok(S, 0, p, ti) and o[i] + hs <= S[LAST0] - step_px:
            _try_add(S, 0, o[i], hs, p, ti)
        if allow and S[N1] > 0 and S[N1] < S[EFFML1] and _adds_ok(S, 1, p, ti) and o[i] - hs >= S[LAST1] + step_px:
            _try_add(S, 1, o[i], hs, p, ti)
        latched = _flush_labels(S, outc)
        m15 = ti // tf_sec
        if m15 != last_m15:
            last_m15 = m15
            if S[N0] == 0 or S[N1] == 0:
                time_ok = True
                if p[16] > 0:
                    if p[17] > 0 and dow == 5:
                        time_ok = False
                    elif not hmap[hour]:
                        time_ok = False
                a_, b_ = _lin(S, hs, cs)
                eq = S[BAL] + a_ * o[i] + b_
                lot1 = _lot_size(1, p)
                free = eq - _margin(S, o[i], p)
                margin_ok = free >= lot1 * cs * o[i] / p[25] * p[8]
                gfl = a_ * o[i] + b_
                gdd = (-gfl / refcap * 100.0) if gfl < 0 else 0.0
                dd_ok = p[10] <= 0 or gdd < p[10]
                spread_ok = p[9] <= 0 or spr_pts <= p[9]
                news_ok = not in_news
                if (time_ok and margin_ok and dd_ok and S[HALT] == 0 and spread_ok and news_ok
                        and in_sess and S[DAYHALT] == 0 and S[KILL] == 0):
                    tpd = p[5] * 10.0 * point
                    want0 = True
                    want1 = True
                    if use_sig:  # v7 stretch mode: open only the reversion side
                        want0 = sig[i] > 0
                        want1 = sig[i] < 0
                    effml = _fit_layers(p, hs, eq)  # v7 pre-trade ladder fit to the risk budget
                    if effml >= 1:
                        if want0 and (latched & 1) == 0 and S[N0] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                            px = o[i] + hs  # requested ask; the fill slips by MktSlip
                            _open(S, 0, px + mslip, lot1, p, ti)
                            S[TP0] = np.round((px + tpd) / point) * point  # EA sets TP from the request
                            S[EFFML0] = effml
                            S[BID0] = S[GRIDS]
                            S[GRIDS] += 1
                            if effml <= 1:
                                S[HITMAX] += 1
                        if want1 and (latched & 2) == 0 and S[N1] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                            px = o[i] - hs
                            _open(S, 1, px - mslip, lot1, p, ti)
                            S[TP1] = np.round((px - tpd) / point) * point
                            S[EFFML1] = effml
                            S[BID1] = S[GRIDS]
                            S[GRIDS] += 1
                            if effml <= 1:
                                S[HITMAX] += 1
        # ---- intra-bar path
        nwp = _build_path(wp, wt, o[i], h[i], l[i], c[i], npts)
        for k in range(nwp - 1):
            tk0 = ti + wt[k] * 60.0
            tk1 = ti + wt[k + 1] * 60.0
            if wp[k + 1] != wp[k]:  # the leg solves hold deadlines and account limits inside itself
                _segment(S, wp[k], wp[k + 1], hs, p, tk0, tk1, allow, allow, refcap, hold_sec)
            if hold_sec > 0:  # flat legs: the clock still runs
                _time_stops(S, wp[k + 1], hs, p, tk1, hold_sec)
            if p[53] > 0 or p[44] > 0:  # the EA checks these on every tick, flat or not
                _acct_guards(S, wp[k + 1], hs, p, tk1)
        _flush_labels(S, outc)
        prev_c = c[i]
        prev_t = ti
    _flush_labels(S, outc)
    # final mark uses the last processed bar's spread
    a_, b_ = _lin(S, hs, cs)
    final_eq = S[BAL] + a_ * prev_c + b_
    if rec_daily:
        k = int(last_day - day0)
        d_eq[k] = final_eq
        d_bal[k] = S[BAL]
    out = np.zeros(len_stats())
    out[0] = S[BAL]; out[1] = final_eq; out[2] = S[MAXDD_PCT]; out[3] = S[MAXDD_ABS]
    out[4] = S[MIN_EQ]; out[5] = S[WINS]; out[6] = S[LOSSES]; out[7] = S[BASKETS]
    out[8] = S[STOPOUTS]; out[9] = S[FRICLOSE]; out[10] = S[NEWSCLOSE]; out[11] = S[COMM]
    out[12] = S[SWAP]; out[13] = S[LAYERS]; out[14] = S[MAXN]; out[15] = S[HITMAX]
    out[16] = S[WORSTFLOAT]; out[17] = S[ADDBLOCK]; out[18] = S[TPPROFIT]; out[19] = S[STOPLOSS]
    out[20] = S[GRIDS]; out[21] = S[WORSTSIDE]
    out[22] = S[TIMESTOPS]; out[23] = S[SESSCLOSE]; out[24] = S[DAYSTOPS]
    out[25] = S[HOLDSUM] / S[HOLDN] if S[HOLDN] > 0 else 0.0
    out[26] = S[MAXHOLD]
    out[27] = S[SIDESTOPS]; out[28] = S[KILL]
    out[29] = out[25] * 60.0; out[30] = out[26] * 60.0
    out[31] = S[PROBEXITS]
    return out, d_eq, d_bal


@njit(cache=True)
def len_stats():
    return 32


@njit(cache=True)
def _acct_guards(S, m, hs, p, t):
    """Kill latch at PeakKillPct below the running equity peak (the same peak the DD
    statistics use) and the daily loss limit vs the balance at the server-day start."""
    a_, b_ = _lin(S, hs, p[27])
    eq = S[BAL] + a_ * m + b_  # flat -> balance: a loss realised by a stop counts too
    if p[53] > 0 and S[KILL] == 0 and eq <= S[PEAK] * (1.0 - p[53] / 100.0):
        _loss_close(S, m, hs, p, t, 2)
        return
    if p[44] > 0 and S[DAYHALT] == 0 and S[DAYBAL] > 0 and eq - S[DAYBAL] <= -p[44] / 100.0 * S[DAYBAL]:
        _loss_close(S, m, hs, p, t, 3)


@njit(cache=True)
def _time_stops(S, m, hs, p, t, hold_sec):
    for sd in range(2):
        if (S[N0] if sd == 0 else S[N1]) > 0:
            if t - (S[START0] if sd == 0 else S[START1]) >= hold_sec - 1e-6:
                S[STOPLOSS] += _close_mkt(S, sd, m, hs, p, t)
                S[TIMESTOPS] += 1


@njit(cache=True)
def _build_path(wp, wt, o, h, l, c, npts):
    """Waypoints of the price path inside one M1 bar.

    npts == 0: deterministic O->L->H->C (bull bar) / O->H->L->C (bear bar), the
    MT5 "1 minute OHLC" convention. npts > 0: the high/low order is a coin flip
    and each leg gets npts Brownian-bridge points clipped to [low, high], so a
    1-3 pip bar can cross a 1-pip TP or grid level several times, as ticks do.
    """
    if npts == 0:
        wp[0] = o
        if c >= o:
            wp[1] = l; wp[2] = h
        else:
            wp[1] = h; wp[2] = l
        wp[3] = c
        wt[0] = 0.0; wt[1] = 1.0 / 3.0; wt[2] = 2.0 / 3.0; wt[3] = 59.0 / 60.0
        return 4
    rng = h - l
    tend = 59.0 / 60.0
    hi_first = np.random.random() < 0.5
    t1 = np.random.uniform(0.05, 0.6)
    t2 = np.random.uniform(t1 + 0.05, 0.93)
    av0 = o
    av1 = h if hi_first else l
    av2 = l if hi_first else h
    av3 = c
    k = 0
    for g in range(3):
        if g == 0:
            a, b, ta, tb = av0, av1, 0.0, t1
        elif g == 1:
            a, b, ta, tb = av1, av2, t1, t2
        else:
            a, b, ta, tb = av2, av3, t2, tend
        wp[k] = a
        wt[k] = ta
        k += 1
        sig = 0.5 * rng * np.sqrt(tb - ta)
        for j in range(npts):
            sfrac = (j + 1.0) / (npts + 1.0)
            wt[k] = ta + (tb - ta) * sfrac
            v = a + (b - a) * sfrac + sig * np.sqrt(sfrac * (1.0 - sfrac)) * np.random.normal()
            if v > h:
                v = h
            if v < l:
                v = l
            wp[k] = v
            k += 1
    wp[k] = c
    wt[k] = tend
    return k + 1


# ---------------------------------------------------------------- python API
class Data:
    def __init__(self, npz_path):
        z = np.load(npz_path)
        self.t = z["t_srv"].astype(np.float64)
        self.o = z["o"]; self.h = z["h"]; self.l = z["l"]; self.c = z["c"]
        self.spread = z["spread"].astype(np.float64) if "spread" in z.files else np.zeros(0)
        self.point = float(z["point"]) if "point" in z.files else 0.00001
        self._sig = {}

    def slice(self, start=None, end=None):
        """start/end as 'YYYY-MM-DD' (server time). Returns a view object."""
        import pandas as pd
        lo = 0 if start is None else int(np.searchsorted(self.t, pd.Timestamp(start).value // 10**9))
        hi = len(self.t) if end is None else int(np.searchsorted(self.t, pd.Timestamp(end).value // 10**9))
        d = Data.__new__(Data)
        d.t = self.t[lo:hi]; d.o = self.o[lo:hi]; d.h = self.h[lo:hi]; d.l = self.l[lo:hi]; d.c = self.c[lo:hi]
        d.spread = self.spread[lo:hi] if self.spread.shape[0] else self.spread
        d.point = self.point
        d._sig = {}
        d._root = getattr(self, "_root", None) or self
        d._lo = getattr(self, "_lo", 0) + lo
        return d

    def stretch_signal(self, zentry, ema_bars, trend_tmax, pip):
        """v7 entry mode 1 (stretch-and-reclaim), decided on CLOSED bars only:
        +1 = open the BUY grid at this bar's open, -1 = SELL, 0 = nothing.
        z = (close - EMA(close)) / sigma15 where sigma15 = EWMA(60-min half-life) std of
        M1 changes x sqrt(15); the bar must reclaim (close back through its open and the
        previous extreme) and the 60-bar trend t-stat must be below trend_tmax."""
        key = (round(zentry, 4), int(ema_bars), round(trend_tmax, 4))
        if key in self._sig:
            return self._sig[key]
        import pandas as pd
        c = pd.Series(self.c); o = pd.Series(self.o); h = pd.Series(self.h); lo = pd.Series(self.l)
        r1 = c.diff() / pip
        sig1 = np.sqrt((r1 ** 2).ewm(halflife=60, min_periods=120).mean())
        z = (c - c.ewm(span=int(ema_bars), adjust=False).mean()) / pip / (sig1 * np.sqrt(15))
        trend = r1.rolling(60).sum().abs() / (sig1 * np.sqrt(60))
        ok = trend < trend_tmax
        up = ok & (z <= -zentry) & (c > o) & (c > lo.shift(1))
        dn = ok & (z >= zentry) & (c < o) & (c < h.shift(1))
        s = np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)
        s = np.concatenate([[0], s[:-1]])  # act on the NEXT bar's open: no lookahead
        self._sig[key] = s
        return s


def _prob_features(data, ema_bars, vol_long, pip):
    """v7.02 probability-exit features on CLOSED M1 bars, shifted one bar (row i = known at the
    open of bar i): z (as the stretch entry), signed 60-bar trend t-stat, ln(sigma1/sigmaLong)."""
    key = ("pf", int(ema_bars), int(vol_long))
    if key in data._sig:
        return data._sig[key]
    root = getattr(data, "_root", None)
    if root is not None:  # a slice: compute on the full history, then cut (no warm-up per slice)
        f = _prob_features(root, ema_bars, vol_long, pip)[data._lo:data._lo + len(data.t)]
        data._sig[key] = f
        return f
    import pandas as pd
    c = pd.Series(data.c)
    r1 = c.diff() / pip
    v1 = (r1 ** 2).ewm(halflife=60, min_periods=120).mean()
    vl = (r1 ** 2).ewm(halflife=float(vol_long), min_periods=120).mean()
    sig1 = np.sqrt(v1)
    z = (c - c.ewm(span=int(ema_bars), adjust=False).mean()) / pip / (sig1 * np.sqrt(15))
    tr = r1.rolling(60).sum() / (sig1 * np.sqrt(60))
    lv = 0.5 * np.log(v1 / vl)
    f = np.column_stack([z.to_numpy(), tr.to_numpy(), lv.to_numpy()]).astype(np.float64)
    f = np.vstack([np.full((1, 3), np.nan), f[:-1]])  # act on the NEXT bar's open: no lookahead
    f[~np.isfinite(f)] = np.nan
    data._sig[key] = f
    return f


def run(data, params, hours=ORIGINAL_HOURS, news=None, daily=False):
    if news is None:
        news = np.zeros(0)
    sig = np.zeros(0, dtype=np.int8)
    if params[PI["EntryMode"]] > 0:
        sig = data.stretch_signal(params[PI["ZEntry"]], params[PI["ZEmaBars"]], params[PI["TrendTMax"]],
                                  10.0 * params[PI["Point"]])
    sprd = getattr(data, "spread", np.zeros(0))
    pf = np.zeros((0, 3))
    if params[PI["ProbExitMode"]] > 0:
        pf = _prob_features(data, params[PI["ZEmaBars"]], params[PI["ProbVolLongBars"]], 10.0 * params[PI["Point"]])
    rec = np.zeros((0, 9))
    outc = np.zeros(0)
    if params[PI["ProbRecord"]] > 0:
        rec = np.full((int(params[PI["ProbRecord"]]), 9), -1.0)  # column 0 = basket id, -1 = unused
        outc = np.full(len(data.t) * 2 + 2, -1.0)
    stats, d_eq, d_bal = run_core(data.t, data.o, data.h, data.l, data.c, params,
                                  hour_map(hours), np.asarray(news, dtype=np.float64), daily,
                                  sig, np.asarray(sprd, dtype=np.float64), pf, rec, outc)
    res = dict(zip(STAT_NAMES, stats.tolist()))
    years = (data.t[-1] - data.t[0]) / (365.25 * 86400)
    b0 = params[PI["Balance"]]
    fe = res["final_equity"]
    res["years"] = years
    res["net_profit"] = fe - b0
    res["cagr_pct"] = ((max(fe, 1e-9) / b0) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    res["ruined"] = int(res["stopouts"] > 0 or res["min_equity"] <= 0.2 * b0)
    tot = res["wins"] + res["losses"]
    res["win_rate"] = res["wins"] / tot * 100 if tot else 0.0
    if daily:
        res["daily_equity"] = d_eq
        res["daily_balance"] = d_bal
    if params[PI["ProbRecord"]] > 0:
        nrec = int(min((rec[:, 0] >= 0).sum(), rec.shape[0]))
        res["prob_rec"] = rec[:nrec] if nrec else rec[:0]
        res["prob_outcome"] = outc[:int(res["grids_opened"])]
    return res
