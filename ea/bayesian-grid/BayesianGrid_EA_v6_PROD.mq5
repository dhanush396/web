//+------------------------------------------------------------------+
//|                                      BayesianGrid_EA_v6_PROD.mq5  |
//|                          Acct #51946597 — Production (Final)      |
//|                                                                  |
//|  CORE (UNCHANGED FROM v6 — the tested edge):                     |
//|   - NO OnTradeTransaction. TP updates happen synchronously after |
//|     each layer add, scoped strictly by magic number.             |
//|   - WAVG computed from LIVE positions every tick. Zero stale     |
//|     state. SetTP() NEVER touches the opposite grid.              |
//|                                                                  |
//|  PRODUCTION ADDITIONS (all default to current live behaviour):   |
//|   - Panel() implemented (was called but undefined in v6).        |
//|   - OnInit input validation + magic-collision guard.             |
//|   - Margin gate now checks the ACTUAL layer-1 lot, not BaseLot.  |
//|   - Optional spread guard on new-grid opens (MaxSpreadPts).      |
//|   - Optional equity basket stop / DD halt (no per-pos SL exists).|
//|   - Accurate per-grid P/L attribution (tracks grid start time).  |
//|   - Stat persistence across restarts via GlobalVariables.        |
//|   - Time filter / Friday handling fully wired and panel-visible. |
//|                                                                  |
//|  TIME FILTER gates NEW-GRID OPENS ONLY. Existing grids continue  |
//|  adding layers and managing TPs 24/5 (by design).                |
//|                                                                  |
//|  v6.13 (small-account / news release):                           |
//|   - NEWS FILTER: MQL5 Economic Calendar live (high-impact events |
//|     of the symbol's currencies). In the Strategy Tester (where   |
//|     the calendar API is unavailable) it reads BG_news_calendar.csv|
//|     from MQL5\Files or Common\Files. Blocks NEW grids in the     |
//|     window; optional layer pause and pre-news close.             |
//|   - FIX: basket stop now keeps flattening until every EA position|
//|     is closed (v6.12 latched after one CloseAll pass, leaving any|
//|     failed closes open and still adding layers).                 |
//|   - FIX: CloseAll retries / cycles fill policy and reports fails.|
//|   - FIX: TP self-heal — a grid whose positions disagree on TP or |
//|     have TP=0 (SetTP failure / no-TP fallback) is re-synced.     |
//|   - FIX: grid start time is no longer overwritten on the first   |
//|     tick after a restart (recovery-seeded value is kept).        |
//|   - FIX: grid P/L attribution includes entry-deal commission.    |
//|   - NEW: LotIncEvery (step lot every N layers -> 0.01 ladders),  |
//|     HaltCooldownMin (auto-resume after a basket stop),           |
//|     RefCapital<0 = DD% vs current balance, start-up risk report. |
//|  All v6.12 inputs keep their meaning and defaults.               |
//|                                                                  |
//|  v7.00 HIGH-FREQUENCY GRID (bounded cycles; all OFF by default): |
//|   - EntryTF: open idle grids on M1/M5 bars (default M15 = v6).   |
//|   - MaxHoldSec: close a grid when its OLDEST position reaches N s|
//|     (every position is younger, so none is held longer).         |
//|   - StopPips: basket stop anchored to the layer-1 fill, mirrored |
//|     as a broker-side SL on every ticket (+BrokerSLBufferPips).   |
//|   - MaxBasketRiskPct: before layer 1, fit the deepest ladder     |
//|     whose loss at the stop fits N% of equity; skip if none fits. |
//|   - AddMinSec / NoAddAfterSec: add pacing and a no-add cutoff.   |
//|   - MaxSpreadPts now also guards layer adds.                     |
//|   - DailyLossPct, PeakKillPct (persistent kill latch).           |
//|   - SessionStart/SessionEnd (+CloseAtSessionEnd).                |
//|   - EntryMode STRETCH: open only the reversion side after a      |
//|     z-score stretch + reclaim on closed M1 bars (trend veto).    |
//|   - LotMultiplier: geometric ladder from BaseLot (0 = additive). |
//|   - FIX: SetTP keeps each position's SL (v6 sent SL=0).          |
//|  v7.01 (adversarial review): per-side layer-1 anchor + flatten   |
//|   latch (persisted) so a partly failed stop never re-anchors or  |
//|   averages; EnsureSL never loosens; hedging-account check;       |
//|   daily-loss state and equity peak persisted; close backoff;     |
//|   prices rounded to tick size; SL set right after each fill.     |
//|  v7.02 PROBABILITY EXIT (ProbExitMode, OFF by default) replaces  |
//|   the holding-period exit (keep MaxHoldSec = 0). On the first    |
//|   tick of each M1 bar a grid estimates P(TP before basket stop): |
//|   L = logit(Pf) + B0 + BZ*zs + BTrend*ts + BVol*ln(s1/sL)        |
//|       + BAge*ln(1+age min) + BLayers*(layers-1); P = 1/(1+e^-L)  |
//|   Pf = random-walk P(shared TP before the basket stop) INCLUDING |
//|   the grid's own future adds: each add trigger re-sets the TP    |
//|   from the new average until the ladder (fitted depth,           |
//|   MaxTotalLots) is full, then the stop is the far barrier        |
//|   (ProbRandomWalk = simulator _p_rw). Features from closed M1    |
//|   bars, EWMA variances normalised like pandas ewm(adjust=True).  |
//|   FLOOR exits if P < ProbMin, EDGE if L - logit(Pf) < -ProbEdge, |
//|   BOTH on either. Otherwise TP or stop. Needs 'Max bars in       |
//|   chart' >= max(1200, 6 x ProbVolLongBars) + 2 M1 bars.          |
//+------------------------------------------------------------------+
#property copyright   "Jeckov Kanani — Bayesian Grid v7.02 (HF)"
#property version     "7.02"
#property strict
#property tester_file "BG_news_calendar.csv"

enum ENUM_NEWS_IMP
  {
   NEWS_IMP_HIGH   = 3,   // High impact only
   NEWS_IMP_MEDIUM = 2    // Medium + high impact
  };

enum ENUM_NEWS_CLOSE
  {
   NEWS_CLOSE_NONE   = 0, // Do not close before news
   NEWS_CLOSE_PROFIT = 1, // Close grids that are in profit
   NEWS_CLOSE_ALL    = 2  // Close all EA grids
  };

enum ENUM_ENTRY_MODE
  {
   ENTRY_SYMMETRIC = 0,   // Open BUY and SELL grids together (v6)
   ENTRY_STRETCH   = 1    // Open only the reversion side after a stretch + reclaim
  };

enum ENUM_PROB_EXIT { PROB_OFF = 0, PROB_FLOOR = 1, PROB_EDGE = 2, PROB_BOTH = 3 };

input group           "══════ Grid Parameters ══════"
input double          BaseLot              = 0.08;     // Base lot (layers 1-5)
input int             FlatLayers           = 5;        // Flat-lot layers
input double          LotIncrement         = 0.07;     // Increment per layer after flat
input int             LotIncEvery          = 1;        // Apply LotIncrement every N layers after flat (1 = v6.12)
input double          LotMultiplier        = 0.0;      // >0: geometric ladder lot = BaseLot x mult^(layer-FlatLayers) (0 = additive)
input int             GridSpacingPts       = 75;       // Grid spacing in points (7.5 pips)
input double          TP_Pips              = 5.3;      // TP distance from wavg (pips)
input int             MaxLayers            = 18;       // Max layers per side
input int             MagicBuy             = 519401;   // BUY grid magic
input int             MagicSell            = 519402;   // SELL grid magic

input group           "══════ Risk ══════"
input double          MaxTotalLots         = 8.0;      // Hard cap on combined lots (both grids)
input double          MarginBufferMult     = 5.0;      // Require free margin >= this x (layer-1 margin)
input int             MaxSpreadPts         = 0;        // Skip NEW grids and layer adds if spread > this (0 = disabled)
input double          MaxEquityDD_Pct      = 0.0;      // Halt NEW grids when GRID float-loss% >= this (0 = off)
input double          EmergencyCloseDD_Pct = 0.0;      // CLOSE EA GRIDS when float-loss% >= this (0 = off). Basket stop.
input double          RefCapital           = 0.0;      // Reference capital for DD% (0 = balance at init, <0 = current balance)
input bool            ResetHaltOnInit      = true;     // Clear emergency-halt latch on (re)load
input int             HaltCooldownMin      = 0;        // Auto-clear halt this many minutes after a basket stop (0 = latch until reload)

input group           "══════ v7 High-Frequency Grid ══════"
input ENUM_TIMEFRAMES EntryTF              = PERIOD_M15; // Bar that opens idle grids (M1/M5 for HF; M15 = v6)
input ENUM_ENTRY_MODE EntryMode            = ENTRY_SYMMETRIC; // Symmetric (v6) or stretch-and-reclaim
input double          ZEntry               = 1.5;      // STRETCH: |z| needed, z = (close-EMA)/sigma15
input int             ZEmaBars             = 90;       // STRETCH: EMA length on M1 closes
input double          TrendTMax            = 2.0;      // STRETCH: veto when |60-bar move| / (sigma1m*sqrt60) >= this
input int             MaxHoldSec           = 0;        // Close a grid when its oldest position is N seconds old (0 = off)
input int             NoAddAfterSec        = 0;        // No new layers once the grid is N seconds old (0 = off)
input int             AddMinSec            = 0;        // Minimum seconds between two layers of one grid (0 = off)
input double          StopPips             = 0.0;      // Basket stop, pips from the layer-1 fill (0 = off)
input double          BrokerSLBufferPips   = 2.0;      // Broker-side SL sits this far beyond the basket stop
input double          MaxBasketRiskPct     = 0.0;      // Fit the ladder so its loss at StopPips <= N% of equity (0 = off)
input double          RiskSlipPts          = 2.0;      // Stop slippage assumed by the ladder fit (points)
input double          RiskCommPerLot       = 0.0;      // Round-trip commission per lot assumed by the ladder fit
input double          DailyLossPct         = 0.0;      // Flatten + stop for the day when the day's loss >= N% (0 = off)
input double          PeakKillPct          = 0.0;      // Flatten + latch when equity falls N% below its peak (0 = off)
input bool            ResetKillLatch       = false;    // Clear a persisted kill latch on load (manual review done)
input string          SessionStart         = "";       // Server time HH:MM; new grids only inside the session ("" = off)
input string          SessionEnd           = "";       // Server time HH:MM (may wrap midnight)
input bool            CloseAtSessionEnd    = false;    // Flatten EA grids outside the session

input group           "══════ v7.02 Probability Exit ══════"
input ENUM_PROB_EXIT  ProbExitMode         = PROB_OFF; // Exit a grid early only when P(TP before its stop) is down
input double          ProbMin              = 0.30;     // FLOOR: exit when P(TP first) < this
input double          ProbEdge             = 0.0;      // EDGE: exit when logit(P) - logit(P_fair) < -this (odds worse than a random walk)
input double          ProbB0               = 0.0;      // Model intercept (logit units)
input double          ProbBZ               = 0.0;      // Coef: stretch in favour of the grid (zs)
input double          ProbBTrend           = 0.0;      // Coef: 60-bar trend t-stat in favour of the grid (ts)
input double          ProbBVol             = 0.0;      // Coef: ln(sigma1 / sigmaLong)
input double          ProbBAge             = 0.0;      // Coef: ln(1 + basket age in minutes)
input double          ProbBLayers          = 0.0;      // Coef: open layers - 1
input int             ProbVolLongBars      = 1440;     // Half-life (M1 bars) of the long-run volatility

input group           "══════ News Filter ══════"
input bool            UseNewsFilter        = true;     // Block NEW grids around high-impact news
input string          NewsCurrencies       = "";       // Currencies to watch ("" = symbol base + quote, e.g. EUR,USD)
input ENUM_NEWS_IMP   NewsMinImportance    = NEWS_IMP_HIGH; // Minimum event importance
input int             NewsBeforeMin        = 30;       // Block window starts N min before the event
input int             NewsAfterMin         = 30;       // ... and ends N min after
input bool            NewsPauseLayers      = false;    // Also pause layer adds inside the window
input ENUM_NEWS_CLOSE NewsCloseMode        = NEWS_CLOSE_NONE; // Close grids before news
input int             NewsCloseMin         = 15;       // ... this many minutes before the event
input string          NewsCsvFile          = "BG_news_calendar.csv"; // Tester / fallback file (server time)
input int             NewsCsvShiftHours    = 0;        // Shift CSV times (CSV is NY-close GMT+2/+3 server time)

input group           "══════ Friday ══════"
input bool            CloseOnFriday        = false;    // Flatten EA positions Friday after hour
input int             FridayCloseHour      = 20;       // Server hour to flatten on Friday

