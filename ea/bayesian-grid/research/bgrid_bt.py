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
NSTATE = 58

STAT_NAMES = [
    "final_balance", "final_equity", "max_dd_pct", "max_dd_abs", "min_equity", "wins", "losses",
    "basket_stops", "stopouts", "friday_closes", "news_closes", "commission", "swap",
    "layers_added", "max_layers_used", "grids_hit_max", "worst_float", "adds_blocked",
    "tp_profit", "stop_losses", "grids_opened", "worst_side_float",
    "time_stops", "session_closes", "daily_stops", "avg_hold_min", "max_hold_min",
    "side_stops", "killed", "avg_hold_sec", "max_hold_sec",
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
        lot = np.round(lot / step) * step
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
    if n >= p[6]:
        S[HITMAX] += 1


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
    _set_tp(S, side, p)
    return True


@njit(cache=True)
def _loss_close(S, m, hs, p, t, is_stopout):
    for sd in range(2):
        pnl = _close_side(S, sd, m, hs, p, t)
        S[STOPLOSS] += pnl
    if is_stopout:
        S[STOPOUTS] += 1
    else:
        S[BASKETS] += 1
        S[HALT] = 1
        cd = p[13]
        S[HALT_UNTIL] = t + cd * 60.0 if cd > 0 else 1e18


@njit(cache=True)
def _thresholds(S, m, p, refcap):
    """Return (basket_loss_threshold, stopout_loss_threshold) as positive $ of float loss."""
    x1 = 1e18
    if p[11] > 0 and S[HALT] == 0:
        x1 = p[11] / 100.0 * refcap
    mg = _margin(S, m, p)
    x2 = 1e18
    if mg > 0:
        x2 = S[BAL] - p[26] / 100.0 * mg
    return x1, x2


@njit(cache=True)
def _segment(S, a, bnd, hs, p, t, allow0, allow1, refcap):
    """Walk mid price continuously from a to bnd, processing events in order."""
    cs = p[27]
    step = p[4] * p[37]
    stop_d = p[50] * 10.0 * p[37]
    slip = p[31] * p[37]
    cur = a
    down = bnd < a
    ok0 = allow0 and _adds_ok(S, 0, p, t)
    ok1 = allow1 and _adds_ok(S, 1, p, t)
    for _ in range(10000):
        A, B = _lin(S, hs, cs)
        x1, x2 = _thresholds(S, cur, p, refcap)
        fl_cur = A * cur + B
        # immediate loss triggers
        if S[N0] + S[N1] > 0:
            if fl_cur <= -x2:
                _loss_close(S, cur, hs, p, t, True)
                continue
            if fl_cur <= -x1:
                _loss_close(S, cur, hs, p, t, False)
                continue
        best = 0.0
        ev = 0
        if down:
            best = -1e18
            if ok0 and S[N0] > 0 and S[N0] < S[EFFML0]:
                lv = S[LAST0] - step - hs
                if lv > cur:
                    lv = cur
                if lv >= bnd - 1e-9 and lv > best:
                    best = lv; ev = 1
            if S[N1] > 0:
                lv = S[TP1] - hs
                if lv > cur:
                    lv = cur
                if lv >= bnd - 1e-9 and lv > best:
                    best = lv; ev = 2
            if S[N0] > 0 and stop_d > 0:
                lv = S[FIRST0] - stop_d + hs  # BUY stop: bid <= L1 ask - StopPips
                if lv > cur:
                    lv = cur
                if lv >= bnd - 1e-9 and lv > best:
                    best = lv; ev = 5
            if A > 0 and S[N0] + S[N1] > 0:
                for k in range(2):
                    x = x2 if k == 0 else x1
                    if x < 1e17:
                        ms = (-x - B) / A
                        if ms >= bnd and ms <= cur and ms > best:
                            best = ms; ev = 3 + k
        else:
            best = 1e18
            if ok1 and S[N1] > 0 and S[N1] < S[EFFML1]:
                lv = S[LAST1] + step + hs  # bid >= last + step
                if lv < cur:
                    lv = cur
                if lv <= bnd + 1e-9 and lv < best:
                    best = lv; ev = 1
            if S[N0] > 0:
                lv = S[TP0] + hs
                if lv < cur:
                    lv = cur
                if lv <= bnd + 1e-9 and lv < best:
                    best = lv; ev = 2
            if S[N1] > 0 and stop_d > 0:
                lv = S[FIRST1] + stop_d - hs  # SELL stop: ask >= L1 bid + StopPips
                if lv < cur:
                    lv = cur
                if lv <= bnd + 1e-9 and lv < best:
                    best = lv; ev = 5
            if A < 0 and S[N0] + S[N1] > 0:
                for k in range(2):
                    x = x2 if k == 0 else x1
                    if x < 1e17:
                        ms = (-x - B) / A
                        if ms <= bnd and ms >= cur and ms < best:
                            best = ms; ev = 3 + k
        if ev == 0:
            _track(S, bnd, hs, cs)
            return
        cur = best
        if ev == 1:
            _track(S, cur, hs, cs)
            side = 0 if down else 1
            if not _try_add(S, side, cur, hs, p, t):
                if side == 0:
                    ok0 = False
                else:
                    ok1 = False
            elif p[52] > 0:  # AddMinSec: at most one add per pacing interval
                if side == 0:
                    ok0 = False
                else:
                    ok1 = False
        elif ev == 5:
            side = 0 if down else 1
            _track(S, cur, hs, cs)
            fillm = (cur - slip) if side == 0 else (cur + slip)
            S[STOPLOSS] += _close_side(S, side, fillm, hs, p, t)
            S[SIDESTOPS] += 1
            _track(S, cur, hs, cs)
        elif ev == 2:
            side = 1 if down else 0
            pnl = _close_side(S, side, cur, hs, p, t)
            S[TPPROFIT] += pnl
            if pnl > 0:
                S[WINS] += 1
            else:
                S[LOSSES] += 1
            _track(S, cur, hs, cs)
        else:
            _track(S, cur, hs, cs)
            _loss_close(S, cur, hs, p, t, ev == 3)
            _track(S, cur, hs, cs)


@njit(cache=True)
def _jump(S, m, hs, p, t, allow0, allow1, refcap):
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
    _track(S, m, hs, cs)
    if S[N0] + S[N1] > 0:
        A, B = _lin(S, hs, cs)
        x1, x2 = _thresholds(S, m, p, refcap)
        fl = A * m + B
        if fl <= -x2:
            _loss_close(S, m, hs, p, t, True)
        elif fl <= -x1:
            _loss_close(S, m, hs, p, t, False)
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
    (incl. stop slippage and commission) fits MaxBasketRiskPct of equity."""
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
        dist = stop_d - (k - 1) * step  # this layer's fill (ask) to the stop (bid)
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
def run_core(t, o, h, l, c, p, hmap, news, rec_daily, sig, sprd):
    n = t.shape[0]
    S = np.zeros(NSTATE)
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
    for i in range(n):
        ti = t[i]
        mod = (ti % 86400) // 60
        if use_sprd:
            spr_pts = sprd[i]  # broker bar spread from an MT5 export
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
        if ti - prev_t > 180:
            _jump(S, o[i], hs, p, ti, allow, allow, refcap)
        else:
            _segment(S, prev_c, o[i], hs, p, ti, allow, allow, refcap)
        # ---- Friday flatten (EA returns early: no adds, no opens)
        if p[14] > 0 and dow == 5 and hour >= p[15]:
            if S[N0] + S[N1] > 0:
                for sd in range(2):
                    S[STOPLOSS] += _close_side(S, sd, o[i], hs, p, ti)
                S[FRICLOSE] += 1
            prev_c = c[i]
            prev_t = ti
            continue
        # ---- max holding time: close a side whose FIRST position is >= the hold limit old
        if hold_sec > 0:
            _time_stops(S, o[i], hs, p, ti, hold_sec)
        # ---- v7 peak-equity kill latch + daily loss limit (also re-checked at every waypoint)
        _acct_guards(S, o[i], hs, p, ti)
        # ---- trading session window (server minutes, may wrap midnight)
        in_sess = True
        if sess_on:
            if p[41] < p[42]:
                in_sess = mod >= p[41] and mod < p[42]
            else:
                in_sess = mod >= p[41] or mod < p[42]
            if not in_sess and p[43] > 0 and S[N0] + S[N1] > 0:
                for sd in range(2):
                    S[STOPLOSS] += _close_side(S, sd, o[i], hs, p, ti)
                S[SESSCLOSE] += 1
        # ---- news pre-close
        if pre_news and S[N0] + S[N1] > 0:
            closed = False
            for sd in range(2):
                if p[22] >= 2 or _side_float(S, sd, o[i], hs, cs) >= 0:
                    if (S[N0] if sd == 0 else S[N1]) > 0:
                        S[STOPLOSS] += _close_side(S, sd, o[i], hs, p, ti)
                        closed = True
            if closed:
                S[NEWSCLOSE] += 1
        # ---- new entry-timeframe bar (M15 in v6.12; M1/M5 for the scalper): open idle grids
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
                        if want0 and S[N0] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                            px = o[i] + hs
                            _open(S, 0, px, lot1, p, ti)
                            S[TP0] = np.round((px + tpd) / point) * point
                            S[EFFML0] = effml
                            S[GRIDS] += 1
                        if want1 and S[N1] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                            px = o[i] - hs
                            _open(S, 1, px, lot1, p, ti)
                            S[TP1] = np.round((px - tpd) / point) * point
                            S[EFFML1] = effml
                            S[GRIDS] += 1
        # ---- intra-bar path
        nwp = _build_path(wp, wt, o[i], h[i], l[i], c[i], npts)
        for k in range(nwp - 1):
            tk = ti + wt[k] * 60.0
            if wp[k + 1] != wp[k]:
                _segment(S, wp[k], wp[k + 1], hs, p, tk, allow, allow, refcap)
            if hold_sec > 0:  # sub-minute clock: check the hold limit at every waypoint
                _time_stops(S, wp[k + 1], hs, p, ti + wt[k + 1] * 60.0, hold_sec)
            if p[53] > 0 or p[44] > 0:  # the EA checks these on every tick
                _acct_guards(S, wp[k + 1], hs, p, ti + wt[k + 1] * 60.0)
        prev_c = c[i]
        prev_t = ti
    # final
    hs = p[32] * point * 0.5
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
    return out, d_eq, d_bal


@njit(cache=True)
def len_stats():
    return 31


@njit(cache=True)
def _acct_guards(S, m, hs, p, t):
    """Kill latch at PeakKillPct below the running equity peak (the same peak the DD
    statistics use) and the daily loss limit vs the balance at the server-day start."""
    if S[N0] + S[N1] == 0:
        return
    a_, b_ = _lin(S, hs, p[27])
    eq = S[BAL] + a_ * m + b_
    if p[53] > 0 and S[KILL] == 0 and eq <= S[PEAK] * (1.0 - p[53] / 100.0):
        for sd in range(2):
            S[STOPLOSS] += _close_side(S, sd, m, hs, p, t)
        S[KILL] = 1
        return
    if p[44] > 0 and S[DAYHALT] == 0 and S[DAYBAL] > 0 and eq - S[DAYBAL] <= -p[44] / 100.0 * S[DAYBAL]:
        for sd in range(2):
            S[STOPLOSS] += _close_side(S, sd, m, hs, p, t)
        S[DAYHALT] = 1
        S[DAYSTOPS] += 1


@njit(cache=True)
def _time_stops(S, m, hs, p, t, hold_sec):
    for sd in range(2):
        if (S[N0] if sd == 0 else S[N1]) > 0:
            if t - (S[START0] if sd == 0 else S[START1]) >= hold_sec - 1e-6:
                S[STOPLOSS] += _close_side(S, sd, m, hs, p, t)
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
    hi_first = np.random.random() < 0.5
    t1 = np.random.uniform(0.05, 0.6)
    t2 = np.random.uniform(t1 + 0.05, 0.95)
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
            a, b, ta, tb = av2, av3, t2, 1.0
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
    wt[k] = 59.0 / 60.0
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


def run(data, params, hours=ORIGINAL_HOURS, news=None, daily=False):
    if news is None:
        news = np.zeros(0)
    sig = np.zeros(0, dtype=np.int8)
    if params[PI["EntryMode"]] > 0:
        sig = data.stretch_signal(params[PI["ZEntry"]], params[PI["ZEmaBars"]], params[PI["TrendTMax"]],
                                  10.0 * params[PI["Point"]])
    sprd = getattr(data, "spread", np.zeros(0))
    stats, d_eq, d_bal = run_core(data.t, data.o, data.h, data.l, data.c, params,
                                  hour_map(hours), np.asarray(news, dtype=np.float64), daily,
                                  sig, np.asarray(sprd, dtype=np.float64))
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
    return res
