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
)

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
NSTATE = 39

STAT_NAMES = [
    "final_balance", "final_equity", "max_dd_pct", "max_dd_abs", "min_equity", "wins", "losses",
    "basket_stops", "stopouts", "friday_closes", "news_closes", "commission", "swap",
    "layers_added", "max_layers_used", "grids_hit_max", "worst_float", "adds_blocked",
    "tp_profit", "stop_losses", "grids_opened", "worst_side_float",
]


@njit(cache=True)
def _lot_size(layer, p):
    base = p[0]
    flat = p[1]
    inc = p[2]
    every = p[3] if p[3] >= 1 else 1.0
    if layer <= flat:
        lot = base
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
def _close_side(S, side, m, hs, p):
    cs = p[27]
    half = p[28] * 0.5
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
def _open(S, side, fill, lot, p):
    half = p[28] * 0.5
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
    _open(S, side, fill, lot, p)
    S[LAYERS] += 1
    _set_tp(S, side, p)
    return True


@njit(cache=True)
def _loss_close(S, m, hs, p, t, is_stopout):
    for sd in range(2):
        pnl = _close_side(S, sd, m, hs, p)
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
    maxl = p[6]
    cur = a
    down = bnd < a
    ok0 = allow0
    ok1 = allow1
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
            if ok0 and S[N0] > 0 and S[N0] < maxl:
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
            if A > 0 and S[N0] + S[N1] > 0:
                for k in range(2):
                    x = x2 if k == 0 else x1
                    if x < 1e17:
                        ms = (-x - B) / A
                        if ms >= bnd and ms <= cur and ms > best:
                            best = ms; ev = 3 + k
        else:
            best = 1e18
            if ok1 and S[N1] > 0 and S[N1] < maxl:
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
        elif ev == 2:
            side = 1 if down else 0
            pnl = _close_side(S, side, cur, hs, p)
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
        pnl = _close_side(S, 0, m, hs, p)
        S[TPPROFIT] += pnl
        if pnl > 0:
            S[WINS] += 1
        else:
            S[LOSSES] += 1
    if S[N1] > 0 and m + hs <= S[TP1] + 1e-9:
        pnl = _close_side(S, 1, m, hs, p)
        S[TPPROFIT] += pnl
        if pnl > 0:
            S[WINS] += 1
        else:
            S[LOSSES] += 1
    _track(S, m, hs, cs)
    if S[N0] + S[N1] > 0:
        A, B = _lin(S, hs, cs)
        x1, x2 = _thresholds(S, m, p, refcap)
        fl = A * m + B
        if fl <= -x2:
            _loss_close(S, m, hs, p, t, True)
        elif fl <= -x1:
            _loss_close(S, m, hs, p, t, False)
    if allow0 and S[N0] > 0 and S[N0] < p[6] and m + hs <= S[LAST0] - step:
        _try_add(S, 0, m, hs, p, t)
    if allow1 and S[N1] > 0 and S[N1] < p[6] and m - hs >= S[LAST1] + step:
        _try_add(S, 1, m, hs, p, t)


@njit(cache=True)
def run_core(t, o, h, l, c, p, hmap, news, rec_daily):
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
    wp = np.zeros(4)
    for i in range(n):
        ti = t[i]
        mod = (ti % 86400) // 60
        spr_pts = p[32]
        if mod >= 1435 or mod < 15:
            spr_pts = p[32] * p[33]
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
                    S[STOPLOSS] += _close_side(S, sd, o[i], hs, p)
                S[FRICLOSE] += 1
            prev_c = c[i]
            prev_t = ti
            continue
        # ---- news pre-close
        if pre_news and S[N0] + S[N1] > 0:
            closed = False
            for sd in range(2):
                if p[22] >= 2 or _side_float(S, sd, o[i], hs, cs) >= 0:
                    if (S[N0] if sd == 0 else S[N1]) > 0:
                        S[STOPLOSS] += _close_side(S, sd, o[i], hs, p)
                        closed = True
            if closed:
                S[NEWSCLOSE] += 1
        # ---- new M15 bar: open idle grids
        m15 = ti // 900
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
                if time_ok and margin_ok and dd_ok and S[HALT] == 0 and spread_ok and news_ok:
                    tpd = p[5] * 10.0 * point
                    if S[N0] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                        px = o[i] + hs
                        _open(S, 0, px, lot1, p)
                        S[TP0] = np.round((px + tpd) / point) * point
                        S[GRIDS] += 1
                    if S[N1] == 0 and S[L0] + S[L1] + lot1 <= p[7] + 1e-9:
                        px = o[i] - hs
                        _open(S, 1, px, lot1, p)
                        S[TP1] = np.round((px - tpd) / point) * point
                        S[GRIDS] += 1
        # ---- intra-bar path
        wp[0] = o[i]
        if c[i] >= o[i]:
            wp[1] = l[i]; wp[2] = h[i]
        else:
            wp[1] = h[i]; wp[2] = l[i]
        wp[3] = c[i]
        for k in range(3):
            if wp[k + 1] != wp[k]:
                _segment(S, wp[k], wp[k + 1], hs, p, ti, allow, allow, refcap)
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
    return out, d_eq, d_bal


@njit(cache=True)
def len_stats():
    return 22


# ---------------------------------------------------------------- python API
class Data:
    def __init__(self, npz_path):
        z = np.load(npz_path)
        self.t = z["t_srv"].astype(np.float64)
        self.o = z["o"]; self.h = z["h"]; self.l = z["l"]; self.c = z["c"]

    def slice(self, start=None, end=None):
        """start/end as 'YYYY-MM-DD' (server time). Returns a view object."""
        import pandas as pd
        lo = 0 if start is None else int(np.searchsorted(self.t, pd.Timestamp(start).value // 10**9))
        hi = len(self.t) if end is None else int(np.searchsorted(self.t, pd.Timestamp(end).value // 10**9))
        d = Data.__new__(Data)
        d.t = self.t[lo:hi]; d.o = self.o[lo:hi]; d.h = self.h[lo:hi]; d.l = self.l[lo:hi]; d.c = self.c[lo:hi]
        return d


def run(data, params, hours=ORIGINAL_HOURS, news=None, daily=False):
    if news is None:
        news = np.zeros(0)
    stats, d_eq, d_bal = run_core(data.t, data.o, data.h, data.l, data.c, params,
                                  hour_map(hours), np.asarray(news, dtype=np.float64), daily)
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