input group           "══════ Time Filter ══════"
input bool            UseTimeFilter        = false;    // Gate NEW grid opens to AllowedHours
input string          AllowedHours         = "2,4,11,13,14,15,16,17,18,21,22"; // Hour list (see HoursInGMT)
input bool            HoursInGMT           = false;    // false = hours are broker server time (current). true = hours are GMT, auto-converted via ServerGMTOffset
input int             ServerGMTOffset      = 3;        // Broker server offset from GMT (Equiti/Vantage = 3 summer, 2 winter). Only used if HoursInGMT=true
input bool            BlockFriday          = false;    // Block all NEW grids on Friday (server day)

input group           "══════ Persistence & Debug ══════"
input bool            OpenOnStart          = false;    // SMOKE TEST: open 1 BUY + 1 SELL immediately on load (bypasses time filter ONCE). Real money — starts a live grid.
input bool            PersistStats         = true;     // Persist wins/losses/sessionPnL across restarts
input bool            ShowPanel            = true;     // On-chart dashboard
input bool            DebugLog             = true;     // Verbose journal logging

//--- runtime state
string   g_sym;
double   g_point;
int      g_digits;
double   g_peakBal;
int      g_wins = 0;
int      g_losses = 0;
double   g_sessionPnL = 0;
datetime g_lastBar = 0;
bool     g_hourMap[24];
bool     g_halt = false;          // emergency-close latch
datetime g_buyGridStart  = 0;     // for accurate P/L attribution
datetime g_sellGridStart = 0;
double   g_refCapital    = 0;     // reference capital for GRID-scoped DD%
bool     g_haltFlatten   = false; // basket stop fired: keep closing until flat
datetime g_haltUntil     = 0;     // cooldown expiry (HaltCooldownMin > 0)

//--- news state (times are trade-server time, ascending)
datetime g_newsTime[];
string   g_newsTitle[];
string   g_newsCur[];
int      g_newsCount    = 0;
int      g_newsIdx      = 0;
datetime g_newsLoadedAt = 0;
bool     g_newsFromCsv  = false;
string   g_newsCcys[];
bool     g_newsBlock    = false;  // inside a news window (evaluated each tick)
bool     g_newsPre      = false;  // inside the pre-news close window
datetime g_newsNextT    = 0;
string   g_newsNextName = "";

//--- v7 state
int      g_effMaxLayers[2];       // per-side ladder depth fitted at layer 1 (BUY=0, SELL=1)
datetime g_dayStart     = 0;      // server day of g_dayBal
double   g_dayBal       = 0;      // balance at the start of the server day
bool     g_dayHalt      = false;  // daily loss limit hit today
double   g_eqPeak       = 0;      // peak of balance + EA float
bool     g_kill         = false;  // peak-equity kill latch (persisted)
int      g_sessStart    = -1;     // session window in server minutes, -1 = off
int      g_sessEnd      = -1;
double   g_anchorPx[2];           // layer-1 fill of the open basket per side (0 = none), persisted
long     g_anchorMs[2];           // layer-1 fill time (ms), persisted
bool     g_sideFlat[2];           // per-side flatten latch: a stop/time/news close is in progress, persisted
datetime g_nextCloseTry[2];       // close retry backoff per side
int      g_closeBackoff[2];
datetime g_lastClose[2];          // time the previous grid of this side closed (stats window)
datetime g_dayHaltDay   = 0;      // server day on which the daily limit fired
double   g_savedPeak    = 0;      // last persisted equity peak
double   g_tickSize     = 0;

//--- v7.02 probability-exit state
datetime g_probLastBar  = 0;      // M1 bar on whose first tick the probability exit last ran
datetime g_probFeatBar  = 0;      // M1 bar the cached features belong to (0 = none)
double   g_probZ        = 0;      // cached features (closed M1 bars)
double   g_probTrend    = 0;
double   g_probLVol     = 0;
bool     g_probFeatWarned = false; // "features unavailable" warning already printed this run

//+------------------------------------------------------------------+
//| GlobalVariable name helper (per symbol + magic set)               |
//+------------------------------------------------------------------+
string GvName(string suffix)
  {
   return StringFormat("BG_%s_%d_%d_%s", g_sym, MagicBuy, MagicSell, suffix);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_sym    = _Symbol;
   g_point  = SymbolInfoDouble(g_sym, SYMBOL_POINT);
   g_digits = (int)SymbolInfoInteger(g_sym, SYMBOL_DIGITS);
   g_peakBal = AccountInfoDouble(ACCOUNT_BALANCE);

   //--- INPUT VALIDATION (fail fast — never run a bad config live) ---
   if(MagicBuy == MagicSell)
     {
      Print("FATAL: MagicBuy == MagicSell. Grids MUST be isolated. Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(BaseLot <= 0 || GridSpacingPts <= 0 || TP_Pips <= 0 || MaxLayers < 1 || FlatLayers < 1)
     {
      Print("FATAL: Invalid grid params (BaseLot/Spacing/TP/MaxLayers/FlatLayers). Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }
   double vmin = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN);
   if(BaseLot < vmin)
     {
      PrintFormat("FATAL: BaseLot %.2f < broker min %.2f. Aborting.", BaseLot, vmin);
      return INIT_PARAMETERS_INCORRECT;
     }
   if(g_point <= 0)
     {
      Print("FATAL: Symbol point size is zero. Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_tickSize = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_SIZE);
   if((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      Print("FATAL: a HEDGING account is required (two independent BUY/SELL grids per symbol). "
            "On a netting account the grids would merge into one position. Aborting.");
      return INIT_FAILED;
     }
   if(LotIncEvery < 1 || NewsBeforeMin < 0 || NewsAfterMin < 0 || NewsCloseMin < 0 || HaltCooldownMin < 0)
     {
      Print("FATAL: LotIncEvery must be >= 1 and news/cooldown minutes >= 0. Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(MaxHoldSec < 0 || NoAddAfterSec < 0 || AddMinSec < 0 || StopPips < 0 || BrokerSLBufferPips < 0
      || MaxBasketRiskPct < 0 || DailyLossPct < 0 || PeakKillPct < 0 || LotMultiplier < 0
      || (EntryMode == ENTRY_STRETCH && (ZEntry <= 0 || ZEmaBars < 2 || TrendTMax <= 0)))
     {
      Print("FATAL: invalid v7 HF inputs (negative values or STRETCH settings). Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(ProbExitMode != PROB_OFF
      && (StopPips <= 0 || ProbMin < 0 || ProbMin >= 1 || ProbEdge < 0 || ProbVolLongBars < 60 || ZEmaBars < 2))
     {
      PrintFormat("FATAL: ProbExitMode=%s needs StopPips > 0 (the basket stop is the loss barrier of P), "
                  "ProbMin in [0,1), ProbEdge >= 0, ProbVolLongBars >= 60 and ZEmaBars >= 2 "
                  "(got StopPips=%.1f ProbMin=%.3f ProbEdge=%.3f ProbVolLongBars=%d ZEmaBars=%d). Aborting.",
                  EnumToString(ProbExitMode), StopPips, ProbMin, ProbEdge, ProbVolLongBars, ZEmaBars);
      return INIT_PARAMETERS_INCORRECT;
     }
   g_sessStart = ParseHHMM(SessionStart);
   g_sessEnd   = ParseHHMM(SessionEnd);
   if((StringLen(SessionStart) > 0 && g_sessStart < 0) || (StringLen(SessionEnd) > 0 && g_sessEnd < 0)
      || ((g_sessStart < 0) != (g_sessEnd < 0)))
     {
      Print("FATAL: SessionStart/SessionEnd must both be HH:MM server time (or both empty). Aborting.");
      return INIT_PARAMETERS_INCORRECT;
     }

   //--- Parse allowed hours (map is indexed by SERVER hour) ---
   ArrayInitialize(g_hourMap, false);
   if(UseTimeFilter)
     {
      string parts[];
      StringSplit(AllowedHours, ',', parts);
      for(int i = 0; i < ArraySize(parts); i++)
        {
         int h = (int)StringToInteger(parts[i]);
         if(h < 0 || h > 23) continue;
         // If hours are GMT, convert to the equivalent SERVER hour: serverH = gmtH + offset
         int serverH = HoursInGMT ? ((h + ServerGMTOffset) % 24 + 24) % 24 : h;
         g_hourMap[serverH] = true;
        }
     }

   //--- Reference capital for grid-scoped DD% ---
   UpdateRefCapital(true);

   //--- Restore persisted stats / halt latch ---
   if(PersistStats)
     {
      if(GlobalVariableCheck(GvName("wins")))    g_wins       = (int)GlobalVariableGet(GvName("wins"));
      if(GlobalVariableCheck(GvName("losses")))  g_losses     = (int)GlobalVariableGet(GvName("losses"));
      if(GlobalVariableCheck(GvName("spnl")))    g_sessionPnL = GlobalVariableGet(GvName("spnl"));
      if(GlobalVariableCheck(GvName("peak")))    g_peakBal    = MathMax(g_peakBal, GlobalVariableGet(GvName("peak")));
      if(!ResetHaltOnInit && GlobalVariableCheck(GvName("halt")))
         g_halt = (GlobalVariableGet(GvName("halt")) > 0.5);
      if(!ResetHaltOnInit && GlobalVariableCheck(GvName("haltUntil")))
         g_haltUntil = (datetime)(long)GlobalVariableGet(GvName("haltUntil"));
     }
   if(ResetHaltOnInit)
     {
      g_halt = false;
      g_haltUntil = 0;
      if(GlobalVariableCheck(GvName("halt")))      GlobalVariableDel(GvName("halt"));
      if(GlobalVariableCheck(GvName("haltUntil"))) GlobalVariableDel(GvName("haltUntil"));
     }

   //--- v7: kill latch + equity peak persist across restarts (manual ResetKillLatch to clear) ---
   g_eqPeak = GridEquity();
   bool samePct = GlobalVariableCheck(GvName("killpct")) && MathAbs(GlobalVariableGet(GvName("killpct")) - PeakKillPct) < 1e-9;
   if(PeakKillPct > 0 && samePct && GlobalVariableCheck(GvName("eqpeak")) && !ResetKillLatch)
      g_eqPeak = MathMax(g_eqPeak, GlobalVariableGet(GvName("eqpeak")));  // a changed PeakKillPct restarts the peak
   g_savedPeak = g_eqPeak;
   GlobalVariableSet(GvName("killpct"), PeakKillPct);
   g_kill = (!ResetKillLatch && GlobalVariableCheck(GvName("kill")) && GlobalVariableGet(GvName("kill")) > 0.5);
   if(ResetKillLatch)
     {
      if(GlobalVariableCheck(GvName("kill")))   GlobalVariableDel(GvName("kill"));
      if(GlobalVariableCheck(GvName("eqpeak"))) GlobalVariableDel(GvName("eqpeak"));
      Print("!!! ResetKillLatch=true: the kill latch was cleared and will NOT persist while this stays true. "
            "Set it back to false after this restart.");
     }
   g_dayStart = 0;      // restored from GlobalVariables on the first tick (V7AccountGuards)
   g_dayHalt  = false;
   for(int k = 0; k < 2; k++)
     {
      int  magic = (k == 0) ? MagicBuy : MagicSell;
      int  cnt   = PosCount(magic);
      string sfx = (k == 0) ? "B" : "S";
      g_anchorPx[k] = 0; g_anchorMs[k] = 0; g_sideFlat[k] = false;
      g_nextCloseTry[k] = 0; g_closeBackoff[k] = 0; g_lastClose[k] = 0;
      if(cnt > 0)
        {
         if(GlobalVariableCheck(GvName("anc" + sfx + "px")))
           {
            g_anchorPx[k] = GlobalVariableGet(GvName("anc" + sfx + "px"));
            g_anchorMs[k] = (long)GlobalVariableGet(GvName("anc" + sfx + "ms"));
           }
         g_sideFlat[k] = GlobalVariableCheck(GvName("flat" + sfx)) && GlobalVariableGet(GvName("flat" + sfx)) > 0.5;
         int fit = GlobalVariableCheck(GvName("fit" + sfx)) ? (int)GlobalVariableGet(GvName("fit" + sfx))
                                                           : FitLayers(GridEquity());
         g_effMaxLayers[k] = (int)MathMax(cnt, fit);
        }
      else
        {
         ClearSide(k);
         g_effMaxLayers[k] = MaxLayers;
        }
     }
   if(MaxBasketRiskPct > 0 && StopPips <= 0)
      Print("!!! WARNING: MaxBasketRiskPct is ignored while StopPips=0 (no stop -> no bounded worst case).");
   if(StopPips > 0)
     {
      double minStop = (SymbolInfoInteger(g_sym, SYMBOL_TRADE_STOPS_LEVEL) + SymbolInfoInteger(g_sym, SYMBOL_SPREAD)) * g_point;
      if((StopPips + BrokerSLBufferPips) * 10.0 * g_point <= minStop)
         PrintFormat("!!! WARNING: StopPips+BrokerSLBufferPips (%.1f pips) is inside stops level + spread (%.1f pips): "
                     "the broker SL will be rejected and only the virtual stop protects.",
                     StopPips + BrokerSLBufferPips, minStop / (10.0 * g_point));
     }

   //--- A restored halt latch with EA positions still open = an unfinished basket stop ---
   if((g_halt || GlobalVariableCheck(GvName("haltFlatten"))) && PosCount(MagicBuy) + PosCount(MagicSell) > 0)
     {
      g_haltFlatten = true;
      Print("!!! Unfinished basket-stop flatten restored with open EA positions -> will finish closing them");
     }
   else if(GlobalVariableCheck(GvName("haltFlatten")))
      GlobalVariableDel(GvName("haltFlatten"));

   //--- News filter ---
   NewsSetupCurrencies();
   if(UseNewsFilter)
      NewsRefresh(true);

   //--- Recovery: detect existing grids and seed their start times ---
   int bc = PosCount(MagicBuy);
   int sc = PosCount(MagicSell);
   g_buyGridStart  = (bc > 0) ? OldestOpenTime(MagicBuy)  : 0;
   g_sellGridStart = (sc > 0) ? OldestOpenTime(MagicSell) : 0;

   PrintFormat("═══ BayesianGrid v7.02 HF ═══");
   PrintFormat("Sym=%s Pt=%.5f Digits=%d Spread=%d StopsLvl=%d",
               g_sym, g_point, g_digits,
               (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD),
               (int)SymbolInfoInteger(g_sym, SYMBOL_TRADE_STOPS_LEVEL));
   PrintFormat("Fill=%d VolMin=%.2f VolStep=%.2f",
               (int)SymbolInfoInteger(g_sym, SYMBOL_FILLING_MODE),
               SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN),
               SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP));
   PrintFormat("Base=%.2f Flat=%d Inc=%.2f/every %d Spacing=%d TP=%.1fpips Max=%d",
               BaseLot, FlatLayers, LotIncrement, LotIncEvery, GridSpacingPts, TP_Pips, MaxLayers);
   PrintFormat("Risk: MaxLots=%.2f MarginMult=%.1f MaxSpread=%d DDhalt=%.1f%% Basket=%.1f%% RefCap=%.2f%s Cooldown=%dmin",
               MaxTotalLots, MarginBufferMult, MaxSpreadPts, MaxEquityDD_Pct, EmergencyCloseDD_Pct, g_refCapital,
               RefCapital < 0 ? "(dynamic)" : "", HaltCooldownMin);
   PrintFormat("News: %s src=%s events=%d ccy=[%s] imp>=%d window=-%d/+%dmin pauseLayers=%s close=%d@-%dmin",
               UseNewsFilter ? "ON" : "OFF", g_newsFromCsv ? "CSV" : "CALENDAR", g_newsCount,
               NewsCcyList(), (int)NewsMinImportance, NewsBeforeMin, NewsAfterMin,
               NewsPauseLayers ? "Y" : "N", (int)NewsCloseMode, NewsCloseMin);
   RiskPreflight();
   PrintFormat("TimeFilter=%s Hours=[%s] InGMT=%s SrvOff=%d BlockFri=%s CloseFri=%s@%d",
               UseTimeFilter ? "ON" : "OFF", AllowedHours,
               HoursInGMT ? "Y" : "N", ServerGMTOffset,
               BlockFriday ? "Y" : "N", CloseOnFriday ? "Y" : "N", FridayCloseHour);
   PrintFormat("v7: EntryTF=%s Mode=%s Hold=%ds NoAdd=%ds AddMin=%ds Stop=%.1fp(+%.1f broker) Budget=%.1f%% "
               "Day=%.1f%% Kill=%.1f%%%s Session=%s-%s%s ProbExit=%s Pmin=%.3f Edge=%.3f",
               EnumToString(EntryTF), EntryMode == ENTRY_STRETCH ? "STRETCH" : "SYMMETRIC", MaxHoldSec,
               NoAddAfterSec, AddMinSec, StopPips, BrokerSLBufferPips, MaxBasketRiskPct, DailyLossPct, PeakKillPct,
               g_kill ? " (LATCHED)" : "", SessionStart, SessionEnd, CloseAtSessionEnd ? " close@end" : "",
               EnumToString(ProbExitMode), ProbMin, ProbEdge);
   g_probFeatWarned = false;
   if(ProbExitMode != PROB_OFF)
     {
      PrintFormat("v7.02 prob model: B0=%.4f BZ=%.4f BTrend=%.4f BVol=%.4f BAge=%.4f BLayers=%.4f VolLong=%d bars EMA=%d",
                  ProbB0, ProbBZ, ProbBTrend, ProbBVol, ProbBAge, ProbBLayers, ProbVolLongBars, ZEmaBars);
      //--- ProbFeatures() copies max(1200, 6 x ProbVolLongBars) CLOSED M1 bars: the chart must hold them
      int probNeed = (int)MathMax(1200, 6 * ProbVolLongBars);
      int maxBars  = (int)TerminalInfoInteger(TERMINAL_MAXBARS);
      if(maxBars < probNeed + 2)
         PrintFormat("!!! WARNING: ProbExitMode=%s needs %d M1 bars (max(1200, 6 x ProbVolLongBars=%d) closed bars + 2) "
                     "but 'Max bars in chart' is %d: the probability exit will stay INACTIVE until "
                     "Tools > Options > Charts > 'Max bars in chart' is raised to at least %d.",
                     EnumToString(ProbExitMode), probNeed + 2, ProbVolLongBars, maxBars, probNeed + 2);
     }
   if(StopPips > 0)
      PrintFormat("v7: ladder fit at equity %.2f -> %d of %d layers", GridEquity(), FitLayers(GridEquity()), MaxLayers);
   PrintFormat("Recovery: BUY=%d positions, SELL=%d positions. Halt=%s",
               bc, sc, g_halt ? "LATCHED" : "clear");
   PrintFormat("═══ INIT DONE ═══");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(PersistStats)
     {
      GlobalVariableSet(GvName("wins"),   g_wins);
      GlobalVariableSet(GvName("losses"), g_losses);
      GlobalVariableSet(GvName("spnl"),   g_sessionPnL);
      GlobalVariableSet(GvName("peak"),   g_peakBal);
      GlobalVariableSet(GvName("halt"),   g_halt ? 1 : 0);
      GlobalVariableSet(GvName("haltUntil"), (double)(long)g_haltUntil);
     }
   GlobalVariableSet(GvName("eqpeak"), g_eqPeak);
   GlobalVariableSet(GvName("kill"), g_kill ? 1 : 0);
   ObjectsDeleteAll(0, "BG_");
   Comment("");
  }

//+------------------------------------------------------------------+
//| MAIN TICK                                                         |
//+------------------------------------------------------------------+
void OnTick()
  {
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if(bal > g_peakBal) g_peakBal = bal;
   UpdateRefCapital(false);

   //--- Basket stop follow-through: never leave a half-closed basket running ---
   if(g_haltFlatten)
     {
      if(PosCount(MagicBuy) + PosCount(MagicSell) > 0)
        {
         int fails = (int)MathMax(0, CloseSide(0)) + (int)MathMax(0, CloseSide(1));
         if(fails > 0 && DebugLog) PrintFormat("!!! HALT FLATTEN: %d position(s) still open, retrying with backoff", fails);
         if(ShowPanel) Panel();
         return;
        }
      g_haltFlatten = false;
      if(GlobalVariableCheck(GvName("haltFlatten"))) GlobalVariableDel(GvName("haltFlatten"));
      Print(">>> HALT FLATTEN complete: all EA positions closed");
     }

   //--- Halt cooldown (only when HaltCooldownMin > 0 and the EA is flat) ---
   if(g_halt && HaltCooldownMin > 0 && g_haltUntil > 0 && TimeCurrent() >= g_haltUntil
      && PosCount(MagicBuy) + PosCount(MagicSell) == 0)
     {
      g_halt = false;
      g_haltUntil = 0;
      if(PersistStats)
        {
         GlobalVariableSet(GvName("halt"), 0);
         GlobalVariableSet(GvName("haltUntil"), 0);
        }
      PrintFormat(">>> HALT COOLDOWN (%d min) elapsed: new grids re-enabled", HaltCooldownMin);
     }

   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   double acctDD = (g_peakBal > 0) ? (g_peakBal - eq) / g_peakBal * 100.0 : 0; // ACCOUNT-level, display only (can be polluted by foreign positions)

   // GRID-SCOPED drawdown: only THIS EA's open float, vs reference capital.
   // Immune to manual trades / other EAs on the same account.
   double gridFloat = FloatPnL(MagicBuy) + FloatPnL(MagicSell);
   double gridDD    = (gridFloat < 0) ? (-gridFloat / g_refCapital * 100.0) : 0.0;

   //--- EMERGENCY BASKET STOP (grid-scoped; only if explicitly enabled) ---
   if(EmergencyCloseDD_Pct > 0 && gridDD >= EmergencyCloseDD_Pct && !g_halt)
     {
      PrintFormat("!!! EMERGENCY BASKET STOP: gridDD=%.2f%% >= %.2f%% (float=$%.2f) — CLOSING EA GRIDS",
                  gridDD, EmergencyCloseDD_Pct, gridFloat);
      CloseAll(MagicBuy);
      CloseAll(MagicSell);
      g_halt = true;
      g_haltFlatten = true;
      GlobalVariableSet(GvName("haltFlatten"), 1);
      GlobalVariablesFlush();
      g_haltUntil = (HaltCooldownMin > 0) ? (datetime)(TimeCurrent() + HaltCooldownMin * 60) : (datetime)0;
      if(PersistStats)
        {
         GlobalVariableSet(GvName("halt"), 1);
         GlobalVariableSet(GvName("haltUntil"), (double)(long)g_haltUntil);
        }
      if(ShowPanel) Panel();
      return;
     }

   //--- Friday flatten ---
   if(CloseOnFriday)
     {
      MqlDateTime dt;
      TimeToStruct(TimeCurrent(), dt);
      if(dt.day_of_week == 5 && dt.hour >= FridayCloseHour)
        {
         CloseAll(MagicBuy);
         CloseAll(MagicSell);
         if(ShowPanel) Panel();
         return;
        }
     }

   //--- v7: per-grid basket stop + holding-time stop + v7.02 probability exit, then account guards ---
   V7GridExits();
   if(!V7AccountGuards())
     {
      if(ShowPanel) Panel();
      return;
     }

   //--- News filter state (refresh is throttled inside) ---
   if(UseNewsFilter)
     {
      NewsRefresh(false);
      NewsEval(TimeCurrent());
     }
   else
     {
      g_newsBlock = false;
      g_newsPre   = false;
     }

   //--- Pre-news close (optional) ---
   if(g_newsPre && NewsCloseMode != NEWS_CLOSE_NONE)
     {
      int mags[2];
      mags[0] = MagicBuy;
      mags[1] = MagicSell;
      for(int k = 0; k < 2; k++)
        {
         if(PosCount(mags[k]) == 0) continue;
         if(NewsCloseMode == NEWS_CLOSE_ALL || FloatPnL(mags[k]) >= 0)
           {
            PrintFormat(">>> PRE-NEWS CLOSE %s grid (float=$%.2f) before %s",
                        mags[k] == MagicBuy ? "BUY" : "SELL", FloatPnL(mags[k]), g_newsNextName);
            LatchSide(k);   // finish the close even if the remainder turns negative
            CloseSide(k);
           }
        }
     }

   //--- Read LIVE grid state from positions ---
   double bWavg, bLastPx, bLots;
   int    bCount;
   ReadGrid(MagicBuy, bWavg, bLastPx, bLots, bCount);

   double sWavg, sLastPx, sLots;
   int    sCount;
   ReadGrid(MagicSell, sWavg, sLastPx, sLots, sCount);

   //--- Track grid start times (for accurate P/L attribution) ---
   static int s_prevBuyCount  = -1;
   static int s_prevSellCount = -1;

   // -1 = first tick after (re)load: keep the start time OnInit recovered from the open positions
   if(s_prevBuyCount == 0 && bCount > 0)  g_buyGridStart  = OldestOpenTime(MagicBuy);
   if(s_prevSellCount == 0 && sCount > 0) g_sellGridStart = OldestOpenTime(MagicSell);
   if(s_prevBuyCount < 0 && bCount > 0 && g_buyGridStart == 0)   g_buyGridStart  = OldestOpenTime(MagicBuy);
   if(s_prevSellCount < 0 && sCount > 0 && g_sellGridStart == 0) g_sellGridStart = OldestOpenTime(MagicSell);

   //--- Detect grid closed by TP ---
   if(s_prevBuyCount > 0 && bCount == 0)
     {
      double pnl = RecentPnL(MagicBuy, (datetime)MathMax((long)g_buyGridStart - 60, (long)g_lastClose[0] + 1));
      g_lastClose[0] = TimeCurrent();
      if(pnl > 0) g_wins++; else g_losses++;
      g_sessionPnL += pnl;
      PrintFormat(">>> BUY GRID CLOSED: P/L=$%.2f (was %d layers)", pnl, s_prevBuyCount);
      PersistSnapshot();
     }
   if(s_prevSellCount > 0 && sCount == 0)
     {
      double pnl = RecentPnL(MagicSell, (datetime)MathMax((long)g_sellGridStart - 60, (long)g_lastClose[1] + 1));
      g_lastClose[1] = TimeCurrent();
      if(pnl > 0) g_wins++; else g_losses++;
      g_sessionPnL += pnl;
      PrintFormat(">>> SELL GRID CLOSED: P/L=$%.2f (was %d layers)", pnl, s_prevSellCount);
      PersistSnapshot();
     }
   s_prevBuyCount  = bCount;
   s_prevSellCount = sCount;

   //--- TP self-heal (once a minute): re-sync grids with inconsistent / missing TPs ---
   static datetime s_lastHeal = 0;
   if(TimeCurrent() - s_lastHeal >= 60)
     {
      s_lastHeal = TimeCurrent();
      if(!g_sideFlat[0]) { HealTP(ORDER_TYPE_BUY,  MagicBuy,  bWavg, bCount); EnsureSL(ORDER_TYPE_BUY,  MagicBuy); }
      if(!g_sideFlat[1]) { HealTP(ORDER_TYPE_SELL, MagicSell, sWavg, sCount); EnsureSL(ORDER_TYPE_SELL, MagicSell); }
     }

   //--- Active grids: add layers (NEVER gated by time/halt — must reach TP) ---
   //    Optional exception: NewsPauseLayers holds adds inside a news window.
   bool layersOK = !(g_newsBlock && NewsPauseLayers);
   if(bCount > 0 && layersOK && !g_sideFlat[0])
      TryAddLayer(ORDER_TYPE_BUY, MagicBuy, bWavg, bLastPx, bLots, bCount);

   if(sCount > 0 && layersOK && !g_sideFlat[1])
      TryAddLayer(ORDER_TYPE_SELL, MagicSell, sWavg, sLastPx, sLots, sCount);

   //--- Idle grids: open new ones (gated) ---
   datetime curBar = iTime(g_sym, EntryTF, 0);
   if(curBar != g_lastBar)
     {
      g_lastBar = curBar;

      bool timeOK   = IsTimeOK();
      bool riskOK   = MarginOKForLot(LotSize(1));
      bool ddOK     = (MaxEquityDD_Pct <= 0 || gridDD < MaxEquityDD_Pct);
      bool haltOK   = !g_halt;
      bool spreadOK = SpreadOK();
      bool newsOK   = !g_newsBlock;
      bool sessOK   = InSession() && !g_dayHalt && !g_kill;

      // SMOKE TEST: force the very first grid open after load, bypassing ONLY the time filter.
      static bool s_startupOpened = false;
      bool forceStart = (OpenOnStart && !s_startupOpened && bCount == 0 && sCount == 0);

      bool openOK   = (timeOK || forceStart) && riskOK && ddOK && haltOK && spreadOK && newsOK && sessOK;

      if(DebugLog)
         PrintFormat("[BAR] Buy=%d Sell=%d Time=%s%s Margin=%s GridDD=%.1f%%(%s) AcctDD=%.1f%% Halt=%s Spread=%s News=%s Sess=%s -> %s",
                     bCount, sCount,
                     timeOK ? "OK" : "BLOCK",
                     forceStart ? "(START-FORCE)" : "",
                     riskOK ? "OK" : "BLOCK",
                     gridDD, ddOK ? "OK" : "HALT",
                     acctDD,
                     haltOK ? "OK" : "LATCHED",
                     spreadOK ? "OK" : "WIDE",
                     newsOK ? "OK" : "BLOCK(" + g_newsNextName + ")",
                     sessOK ? "OK" : (g_kill ? "KILLED" : (g_dayHalt ? "DAY-STOP" : "OUT")),
                     openOK ? "OPEN-ALLOWED" : "OPEN-BLOCKED");

      if(openOK)
        {
         if(forceStart)
            PrintFormat(">>> OPEN-ON-START: forcing confirmation grids (time filter bypassed once)");
         bool wantBuy = true, wantSell = true;
         if(EntryMode == ENTRY_STRETCH && !forceStart)
           {
            int sig = StretchSignal();
            wantBuy  = (sig > 0);
            wantSell = (sig < 0);
           }
         int fit = FitLayers(GridEquity());
         if(fit < 1)
           {
            if(DebugLog) PrintFormat("L1 BLOCK: no ladder fits MaxBasketRiskPct=%.1f%% at equity %.2f",
                                     MaxBasketRiskPct, GridEquity());
           }
         else
           {
            if(bCount == 0 && wantBuy && !g_sideFlat[0] && OpenLayer1(ORDER_TYPE_BUY, MagicBuy))
              {
               g_effMaxLayers[0] = fit;
               GlobalVariableSet(GvName("fitB"), fit);
              }
            if(sCount == 0 && wantSell && !g_sideFlat[1] && OpenLayer1(ORDER_TYPE_SELL, MagicSell))
              {
               g_effMaxLayers[1] = fit;
               GlobalVariableSet(GvName("fitS"), fit);
              }
           }
         if(forceStart)
            s_startupOpened = true;
        }
     }

   if(ShowPanel) Panel();
  }

//+------------------------------------------------------------------+
//| READ live grid state from open positions (ZERO stale state)       |
//+------------------------------------------------------------------+
void ReadGrid(int magic, double &wavg, double &lastPx, double &totalLots, int &count)
  {
   wavg = 0; lastPx = 0; totalLots = 0; count = 0;
   double sumPxVol = 0;
   datetime newest = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;

      double px  = PositionGetDouble(POSITION_PRICE_OPEN);
      double vol = PositionGetDouble(POSITION_VOLUME);
      datetime ot = (datetime)PositionGetInteger(POSITION_TIME);

      sumPxVol  += px * vol;
      totalLots += vol;
      count++;

      if(ot > newest)
        {
         newest = ot;
         lastPx = px;
        }
     }

   if(count > 0 && totalLots > 0)
      wavg = sumPxVol / totalLots;
  }

//+------------------------------------------------------------------+
//| Open first layer of a new grid                                    |
//+------------------------------------------------------------------+
bool OpenLayer1(ENUM_ORDER_TYPE type, int magic)
  {
   double lot = LotSize(1);

   //--- respect combined-lots cap even on a fresh open ---
   if(TotalLots() + lot > MaxTotalLots)
     {
      if(DebugLog) PrintFormat("L1 BLOCK: lots cap %.2f+%.2f>%.2f", TotalLots(), lot, MaxTotalLots);
      return false;
     }

   MqlTick tick;
   if(!SymbolInfoTick(g_sym, tick)) return false;

   double price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;

   double tp;
   if(type == ORDER_TYPE_BUY)
      tp = ND(price + TP_Pips * 10.0 * g_point);
   else
      tp = ND(price - TP_Pips * 10.0 * g_point);

   tp = ClampTP(tp, type);
   double sl = (StopPips > 0) ? BrokerSL(type, price) : 0.0;

   double fill = 0;
   if(MarketSend(type, lot, tp, sl, magic, "L1", fill))
     {
      PrintFormat(">>> NEW %s GRID: fill=%.5f TP=%.5f SL=%.5f lot=%.2f",
                  type == ORDER_TYPE_BUY ? "BUY" : "SELL",
                  fill > 0 ? fill : price, tp, sl, lot);
      int side = (type == ORDER_TYPE_BUY) ? 0 : 1;
      long oMs, nMs;
      double fPx;
      if(GridTimes(magic, oMs, nMs, fPx))
         SetAnchor(side, fPx, oMs);                           // the broker's fill and time
      else
         SetAnchor(side, fill > 0 ? fill : price, tick.time_msc);
      if(StopPips > 0) EnsureSL(type, magic);                 // SL from the real fill, not the request
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Try adding a layer to an active grid                              |
//+------------------------------------------------------------------+
void TryAddLayer(ENUM_ORDER_TYPE type, int magic,
                 double wavg, double lastPx, double totalLots, int count)
  {
   int side = (type == ORDER_TYPE_BUY) ? 0 : 1;
   int maxL = (g_effMaxLayers[side] > 0) ? (int)MathMin(g_effMaxLayers[side], MaxLayers) : MaxLayers;
   if(count >= maxL) return;

   double step = GridSpacingPts * g_point;

   MqlTick tick;
   if(!SymbolInfoTick(g_sym, tick)) return;

   double curPx = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;

   bool shouldAdd = false;
   if(type == ORDER_TYPE_BUY && lastPx - curPx >= step)
      shouldAdd = true;
   if(type == ORDER_TYPE_SELL && curPx - lastPx >= step)
      shouldAdd = true;

   if(!shouldAdd) return;

   //--- v7 add gating: spread guard, pacing, no-add cutoff (all off by default) ---
   if(MaxSpreadPts > 0 && (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD) > MaxSpreadPts) return;
   long oldestMs, newestMs;
   double firstPx;
   if(!GridTimes(magic, oldestMs, newestMs, firstPx)) return;
   long ancMs;
   SideAnchor(side, firstPx, ancMs);                          // layer-1 anchor, not the oldest survivor
   if(AddMinSec > 0 && tick.time_msc - newestMs < (long)AddMinSec * 1000) return;
   if(NoAddAfterSec > 0 && tick.time_msc - ancMs >= (long)NoAddAfterSec * 1000) return;

   int    nextLayer = count + 1;
   double lot       = LotSize(nextLayer);

   double allLots = TotalLots();
   if(allLots + lot > MaxTotalLots)
     {
      PrintFormat("LOTS CAP: %.2f + %.2f > %.2f", allLots, lot, MaxTotalLots);
      return;
     }

   double fill = 0;
   double sl = (StopPips > 0) ? BrokerSL(type, firstPx) : 0.0;
   if(!MarketSend(type, lot, 0, sl, magic, "L" + IntegerToString(nextLayer), fill))
      return;

   double actualPx = (fill > 0) ? fill : curPx;

   if(!MQLInfoInteger(MQL_TESTER))
      Sleep(150);

   double newWavg, newLastPx, newLots;
   int    newCount;
   ReadGrid(magic, newWavg, newLastPx, newLots, newCount);

   if(newCount <= count)
     {
      newWavg = (wavg * totalLots + actualPx * lot) / (totalLots + lot);
      newCount = count + 1;
      newLots = totalLots + lot;
     }

   double newTP;
   if(type == ORDER_TYPE_BUY)
      newTP = ND(newWavg + TP_Pips * 10.0 * g_point);
   else
      newTP = ND(newWavg - TP_Pips * 10.0 * g_point);

   newTP = ClampTP(newTP, type);

   PrintFormat(">>> LAYER %d %s: fill=%.5f lot=%.2f | wavg=%.5f TP=%.5f lots=%.2f",
               newCount, type == ORDER_TYPE_BUY ? "BUY" : "SELL",
               actualPx, lot, newWavg, newTP, newLots);

   SetTP(magic, newTP);
   if(StopPips > 0) EnsureSL(type, magic);
  }

//+------------------------------------------------------------------+
//| Set TP on all positions of a specific magic number ONLY           |
//| CRITICAL ISOLATION POINT — NEVER touches the other grid.          |
//+------------------------------------------------------------------+
void SetTP(int magic, double tp)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;

      double curTP = PositionGetDouble(POSITION_TP);
      if(MathAbs(curTP - tp) < 0.5 * MathMax(g_tickSize, g_point)) continue;

      MqlTradeRequest  req = {};
      MqlTradeResult   res = {};
      req.action   = TRADE_ACTION_SLTP;
      req.symbol   = g_sym;
      req.position = ticket;
      req.tp       = tp;
      req.sl       = PositionGetDouble(POSITION_SL);   // v7 fix: keep the broker-side SL

      ResetLastError();
      if(!OrderSend(req, res))
        {
         if(DebugLog)
            PrintFormat("  TP SET FAIL: pos=%d ret=%d err=%d tp=%.5f",
                        (int)ticket, res.retcode, GetLastError(), tp);
        }
     }
  }

//+------------------------------------------------------------------+
//| Market order with fill-policy cycling and fallback                |
//+------------------------------------------------------------------+
bool MarketSend(ENUM_ORDER_TYPE type, double lot, double tp, double sl, int magic,
                string comment, double &outFill)
  {
   outFill = 0;
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = g_sym;
   req.volume       = lot;
   req.type         = type;
   req.deviation    = 30;
   req.magic        = magic;
   req.comment      = "BG_" + comment;
   req.tp           = tp;
   req.sl           = sl;
   req.type_filling = FillPolicy();

   MqlTick tick;
   if(!SymbolInfoTick(g_sym, tick)) return false;
   req.price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;

   for(int a = 0; a < 3; a++)
     {
      ResetLastError();
      if(OrderSend(req, res))
        {
         if(res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED)
           {
            outFill = res.price;
            return true;
           }
        }

      PrintFormat("  SEND FAIL #%d: ret=%d [%s] err=%d fill=%s",
                  a+1, res.retcode, res.comment, GetLastError(),
                  EnumToString(req.type_filling));

      if(res.retcode == TRADE_RETCODE_INVALID_FILL)
        {
         if(req.type_filling == ORDER_FILLING_FOK)       req.type_filling = ORDER_FILLING_IOC;
         else if(req.type_filling == ORDER_FILLING_IOC)   req.type_filling = ORDER_FILLING_RETURN;
         else                                              req.type_filling = ORDER_FILLING_FOK;
        }
      else if(res.retcode == TRADE_RETCODE_INVALID_STOPS)
        {
         if(req.tp != 0) req.tp = 0;   // drop TP first (HealTP restores it) ...
         else            req.sl = 0;   // ... then SL (virtual stop + EnsureSL cover it)
        }
      else if(res.retcode == TRADE_RETCODE_NO_MONEY)
         return false;

      if(!SymbolInfoTick(g_sym, tick)) return false;
      req.price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;
      if(!MQLInfoInteger(MQL_TESTER)) Sleep(200);
     }

   //--- Fallback: no TP / SL (HealTP and EnsureSL restore them; the virtual stop stays active) ---
   req.tp = 0;
   req.sl = 0;
   if(!SymbolInfoTick(g_sym, tick)) return false;
   req.price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;
   ResetLastError();
   if(OrderSend(req, res))
     {
      if(res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED)
        {
         outFill = res.price;
         return true;
        }
     }
   PrintFormat("  !!! FINAL FAIL ret=%d err=%d", res.retcode, GetLastError());
   return false;
  }

//+------------------------------------------------------------------+
//| Lot size for given layer                                          |
//+------------------------------------------------------------------+
double LotSize(int layer)
  {
   double lot;
   if(layer <= FlatLayers)
      lot = BaseLot;
   else if(LotMultiplier > 0)
      lot = BaseLot * MathPow(LotMultiplier, layer - FlatLayers);   // v7 geometric ladder
   else
     {
      int every = MathMax(1, LotIncEvery);
      int steps = (layer - FlatLayers + every - 1) / every;   // ceil; every=1 -> v6.12 ladder
      lot = BaseLot + steps * LotIncrement;
     }

   double minL = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN);
   double maxL = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP);

   if(step > 0)
      lot = NormalizeDouble(MathRound(lot / step) * step, 2);
   lot = MathMax(lot, minL);
   lot = MathMin(lot, maxL);
   return lot;
  }

//+------------------------------------------------------------------+
//| Clamp TP to respect minimum stop level                            |
//+------------------------------------------------------------------+
double ClampTP(double tp, ENUM_ORDER_TYPE type)
  {
   int minPts = MathMax((int)SymbolInfoInteger(g_sym, SYMBOL_TRADE_STOPS_LEVEL),
                        (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD)) + 5;
   double minD = minPts * g_point;

   MqlTick tick;
   if(!SymbolInfoTick(g_sym, tick)) return tp;

   if(type == ORDER_TYPE_BUY)
     {
      double floorPx = ND(tick.bid + minD);
      if(tp < floorPx) tp = floorPx;
     }
   else
     {
      double ceilPx = ND(tick.ask - minD);
      if(tp > ceilPx) tp = ceilPx;
     }
   return tp;
  }

//+------------------------------------------------------------------+
//| Conditions / guards                                               |
//+------------------------------------------------------------------+
bool IsTimeOK()
  {
   if(!UseTimeFilter) return true;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(BlockFriday && dt.day_of_week == 5) return false;
   return g_hourMap[dt.hour];
  }

bool MarginOKForLot(double lot)
  {
   double fm = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double mr = 0;
   if(!OrderCalcMargin(ORDER_TYPE_BUY, g_sym, lot,
                       SymbolInfoDouble(g_sym, SYMBOL_ASK), mr))
      return false;
   return (fm >= mr * MarginBufferMult);
  }

bool SpreadOK()
  {
   if(MaxSpreadPts <= 0) return true;
   return (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD) <= MaxSpreadPts;
  }

//+------------------------------------------------------------------+
//| Helpers                                                           |
//+------------------------------------------------------------------+
//--- price normaliser: rounds to the symbol's tick size (tick > point on some CFDs), then digits
double ND(double v)
  {
   if(g_tickSize > 0) v = MathRound(v / g_tickSize) * g_tickSize;
   return NormalizeDouble(v, g_digits);
  }

void PersistSnapshot()
  {
   if(!PersistStats) return;
   GlobalVariableSet(GvName("wins"),   g_wins);
   GlobalVariableSet(GvName("losses"), g_losses);
   GlobalVariableSet(GvName("spnl"),   g_sessionPnL);
   GlobalVariableSet(GvName("peak"),   g_peakBal);
  }

ENUM_ORDER_TYPE_FILLING FillPolicy()
  {
   long m = SymbolInfoInteger(g_sym, SYMBOL_FILLING_MODE);
   if(m & SYMBOL_FILLING_FOK) return ORDER_FILLING_FOK;
   if(m & SYMBOL_FILLING_IOC) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
  }

int PosCount(int magic)
  {
   int c = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      c++;
     }
   return c;
  }

datetime OldestOpenTime(int magic)
  {
   datetime oldest = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      datetime ot = (datetime)PositionGetInteger(POSITION_TIME);
      if(oldest == 0 || ot < oldest) oldest = ot;
     }
   return (oldest == 0) ? TimeCurrent() : oldest;
  }

double TotalLots()
  {
   double s = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      long m = PositionGetInteger(POSITION_MAGIC);
      if(m != MagicBuy && m != MagicSell) continue;
      s += PositionGetDouble(POSITION_VOLUME);
     }
   return s;
  }

double FloatPnL(int magic)
  {
   double p = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      p += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return p;
  }

//--- Accurate P/L: select history from this grid's actual start time ---
double RecentPnL(int magic, datetime fromTime)
  {
   double p = 0;
   datetime from = (fromTime > 0) ? fromTime : (TimeCurrent() - 86400);  // callers pass the exact window start
   if(!HistorySelect(from, TimeCurrent() + 60)) return 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      if(HistoryDealGetString(d, DEAL_SYMBOL) != g_sym) continue;
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != magic) continue;
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN)
        {
         // brokers that charge commission per side book half of it on the entry deal
         p += HistoryDealGetDouble(d, DEAL_COMMISSION);
         continue;
        }
      if(entry != DEAL_ENTRY_OUT) continue;
      p += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP)
         + HistoryDealGetDouble(d, DEAL_COMMISSION);
     }
   return p;
  }

//--- Close every position of one magic. Returns the number that could NOT be closed.
int CloseAll(int magic)
  {
   int fails = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;

      MqlTradeRequest r = {};
      MqlTradeResult  x = {};
      r.action       = TRADE_ACTION_DEAL;
      r.symbol       = g_sym;
      r.position     = t;
      r.volume       = PositionGetDouble(POSITION_VOLUME);
      r.deviation    = 30;
      r.magic        = magic;
      r.type_filling = FillPolicy();

      long pt = PositionGetInteger(POSITION_TYPE);
      r.type  = (pt == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;

      bool ok = false;
      for(int a = 0; a < 3 && !ok; a++)
        {
         r.price = (pt == POSITION_TYPE_BUY) ? SymbolInfoDouble(g_sym, SYMBOL_BID)
                                              : SymbolInfoDouble(g_sym, SYMBOL_ASK);
         ResetLastError();
         if(OrderSend(r, x) && (x.retcode == TRADE_RETCODE_DONE || x.retcode == TRADE_RETCODE_PLACED))
           {
            ok = true;
            break;
           }
         if(x.retcode == TRADE_RETCODE_INVALID_FILL)
           {
            if(r.type_filling == ORDER_FILLING_FOK)      r.type_filling = ORDER_FILLING_IOC;
            else if(r.type_filling == ORDER_FILLING_IOC) r.type_filling = ORDER_FILLING_RETURN;
            else                                         r.type_filling = ORDER_FILLING_FOK;
           }
         else if(!MQLInfoInteger(MQL_TESTER))
            Sleep(100);
        }
      if(!ok)
        {
         fails++;
         PrintFormat("  CLOSE FAIL pos=%I64u ret=%d [%s] err=%d", t, x.retcode, x.comment, GetLastError());
        }
     }
   return fails;
  }

//--- RefCapital: >0 fixed, 0 = balance at init, <0 = current balance (DD% scales with the account)
void UpdateRefCapital(bool atInit)
  {
   if(RefCapital > 0)
      g_refCapital = RefCapital;
   else if(RefCapital < 0 || atInit)
      g_refCapital = AccountInfoDouble(ACCOUNT_BALANCE);
   if(g_refCapital <= 0) g_refCapital = 1; // guard against /0
  }

//--- Re-sync a grid whose positions disagree on TP or carry no TP at all.
//    A healthy grid (all TPs equal) is never touched, so the v6 TP logic is unchanged.
void HealTP(ENUM_ORDER_TYPE type, int magic, double wavg, int count)
  {
   if(count <= 0 || wavg <= 0) return;
   double tpMin = DBL_MAX, tpMax = -DBL_MAX;
   bool   missing = false;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      double tp = PositionGetDouble(POSITION_TP);
      if(tp <= 0) { missing = true; continue; }
      tpMin = MathMin(tpMin, tp);
      tpMax = MathMax(tpMax, tp);
     }
   if(!missing && tpMax - tpMin < 0.5 * MathMax(g_tickSize, g_point)) return;

   double tp = (type == ORDER_TYPE_BUY) ? ND(wavg + TP_Pips * 10.0 * g_point)
                                        : ND(wavg - TP_Pips * 10.0 * g_point);
   tp = ClampTP(tp, type);
   PrintFormat(">>> TP HEAL %s: %d pos, missing=%s spread=%.5f -> TP=%.5f",
               type == ORDER_TYPE_BUY ? "BUY" : "SELL", count, missing ? "Y" : "N",
               missing ? 0.0 : tpMax - tpMin, tp);
   SetTP(magic, tp);
  }

//--- Start-up risk report: what the full ladder costs on THIS account.
void RiskPreflight()
  {
   double tv = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_SIZE);
   if(tv <= 0 || ts <= 0) return;
   double perPt = tv * g_point / ts;           // $ per point per 1.0 lot
   double lots = 0, lossAtLast = 0;
   for(int k = 1; k <= MaxLayers; k++)
     {
      double lot = LotSize(k);
      lots       += lot;
      lossAtLast += lot * (MaxLayers - k) * GridSpacingPts * perPt;
     }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double perPip = lots * 10.0 * perPt;
   string ccy = AccountInfoString(ACCOUNT_CURRENCY);   // "USC" on cent accounts
   PrintFormat("PREFLIGHT: full ladder %.2f lots/side over %.1f pips | float at last fill %.2f %s (%.0f%% of bal) | then %.2f %s per pip",
               lots, (MaxLayers - 1) * GridSpacingPts / 10.0, lossAtLast, ccy,
               bal > 0 ? lossAtLast / bal * 100.0 : 0, perPip, ccy);
   if(EmergencyCloseDD_Pct > 0)
      PrintFormat("PREFLIGHT: basket stop closes at -%.2f %s grid float (%.1f%% of RefCap %.2f)",
                  EmergencyCloseDD_Pct / 100.0 * g_refCapital, ccy, EmergencyCloseDD_Pct, g_refCapital);
   else if(bal > 0 && lossAtLast > 0.5 * bal)
      PrintFormat("!!! PREFLIGHT WARNING: no basket stop and a full ladder costs %.0f%% of balance. "
                  "Consider EmergencyCloseDD_Pct > 0 or a smaller ladder.", lossAtLast / bal * 100.0);
  }

//+------------------------------------------------------------------+
//| v7 HIGH-FREQUENCY GRID HELPERS                                    |
//+------------------------------------------------------------------+
//--- balance + this EA's open float (both magics): the equity the v7 guards use
double GridEquity()
  {
   return AccountInfoDouble(ACCOUNT_BALANCE) + FloatPnL(MagicBuy) + FloatPnL(MagicSell);
  }

//--- oldest/newest position time (ms) and the layer-1 (oldest) fill of one grid, read live
bool GridTimes(int magic, long &oldestMs, long &newestMs, double &firstPx)
  {
   oldestMs = 0; newestMs = 0; firstPx = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      long ms = PositionGetInteger(POSITION_TIME_MSC);
      if(oldestMs == 0 || ms < oldestMs)
        {
         oldestMs = ms;
         firstPx  = PositionGetDouble(POSITION_PRICE_OPEN);
        }
      if(ms > newestMs) newestMs = ms;
     }
   return oldestMs > 0;
  }

//--- virtual basket stop: BUY when bid <= L1 fill - StopPips; SELL when ask >= L1 fill + StopPips
double StopPrice(ENUM_ORDER_TYPE type, double firstPx)
  {
   double d = StopPips * 10.0 * g_point;
   return ND(type == ORDER_TYPE_BUY ? firstPx - d : firstPx + d);
  }

//--- broker-side SL (disconnect insurance) sits BrokerSLBufferPips beyond the virtual stop
double BrokerSL(ENUM_ORDER_TYPE type, double firstPx)
  {
   double d = (StopPips + BrokerSLBufferPips) * 10.0 * g_point;
   return ND(type == ORDER_TYPE_BUY ? firstPx - d : firstPx + d);
  }

//--- pre-trade ladder fit: deepest ladder whose loss at the L1-anchored stop fits the budget
//    (same arithmetic as the simulator's _fit_layers)
int FitLayers(double equity)
  {
   if(StopPips <= 0) return MaxLayers;
   double tv = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(g_sym, SYMBOL_TRADE_TICK_SIZE);
   if(tv <= 0 || ts <= 0) return 0;
   double perPrice = tv / ts;                  // money per 1.0 price unit per 1.0 lot
   double stopD    = StopPips * 10.0 * g_point;
   double step     = GridSpacingPts * g_point;
   double slip     = RiskSlipPts * g_point;
   double budget   = (MaxBasketRiskPct > 0) ? MaxBasketRiskPct / 100.0 * equity : DBL_MAX;
   double worst    = 0;
   int    n        = 0;
   double spr = SymbolInfoInteger(g_sym, SYMBOL_SPREAD) * g_point;
   for(int k = 1; k <= MaxLayers; k++)
     {
      double dist = stopD - (k - 1) * (step - slip);   // layer k fill (ask) to the stop (bid), incl. add slippage
      if(k > 1 && dist <= spr + slip) break;          // trigger not reachable before the stop
      if(dist <= 0) break;
      double lot = LotSize(k);
      double add = lot * perPrice * (dist + slip) + lot * RiskCommPerLot;
      if(worst + add > budget) break;
      worst += add;
      n = k;
     }
   return n;
  }

//--- per-side anchor (layer-1 fill + time) and flatten latch, persisted in GlobalVariables
string SideSfx(int k) { return (k == 0) ? "B" : "S"; }

void SetAnchor(int k, double px, long ms)
  {
   g_anchorPx[k] = px;
   g_anchorMs[k] = ms;
   GlobalVariableSet(GvName("anc" + SideSfx(k) + "px"), px);
   GlobalVariableSet(GvName("anc" + SideSfx(k) + "ms"), (double)ms);
  }

//--- the layer-1 anchor; recovered from the oldest live ticket only if it was never recorded
void SideAnchor(int k, double &px, long &ms)
  {
   if(g_anchorPx[k] > 0)
     {
      px = g_anchorPx[k];
      ms = g_anchorMs[k];
      return;
     }
   long oMs, nMs;
   double fPx;
   if(GridTimes(k == 0 ? MagicBuy : MagicSell, oMs, nMs, fPx))
     {
      SetAnchor(k, fPx, oMs);
      px = fPx;
      ms = oMs;
      return;
     }
   px = 0;
   ms = 0;
  }

void LatchSide(int k)
  {
   if(g_sideFlat[k]) return;
   g_sideFlat[k] = true;
   GlobalVariableSet(GvName("flat" + SideSfx(k)), 1);
   GlobalVariablesFlush();
  }

//--- side is flat: forget its anchor, latch and fitted ladder
void ClearSide(int k)
  {
   g_anchorPx[k] = 0;
   g_anchorMs[k] = 0;
   g_sideFlat[k] = false;
   g_closeBackoff[k] = 0;
   g_nextCloseTry[k] = 0;
   string names[4];
   names[0] = "anc" + SideSfx(k) + "px";
   names[1] = "anc" + SideSfx(k) + "ms";
   names[2] = "flat" + SideSfx(k);
   names[3] = "fit" + SideSfx(k);
   for(int i = 0; i < 4; i++)
      if(GlobalVariableCheck(GvName(names[i]))) GlobalVariableDel(GvName(names[i]));
  }

//--- close one side with exponential backoff (2..30 s) after failures; -1 = waiting / trading off
int CloseSide(int k)
  {
   if(TimeCurrent() < g_nextCloseTry[k]) return -1;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED)
      || SymbolInfoInteger(g_sym, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED)
     {
      g_nextCloseTry[k] = TimeCurrent() + 5;
      return -1;
     }
   int f = CloseAll(k == 0 ? MagicBuy : MagicSell);
   if(f > 0)
     {
      g_closeBackoff[k] = (int)MathMin(MathMax(2, g_closeBackoff[k] * 2), 30);
      g_nextCloseTry[k] = TimeCurrent() + g_closeBackoff[k];
     }
   else
      g_closeBackoff[k] = 0;
   return f;
  }

//--- per-grid exits every tick. Once a stop, time or probability exit fires, the side is LATCHED: it
//    keeps closing (with backoff) and cannot re-anchor, add layers or loosen its SL until it is flat.
//    v7.02: the probability exit runs only on the first tick of each new M1 bar, after the stops.
void V7GridExits()
  {
   MqlTick tk;
   if(!SymbolInfoTick(g_sym, tk)) return;
   datetime m1Bar    = (ProbExitMode != PROB_OFF) ? iTime(g_sym, PERIOD_M1, 0) : (datetime)0;
   bool     probBar  = (m1Bar > 0 && m1Bar != g_probLastBar);
   int      probFeat = -1;                       // features this bar: -1 = not computed, 0 = unavailable, 1 = ok
   double   pz = 0, ptr = 0, plv = 0;
   for(int k = 0; k < 2; k++)
     {
      int magic = (k == 0) ? MagicBuy : MagicSell;
      ENUM_ORDER_TYPE type = (k == 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      if(PosCount(magic) == 0)
        {
         if(g_sideFlat[k] || g_anchorPx[k] > 0) ClearSide(k);
         continue;
        }
      if(g_sideFlat[k])
        {
         CloseSide(k);
         continue;
        }
      if(StopPips <= 0 && MaxHoldSec <= 0) continue;
      double ancPx;
      long   ancMs;
      SideAnchor(k, ancPx, ancMs);
      if(ancPx <= 0) continue;
      if(StopPips > 0)
        {
         double sp = StopPrice(type, ancPx);
         bool hit = (k == 0) ? (tk.bid <= sp) : (tk.ask >= sp);
         if(hit)
           {
            PrintFormat(">>> BASKET STOP %s: L1=%.5f stop=%.5f px=%.5f float=%.2f", k == 0 ? "BUY" : "SELL",
                        ancPx, sp, k == 0 ? tk.bid : tk.ask, FloatPnL(magic));
            LatchSide(k);
            CloseSide(k);
            continue;
           }
        }
      if(MaxHoldSec > 0 && tk.time_msc - ancMs >= (long)MaxHoldSec * 1000)
        {
         PrintFormat(">>> TIME STOP %s: age %.0fs >= %ds float=%.2f", k == 0 ? "BUY" : "SELL",
                     (tk.time_msc - ancMs) / 1000.0, MaxHoldSec, FloatPnL(magic));
         LatchSide(k);
         CloseSide(k);
         continue;
        }
      if(probBar)
        {
         if(probFeat < 0)                        // once per bar, shared by both sides
           {
            probFeat = ProbFeatures(pz, ptr, plv) ? 1 : 0;
            if(probFeat == 0 && !g_probFeatWarned)      // once per run, regardless of DebugLog
              {
               g_probFeatWarned = true;
               PrintFormat("!!! WARNING: probability exit INACTIVE: M1 features unavailable (fewer than %d closed M1 bars "
                           "available: %d bars, 'Max bars in chart' %d; or zero volatility) -> no probability exit "
                           "until enough M1 history is loaded. (Printed once per run.)",
                           (int)MathMax(1200, 6 * ProbVolLongBars), Bars(g_sym, PERIOD_M1),
                           (int)TerminalInfoInteger(TERMINAL_MAXBARS));
              }
            if(probFeat == 0 && DebugLog)
               Print("  PROB EXIT: M1 features unavailable (short history or zero volatility) -> no probability exit this bar");
           }
         if(probFeat == 1)
            ProbExitSide(k, type, magic, ancPx, ancMs, tk.bid, tk.ask, tk.time_msc, pz, ptr, plv);
        }
     }
   if(probBar) g_probLastBar = m1Bar;
  }

//--- the grid's shared TP: POSITION_TP of its first position that carries one (0 = none)
double GridTP(int magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      double tp = PositionGetDouble(POSITION_TP);
      if(tp > 0) return tp;
     }
   return 0;
  }

//--- v7.02 probability-exit features on CLOSED M1 bars (StretchSignal conventions), cached per M1 bar:
//    z     = (close - EMA(ZEmaBars)) / (sigma1 x sqrt15)     sigma1 = EWMA std of M1 changes, half-life 60
//    trend = (close - close[60]) / (sigma1 x sqrt60)          SIGNED (StretchSignal uses |.|)
//    lvol  = ln(sigma1 / sigmaLong)                           sigmaLong = same EWMA, half-life ProbVolLongBars
//    Both EWMA variances use the pandas adjust=True normalisation over the copied window (no seed).
//    false = not enough M1 history or zero volatility.
bool ProbFeatures(double &z, double &trend, double &lvol)
  {
   datetime bar = iTime(g_sym, PERIOD_M1, 0);
   if(bar > 0 && bar == g_probFeatBar)
     {
      z     = g_probZ;
      trend = g_probTrend;
      lvol  = g_probLVol;
      return true;
     }
   z = 0; trend = 0; lvol = 0;
   int need = (int)MathMax(1200, 6 * ProbVolLongBars);
   double c[];
   ArraySetAsSeries(c, true);
   if(CopyClose(g_sym, PERIOD_M1, 1, need, c) < need) return false;   // c[0] = last CLOSED bar
   double pip  = 10.0 * g_point;
   double a1   = 1.0 - MathPow(0.5, 1.0 / 60.0);
   double aL   = 1.0 - MathPow(0.5, 1.0 / (double)ProbVolLongBars);
   //--- pandas ewm(halflife, adjust=True) over the window: var = sum w_i r_i^2 / sum w_i, w_i = (1-a)^age
   double num1 = 0, den1 = 0, numL = 0, denL = 0;
   for(int i = need - 2; i >= 0; i--)                        // oldest -> newest
     {
      double r  = (c[i] - c[i + 1]) / pip;
      double r2 = r * r;
      num1 = (1.0 - a1) * num1 + r2;
      den1 = (1.0 - a1) * den1 + 1.0;
      numL = (1.0 - aL) * numL + r2;
      denL = (1.0 - aL) * denL + 1.0;
     }
   double var1 = (den1 > 0) ? num1 / den1 : 0.0;
   double varL = (denL > 0) ? numL / denL : 0.0;
   double sig1 = MathSqrt(var1);
   double sigL = MathSqrt(varL);
   if(sig1 <= 0 || sigL <= 0) return false;
   double alpha = 2.0 / (ZEmaBars + 1.0);
   double ema   = c[need - 1];
   for(int i = need - 2; i >= 0; i--)
      ema = alpha * c[i] + (1.0 - alpha) * ema;
   z     = (c[0] - ema) / pip / (sig1 * MathSqrt(15.0));
   trend = (c[0] - c[60]) / pip / (sig1 * MathSqrt(60.0));
   lvol  = MathLog(sig1 / sigL);
   g_probZ       = z;
   g_probTrend   = trend;
   g_probLVol    = lvol;
   g_probFeatBar = bar;
   return true;
  }

//--- v7.02: random-walk P(the shared TP is reached before the basket stop) INCLUDING the grid's own
//    future adds; mirrors research/bgrid_bt.py _p_rw. BUY: between the TP (above) and the next add
//    trigger A (below) a driftless bid hits the TP first with probability (x-A)/(TP-A); reaching A
//    adds the next layer (fill = trigger + RiskSlipPts slippage), which re-sets the TP from the new
//    average (the add path's ND(wavg + TP)), and so on until the ladder is full (fitted depth as in
//    TryAddLayer, MaxTotalLots on both grids), after which the basket stop is the lower barrier.
//    SELL is the mirror image (ask, TP below, adds and stop above). The spread is held at its
//    current value; TryAddLayer's spread / pacing / news gates and ClampTP are not modelled (as in
//    the simulator). Returns -1 when the grid cannot be read.
double ProbRandomWalk(int k, ENUM_ORDER_TYPE type, int magic, double bid, double ask, double barrier, double tp)
  {
   double wavg = 0, last = 0, lots = 0;
   int    n = 0;
   ReadGrid(magic, wavg, last, lots, n);                     // last = TryAddLayer's lastPx (newest position)
   if(n <= 0 || lots <= 0 || last <= 0) return -1.0;
   bool   buy   = (type == ORDER_TYPE_BUY);
   int    side  = buy ? 0 : 1;                                // == k
   double pv    = wavg * lots;                                // sum(price_open x volume)
   double other = TotalLots() - lots;                         // the opposite grid's lots (TryAddLayer's cap)
   int    effml = (g_effMaxLayers[side] > 0) ? (int)MathMin(g_effMaxLayers[side], MaxLayers) : MaxLayers;
   double hs    = (ask - bid) / 2.0;
   double step  = GridSpacingPts * g_point;
   double slip  = RiskSlipPts * g_point;
   double tpd   = TP_Pips * 10.0 * g_point;
   double x     = buy ? bid : ask;
   double res   = 0.0;
   double reach = 1.0;
   for(int it = 0; it < 64; it++)
     {
      if((buy && x >= tp) || (!buy && x <= tp))
        {
         res += reach;
         break;
        }
      double lot    = LotSize(n + 1);
      bool   canAdd = (n < effml && other + lots + lot <= MaxTotalLots + 1e-9);
      double a = 0.0, f = 0.0, d = 0.0;
      if(buy)
        {
         a = last - step - 2.0 * hs;                           // bid when the ask reaches the next add level
         if(!(canAdd && a > barrier)) { a = barrier; canAdd = false; }
         d = tp - a;
         f = (x <= a) ? 0.0 : ((d > 0) ? (x - a) / d : 1.0);
        }
      else
        {
         a = last + step + 2.0 * hs;                           // ask when the bid reaches the next add level
         if(!(canAdd && a < barrier)) { a = barrier; canAdd = false; }
         d = a - tp;
         f = (x >= a) ? 0.0 : ((d > 0) ? (a - x) / d : 1.0);
        }
      res   += reach * f;
      reach *= (1.0 - f);
      if(!canAdd || reach <= 1e-12) break;
      double fill = buy ? (last - step + slip) : (last + step - slip);
      pv   += fill * lot;
      lots += lot;
      last  = fill;
      n++;
      double w = pv / lots;
      tp = buy ? ND(w + tpd) : ND(w - tpd);                    // TryAddLayer: ND(newWavg +/- TP_Pips x 10 x point)
      x  = a;
     }
   return res;
  }

//--- v7.02: P(the grid's shared TP is reached before its basket stop), logistic around the
//    random-walk probability Pf = ProbRandomWalk (with the grid's own future adds).
//    FLOOR: P < ProbMin; EDGE: logit(P) - logit(Pf) < -ProbEdge.
//    On exit the side is latched and closed like a basket stop. Returns true when it fired.
bool ProbExitSide(int k, ENUM_ORDER_TYPE type, int magic, double ancPx, long ancMs,
                  double bid, double ask, long nowMs, double z, double trend, double lvol)
  {
   double tp = GridTP(magic);
   if(tp <= 0) return false;                                  // no shared TP yet (HealTP restores it)
   double barrier = StopPrice(type, ancPx);                  // BUY: bid level below; SELL: ask level above
   double s = (k == 0) ? 1.0 : -1.0;
   double x = 0, u = 0, v = 0;
   if(k == 0) { x = bid; u = tp - x; v = x - barrier; }     // BUY grid: TP above the bid, stop below
   else       { x = ask; u = x - tp; v = barrier - x; }     // SELL grid: TP below the ask, stop above
   if(u <= 0 || v <= 0) return false;                        // TP or the basket stop is executing
   double pf  = ProbRandomWalk(k, type, magic, bid, ask, barrier, tp);
   if(pf < 0) return false;                                  // grid unreadable this tick
   pf         = MathMax(1e-6, MathMin(1.0 - 1e-6, pf));
   double lf  = MathLog(pf / (1.0 - pf));
   double zs  = -s * z;                                      // stretch in favour of the grid
   double ts  = s * trend;                                   // trend in favour of the grid
   double age = MathLog(1.0 + MathMax(0.0, (double)(nowMs - ancMs)) / 60000.0);
   double nl  = (double)(PosCount(magic) - 1);
   double L   = lf + ProbB0 + ProbBZ * zs + ProbBTrend * ts + ProbBVol * lvol + ProbBAge * age + ProbBLayers * nl;
   double P   = 1.0 / (1.0 + MathExp(-L));
   bool floorHit = ((ProbExitMode == PROB_FLOOR || ProbExitMode == PROB_BOTH) && P < ProbMin);
   bool edgeHit  = ((ProbExitMode == PROB_EDGE  || ProbExitMode == PROB_BOTH) && (L - lf) < -ProbEdge);
   if(!floorHit && !edgeHit) return false;
   double pip = 10.0 * g_point;
   PrintFormat(">>> PROB EXIT %s: P=%.3f fair=%.3f dlogit=%.3f u=%.1fp v=%.1fp z=%.2f tr=%.2f lv=%.2f float=%.2f",
               k == 0 ? "BUY" : "SELL", P, pf, L - lf, u / pip, v / pip, z, trend, lvol, FloatPnL(magic));
   LatchSide(k);
   CloseSide(k);
   return true;
  }

//--- account guards: daily loss (state persisted per server day), peak kill latch, session-end
//    flatten. Returns false while the EA must stay flat for the rest of this tick.
bool V7AccountGuards()
  {
   double eq = GridEquity();
   int nOpen = PosCount(MagicBuy) + PosCount(MagicSell);
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   datetime day = TimeCurrent() - (dt.hour * 3600 + dt.min * 60 + dt.sec);
   if(day != g_dayStart)
     {
      if(GlobalVariableCheck(GvName("dayStart")) && (datetime)(long)GlobalVariableGet(GvName("dayStart")) == day)
        {
         g_dayBal  = GlobalVariableGet(GvName("dayBal"));          // restart inside the same day
         g_dayHalt = GlobalVariableGet(GvName("dayHalt")) > 0.5;
         if(g_dayHalt) g_dayHaltDay = day;
        }
      else
        {
         g_dayBal = AccountInfoDouble(ACCOUNT_BALANCE);
         if(!(g_dayHalt && nOpen > 0)) g_dayHalt = false;          // a still-flattening halt carries over
         GlobalVariableSet(GvName("dayStart"), (double)(long)day);
         GlobalVariableSet(GvName("dayBal"), g_dayBal);
         GlobalVariableSet(GvName("dayHalt"), g_dayHalt ? 1 : 0);
        }
      g_dayStart = day;
     }
   if(g_dayHalt && nOpen == 0 && g_dayHaltDay != g_dayStart)
     {
      g_dayHalt = false;
      GlobalVariableSet(GvName("dayHalt"), 0);
     }

   if(PeakKillPct > 0)
     {
      if(eq > g_eqPeak) g_eqPeak = eq;
      if(g_eqPeak > g_savedPeak * 1.005)
        {
         GlobalVariableSet(GvName("eqpeak"), g_eqPeak);
         g_savedPeak = g_eqPeak;
        }
      if(!g_kill && g_eqPeak > 0 && eq <= g_eqPeak * (1.0 - PeakKillPct / 100.0))
        {
         g_kill = true;
         GlobalVariableSet(GvName("kill"), 1);
         GlobalVariablesFlush();
         PrintFormat("!!! KILL LATCH: equity %.2f <= %.1f%% below peak %.2f. Flattening; set ResetKillLatch=true after review.",
                     eq, PeakKillPct, g_eqPeak);
        }
     }
   if(g_kill)
     {
      if(nOpen > 0) { CloseSide(0); CloseSide(1); }
      return false;
     }
   if(DailyLossPct > 0 && !g_dayHalt && g_dayBal > 0 && eq - g_dayBal <= -DailyLossPct / 100.0 * g_dayBal)
     {
      g_dayHalt    = true;
      g_dayHaltDay = g_dayStart;
      GlobalVariableSet(GvName("dayHalt"), 1);
      GlobalVariablesFlush();
      PrintFormat("!!! DAILY LOSS LIMIT: equity %.2f vs day start %.2f (-%.1f%%). Flat until the next server day.",
                  eq, g_dayBal, DailyLossPct);
     }
   if(g_dayHalt && nOpen > 0)
     {
      CloseSide(0);                    // keep flattening until flat (a failed close never keeps averaging)
      CloseSide(1);
      return false;
     }
   if(CloseAtSessionEnd && !InSession() && nOpen > 0)
     {
      if(DebugLog) Print(">>> SESSION END: flattening EA grids");
      CloseSide(0);
      CloseSide(1);
      return false;
     }
   return true;
  }

//--- "HH:MM" -> minutes after server midnight, -1 if empty/invalid
int ParseHHMM(string v)
  {
   StringTrimLeft(v);
   StringTrimRight(v);
   if(StringLen(v) == 0) return -1;
   string parts[];
   if(StringSplit(v, ':', parts) != 2) return -1;
   int hh = (int)StringToInteger(parts[0]);
   int mm = (int)StringToInteger(parts[1]);
   if(hh < 0 || hh > 23 || mm < 0 || mm > 59) return -1;
   return hh * 60 + mm;
  }

bool InSession()
  {
   if(g_sessStart < 0 || g_sessEnd < 0 || g_sessStart == g_sessEnd) return true;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int mod = dt.hour * 60 + dt.min;
   if(g_sessStart < g_sessEnd) return (mod >= g_sessStart && mod < g_sessEnd);
   return (mod >= g_sessStart || mod < g_sessEnd);
  }

//--- STRETCH entry on CLOSED M1 bars (mirrors the simulator's stretch_signal):
//    z = (close - EMA(ZEmaBars)) / sigma15, sigma15 = EWMA(half-life 60) std of M1 changes x sqrt(15)
//    +1 = open BUY grid, -1 = open SELL grid, 0 = nothing
int StretchSignal()
  {
   const int need = 1200;
   double c[], o[], h[], l[];
   ArraySetAsSeries(c, true);
   ArraySetAsSeries(o, true);
   ArraySetAsSeries(h, true);
   ArraySetAsSeries(l, true);
   if(CopyClose(g_sym, PERIOD_M1, 1, need, c) < need) return 0;   // c[0] = last CLOSED bar
   if(CopyOpen(g_sym, PERIOD_M1, 1, 2, o) < 2) return 0;
   if(CopyHigh(g_sym, PERIOD_M1, 1, 2, h) < 2) return 0;
   if(CopyLow(g_sym, PERIOD_M1, 1, 2, l) < 2) return 0;
   double pip = 10.0 * g_point;
   double a = 1.0 - MathPow(0.5, 1.0 / 60.0);
   double var = 0;
   bool   init = false;
   for(int i = need - 2; i >= 0; i--)
     {
      double r = (c[i] - c[i + 1]) / pip;
      if(!init) { var = r * r; init = true; }
      else        var = (1.0 - a) * var + a * r * r;
     }
   double sig1 = MathSqrt(var);
   if(sig1 <= 0) return 0;
   double alpha = 2.0 / (ZEmaBars + 1.0);
   double ema = c[need - 1];
   for(int i = need - 2; i >= 0; i--)
      ema = alpha * c[i] + (1.0 - alpha) * ema;
   double z     = (c[0] - ema) / pip / (sig1 * MathSqrt(15.0));
   double trend = MathAbs((c[0] - c[60]) / pip) / (sig1 * MathSqrt(60.0));
   if(trend >= TrendTMax) return 0;
   if(z <= -ZEntry && c[0] > o[0] && c[0] > l[1]) return 1;
   if(z >=  ZEntry && c[0] < o[0] && c[0] < h[1]) return -1;
   return 0;
  }

//--- make sure every ticket of a grid carries the broker-side SL (after fallbacks/rejects).
//    The SL comes from the persisted layer-1 anchor and is never moved further away.
void EnsureSL(ENUM_ORDER_TYPE type, int magic)
  {
   if(StopPips <= 0) return;
   int k = (type == ORDER_TYPE_BUY) ? 0 : 1;
   if(PosCount(magic) == 0) return;
   double ancPx;
   long   ancMs;
   SideAnchor(k, ancPx, ancMs);
   if(ancPx <= 0) return;
   double sl  = BrokerSL(type, ancPx);
   double tol = 0.5 * MathMax(g_tickSize, g_point);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      double cur = PositionGetDouble(POSITION_SL);
      if(MathAbs(cur - sl) < tol) continue;
      if(cur > 0 && ((type == ORDER_TYPE_BUY && sl < cur - tol) || (type == ORDER_TYPE_SELL && sl > cur + tol)))
         continue;                                            // never loosen an existing SL
      MqlTradeRequest req = {};
      MqlTradeResult  res = {};
      req.action   = TRADE_ACTION_SLTP;
      req.symbol   = g_sym;
      req.position = ticket;
      req.sl       = sl;
      req.tp       = PositionGetDouble(POSITION_TP);
      ResetLastError();
      if(!OrderSend(req, res) && DebugLog)
         PrintFormat("  SL SET FAIL: pos=%I64u ret=%d err=%d sl=%.5f", ticket, res.retcode, GetLastError(), sl);
     }
  }

//+------------------------------------------------------------------+
//| NEWS FILTER                                                       |
//+------------------------------------------------------------------+
void NewsSetupCurrencies()
  {
   string list = NewsCurrencies;
   if(StringLen(list) == 0)
      list = SymbolInfoString(g_sym, SYMBOL_CURRENCY_BASE) + "," + SymbolInfoString(g_sym, SYMBOL_CURRENCY_PROFIT);
   string parts[];
   int n = StringSplit(list, ',', parts);
   ArrayResize(g_newsCcys, 0);
   for(int i = 0; i < n; i++)
     {
      string c = parts[i];
      StringTrimLeft(c);
      StringTrimRight(c);
      StringToUpper(c);
      if(StringLen(c) != 3 || NewsCcyWanted(c)) continue;
      int k = ArraySize(g_newsCcys);
      ArrayResize(g_newsCcys, k + 1);
      g_newsCcys[k] = c;
     }
  }

bool NewsCcyWanted(string c)
  {
   for(int i = 0; i < ArraySize(g_newsCcys); i++)
      if(g_newsCcys[i] == c) return true;
   return false;
  }

string NewsCcyList()
  {
   string s = "";
   for(int i = 0; i < ArraySize(g_newsCcys); i++)
      s += (i > 0 ? "," : "") + g_newsCcys[i];
   return s;
  }

int NewsImpFromStr(string s)
  {
   StringTrimLeft(s);
   StringTrimRight(s);
   StringToUpper(s);
   if(s == "HIGH" || s == "3")                     return 3;
   if(s == "MEDIUM" || s == "MODERATE" || s == "2") return 2;
   if(s == "LOW" || s == "1")                      return 1;
   return 0;
  }

void NewsClear()
  {
   g_newsCount = 0;
   g_newsIdx   = 0;
   ArrayResize(g_newsTime, 0);
   ArrayResize(g_newsTitle, 0);
   ArrayResize(g_newsCur, 0);
  }

//--- insert keeping ascending time order (input is usually already sorted -> O(1))
void NewsAdd(datetime t, string cur, string title)
  {
   int n = g_newsCount;
   ArrayResize(g_newsTime,  n + 1, 512);
   ArrayResize(g_newsTitle, n + 1, 512);
   ArrayResize(g_newsCur,   n + 1, 512);
   int j = n;
   while(j > 0 && g_newsTime[j - 1] > t)
     {
      g_newsTime[j]  = g_newsTime[j - 1];
      g_newsTitle[j] = g_newsTitle[j - 1];
      g_newsCur[j]   = g_newsCur[j - 1];
      j--;
     }
   g_newsTime[j]  = t;
   g_newsTitle[j] = title;
   g_newsCur[j]   = cur;
   g_newsCount    = n + 1;
  }

//--- CSV: "YYYY.MM.DD HH:MI,CUR,IMPORTANCE,TITLE" in NY-close server time ('#' = comment)
int NewsLoadCsv()
  {
   int h = FileOpen(NewsCsvFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      h = FileOpen(NewsCsvFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return -1;
   NewsClear();
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      if(StringLen(line) < 16 || StringGetCharacter(line, 0) == '#') continue;
      string f[];
      if(StringSplit(line, ',', f) < 3) continue;
      string cur = f[1];
      StringTrimLeft(cur);
      StringTrimRight(cur);
      StringToUpper(cur);
      if(!NewsCcyWanted(cur)) continue;
      if(NewsImpFromStr(f[2]) < (int)NewsMinImportance) continue;
      datetime t = StringToTime(f[0]);
      if(t <= 0) continue;
      NewsAdd(t + NewsCsvShiftHours * 3600, cur, ArraySize(f) > 3 ? f[3] : "event");
     }
   FileClose(h);
   g_newsFromCsv = true;
   return g_newsCount;
  }

//--- Live MQL5 economic calendar (times are already trade-server time)
int NewsLoadCalendar()
  {
   datetime now  = TimeTradeServer();
   datetime from = now - 6 * 3600;
   datetime to   = now + 3 * 86400;
   int ok = 0;
   NewsClear();
   for(int c = 0; c < ArraySize(g_newsCcys); c++)
     {
      MqlCalendarValue vals[];
      ResetLastError();
      if(!CalendarValueHistory(vals, from, to, NULL, g_newsCcys[c]))
        {
         if(DebugLog) PrintFormat("  news: calendar query %s failed err=%d", g_newsCcys[c], GetLastError());
         continue;
        }
      ok++;
      for(int i = 0; i < ArraySize(vals); i++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(vals[i].event_id, ev)) continue;
         if((int)ev.importance < (int)NewsMinImportance) continue;
         if(ev.time_mode != CALENDAR_TIMEMODE_DATETIME) continue;
         NewsAdd(vals[i].time, g_newsCcys[c], ev.name);
        }
     }
   if(ok == 0) return -1;
   g_newsFromCsv = false;
   return g_newsCount;
  }

//--- Load once in the tester (CSV), refresh every 30 min live (calendar, CSV fallback)
void NewsRefresh(bool force)
  {
   static bool s_warned = false;
   bool tester = (MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION));
   if(tester)
     {
      if(!force && g_newsLoadedAt > 0) return;
      g_newsLoadedAt = TimeCurrent();
      if(NewsLoadCsv() < 0 && !s_warned)
        {
         s_warned = true;
         PrintFormat("!!! NEWS: '%s' not found in MQL5\\Files or Common\\Files -> news filter INACTIVE in tester",
                     NewsCsvFile);
        }
      return;
     }
   if(!force && g_newsLoadedAt > 0 && TimeCurrent() - g_newsLoadedAt < 30 * 60) return;
   g_newsLoadedAt = TimeCurrent();
   int n = NewsLoadCalendar();
   if(n < 0)
     {
      n = NewsLoadCsv();
      if(n < 0 && !s_warned)
        {
         s_warned = true;
         Print("!!! NEWS: economic calendar unavailable and no CSV fallback -> news filter INACTIVE");
        }
      else if(n >= 0)
         PrintFormat("  news: calendar unavailable, using CSV fallback (%d events)", n);
     }
   else if(DebugLog)
      PrintFormat("  news: %d upcoming high-impact event(s) for %s", n, NewsCcyList());
  }

//--- Sets g_newsBlock / g_newsPre / g_newsNextT / g_newsNextName for time 'now'
void NewsEval(datetime now)
  {
   g_newsBlock = false;
   g_newsPre   = false;
   g_newsNextT = 0;
   g_newsNextName = "";
   if(g_newsCount == 0) return;

   int before = NewsBeforeMin * 60;
   if(NewsCloseMode != NEWS_CLOSE_NONE && NewsCloseMin > NewsBeforeMin)
      before = NewsCloseMin * 60;   // never re-open a grid between the pre-close and the event
   int after = NewsAfterMin * 60;

   while(g_newsIdx < g_newsCount && g_newsTime[g_newsIdx] + after < now)
      g_newsIdx++;
   for(int j = g_newsIdx; j < g_newsCount; j++)
     {
      datetime t = g_newsTime[j];
      if(g_newsNextT == 0)
        {
         g_newsNextT    = t;
         g_newsNextName = g_newsCur[j] + " " + g_newsTitle[j];
        }
      if(t - before > now) break;
      if(now >= t - before && now <= t + after)
         g_newsBlock = true;
      if(NewsCloseMode != NEWS_CLOSE_NONE && now >= t - NewsCloseMin * 60 && now < t)
         g_newsPre = true;
     }
  }

string NewsPanelStr()
  {
   if(!UseNewsFilter) return "OFF";
   if(g_newsCount == 0) return "ON (no events loaded)";
   if(g_newsNextT == 0) return StringFormat("ON  %s, none upcoming", g_newsFromCsv ? "csv" : "cal");
   long mins = ((long)g_newsNextT - (long)TimeCurrent()) / 60;
   return StringFormat("%s %s %s (%s)", g_newsBlock ? "BLOCK" : "ok",
                       TimeToString(g_newsNextT, TIME_DATE | TIME_MINUTES), g_newsNextName,
                       mins >= 0 ? StringFormat("in %dm", (int)mins) : "now");
  }

//--- next allowed server hour (for panel) ---
string NextAllowedHourStr()
  {
   if(!UseTimeFilter) return "any";
   MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
   if(BlockFriday && dt.day_of_week == 5) return "Mon";
   for(int k = 0; k < 24; k++)
     {
      int h = (dt.hour + k) % 24;
      if(g_hourMap[h]) return StringFormat("%02d:00", h);
     }
   return "none";
  }

//+------------------------------------------------------------------+
//| On-chart dashboard (was missing in v6)                            |
//+------------------------------------------------------------------+
void Panel()
  {
   double bWavg, bLastPx, bLots; int bCount;
   double sWavg, sLastPx, sLots; int sCount;
   ReadGrid(MagicBuy,  bWavg, bLastPx, bLots, bCount);
   ReadGrid(MagicSell, sWavg, sLastPx, sLots, sCount);

   double bal   = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq    = AccountInfoDouble(ACCOUNT_EQUITY);
   double acctDD = (g_peakBal > 0) ? (g_peakBal - eq) / g_peakBal * 100.0 : 0;
   double bFloat = FloatPnL(MagicBuy);
   double sFloat = FloatPnL(MagicSell);
   double gridFloat = bFloat + sFloat;
   double gridDD = (gridFloat < 0) ? (-gridFloat / g_refCapital * 100.0) : 0.0;
   double allLots = TotalLots();
   int total = g_wins + g_losses;
   double wr = (total > 0) ? (100.0 * g_wins / total) : 0;

   string s = "";
   s += "════ BayesianGrid v7.02 HF ════\n";
   s += StringFormat("%s  |  %s\n", g_sym, TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES));
   s += StringFormat("Bal %.2f  Eq %.2f  AcctDD %.2f%%\n", bal, eq, acctDD);
   s += StringFormat("GridFloat $%.2f  GridDD %.2f%%/cap %.0f\n", gridFloat, gridDD, g_refCapital);
   s += StringFormat("Lots %.2f / %.2f  Spread %d\n",
                     allLots, MaxTotalLots, (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD));
   s += "─────────────────────────────\n";
   s += StringFormat("BUY : %2d/%d  lots %.2f  PnL %.2f\n", bCount, MaxLayers, bLots, bFloat);
   if(bCount > 0) s += StringFormat("      wavg %.5f  last %.5f\n", bWavg, bLastPx);
   s += StringFormat("SELL: %2d/%d  lots %.2f  PnL %.2f\n", sCount, MaxLayers, sLots, sFloat);
   if(sCount > 0) s += StringFormat("      wavg %.5f  last %.5f\n", sWavg, sLastPx);
   s += "─────────────────────────────\n";
   s += StringFormat("W %d  L %d  WR %.0f%%  Sess $%.2f\n", g_wins, g_losses, wr, g_sessionPnL);
   s += StringFormat("TimeFilter %s%s  next %s\n",
                     UseTimeFilter ? "ON" : "OFF",
                     (UseTimeFilter && HoursInGMT) ? StringFormat(" (GMT+%d)", ServerGMTOffset) : "",
                     NextAllowedHourStr());
   s += "News " + NewsPanelStr() + "\n";
   if(MaxHoldSec > 0 || StopPips > 0 || DailyLossPct > 0 || PeakKillPct > 0 || EntryMode == ENTRY_STRETCH)
      s += StringFormat("HF %s %s hold %ds stop %.1fp ladder %d/%d|%d/%d day %s kill %s\n",
                        EnumToString(EntryTF), EntryMode == ENTRY_STRETCH ? "STRETCH" : "SYM", MaxHoldSec, StopPips,
                        bCount, g_effMaxLayers[0], sCount, g_effMaxLayers[1],
                        g_dayHalt ? "STOPPED" : "ok", g_kill ? "LATCHED" : "ok");
   if(ProbExitMode != PROB_OFF)
      s += StringFormat("ProbExit %s  Pmin %.2f  edge %.2f\n", EnumToString(ProbExitMode), ProbMin, ProbEdge);
   string guards = "";
   if(MaxEquityDD_Pct > 0)      guards += StringFormat("DDhalt %.0f%% ", MaxEquityDD_Pct);
   if(EmergencyCloseDD_Pct > 0) guards += StringFormat("Basket %.0f%% ", EmergencyCloseDD_Pct);
   if(MaxSpreadPts > 0)         guards += StringFormat("MaxSprd %d ", MaxSpreadPts);
   if(CloseOnFriday)            guards += StringFormat("FriClose@%d ", FridayCloseHour);
   if(UseNewsFilter && NewsPauseLayers)         guards += "NewsPause ";
   if(UseNewsFilter && NewsCloseMode != NEWS_CLOSE_NONE) guards += StringFormat("NewsClose%d ", (int)NewsCloseMode);
   if(guards == "")             guards = "none (no SL active)";
   s += "Guards: " + guards + "\n";
   if(g_halt)
     {
      if(HaltCooldownMin > 0 && g_haltUntil > 0)
         s += StringFormat(">>> EMERGENCY HALT — resumes %s <<<\n", TimeToString(g_haltUntil, TIME_DATE | TIME_MINUTES));
      else
         s += ">>> EMERGENCY HALT LATCHED <<<\n";
     }
   s += "═════════════════════════════";

   Comment(s);
  }
//+------------------------------------------------------------------+
