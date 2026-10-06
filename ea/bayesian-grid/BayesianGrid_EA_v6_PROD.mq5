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
//+------------------------------------------------------------------+
#property copyright   "Jeckov Kanani — Bayesian Grid v6.13 (Production)"
#property version     "6.13"
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

input group           "══════ Grid Parameters ══════"
input double          BaseLot              = 0.08;     // Base lot (layers 1-5)
input int             FlatLayers           = 5;        // Flat-lot layers
input double          LotIncrement         = 0.07;     // Increment per layer after flat
input int             LotIncEvery          = 1;        // Apply LotIncrement every N layers after flat (1 = v6.12)
input int             GridSpacingPts       = 75;       // Grid spacing in points (7.5 pips)
input double          TP_Pips              = 5.3;      // TP distance from wavg (pips)
input int             MaxLayers            = 18;       // Max layers per side
input int             MagicBuy             = 519401;   // BUY grid magic
input int             MagicSell            = 519402;   // SELL grid magic

input group           "══════ Risk ══════"
input double          MaxTotalLots         = 8.0;      // Hard cap on combined lots (both grids)
input double          MarginBufferMult     = 5.0;      // Require free margin >= this x (layer-1 margin)
input int             MaxSpreadPts         = 0;        // Skip NEW grid if spread > this (0 = disabled)
input double          MaxEquityDD_Pct      = 0.0;      // Halt NEW grids when GRID float-loss% >= this (0 = off)
input double          EmergencyCloseDD_Pct = 0.0;      // CLOSE EA GRIDS when float-loss% >= this (0 = off). Basket stop.
input double          RefCapital           = 0.0;      // Reference capital for DD% (0 = balance at init, <0 = current balance)
input bool            ResetHaltOnInit      = true;     // Clear emergency-halt latch on (re)load
input int             HaltCooldownMin      = 0;        // Auto-clear halt this many minutes after a basket stop (0 = latch until reload)

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
   if(LotIncEvery < 1 || NewsBeforeMin < 0 || NewsAfterMin < 0 || NewsCloseMin < 0 || HaltCooldownMin < 0)
     {
      Print("FATAL: LotIncEvery must be >= 1 and news/cooldown minutes >= 0. Aborting.");
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

   //--- A restored halt latch with EA positions still open = an unfinished basket stop ---
   if(g_halt && PosCount(MagicBuy) + PosCount(MagicSell) > 0)
     {
      g_haltFlatten = true;
      Print("!!! Restored HALT latch with open EA positions -> will finish closing them");
     }

   //--- News filter ---
   NewsSetupCurrencies();
   if(UseNewsFilter)
      NewsRefresh(true);

   //--- Recovery: detect existing grids and seed their start times ---
   int bc = PosCount(MagicBuy);
   int sc = PosCount(MagicSell);
   g_buyGridStart  = (bc > 0) ? OldestOpenTime(MagicBuy)  : 0;
   g_sellGridStart = (sc > 0) ? OldestOpenTime(MagicSell) : 0;

   PrintFormat("═══ BayesianGrid v6.13 PRODUCTION ═══");
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
         int fails = CloseAll(MagicBuy) + CloseAll(MagicSell);
         if(fails > 0 && DebugLog) PrintFormat("!!! HALT FLATTEN: %d position(s) still open, retrying", fails);
         if(ShowPanel) Panel();
         return;
        }
      g_haltFlatten = false;
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
            CloseAll(mags[k]);
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
   if(s_prevBuyCount == 0 && bCount > 0)  g_buyGridStart  = TimeCurrent();
   if(s_prevSellCount == 0 && sCount > 0) g_sellGridStart = TimeCurrent();
   if(s_prevBuyCount < 0 && bCount > 0 && g_buyGridStart == 0)   g_buyGridStart  = OldestOpenTime(MagicBuy);
   if(s_prevSellCount < 0 && sCount > 0 && g_sellGridStart == 0) g_sellGridStart = OldestOpenTime(MagicSell);

   //--- Detect grid closed by TP ---
   if(s_prevBuyCount > 0 && bCount == 0)
     {
      double pnl = RecentPnL(MagicBuy, g_buyGridStart);
      if(pnl > 0) g_wins++; else g_losses++;
      g_sessionPnL += pnl;
      PrintFormat(">>> BUY GRID CLOSED: P/L=$%.2f (was %d layers)", pnl, s_prevBuyCount);
      PersistSnapshot();
     }
   if(s_prevSellCount > 0 && sCount == 0)
     {
      double pnl = RecentPnL(MagicSell, g_sellGridStart);
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
      HealTP(ORDER_TYPE_BUY,  MagicBuy,  bWavg, bCount);
      HealTP(ORDER_TYPE_SELL, MagicSell, sWavg, sCount);
     }

   //--- Active grids: add layers (NEVER gated by time/halt — must reach TP) ---
   //    Optional exception: NewsPauseLayers holds adds inside a news window.
   bool layersOK = !(g_newsBlock && NewsPauseLayers);
   if(bCount > 0 && layersOK)
      TryAddLayer(ORDER_TYPE_BUY, MagicBuy, bWavg, bLastPx, bLots, bCount);

   if(sCount > 0 && layersOK)
      TryAddLayer(ORDER_TYPE_SELL, MagicSell, sWavg, sLastPx, sLots, sCount);

   //--- Idle grids: open new ones (gated) ---
   datetime curBar = iTime(g_sym, PERIOD_M15, 0);
   if(curBar != g_lastBar)
     {
      g_lastBar = curBar;

      bool timeOK   = IsTimeOK();
      bool riskOK   = MarginOKForLot(LotSize(1));
      bool ddOK     = (MaxEquityDD_Pct <= 0 || gridDD < MaxEquityDD_Pct);
      bool haltOK   = !g_halt;
      bool spreadOK = SpreadOK();
      bool newsOK   = !g_newsBlock;

      // SMOKE TEST: force the very first grid open after load, bypassing ONLY the time filter.
      static bool s_startupOpened = false;
      bool forceStart = (OpenOnStart && !s_startupOpened && bCount == 0 && sCount == 0);

      bool openOK   = (timeOK || forceStart) && riskOK && ddOK && haltOK && spreadOK && newsOK;

      if(DebugLog)
         PrintFormat("[BAR] Buy=%d Sell=%d Time=%s%s Margin=%s GridDD=%.1f%%(%s) AcctDD=%.1f%% Halt=%s Spread=%s News=%s -> %s",
                     bCount, sCount,
                     timeOK ? "OK" : "BLOCK",
                     forceStart ? "(START-FORCE)" : "",
                     riskOK ? "OK" : "BLOCK",
                     gridDD, ddOK ? "OK" : "HALT",
                     acctDD,
                     haltOK ? "OK" : "LATCHED",
                     spreadOK ? "OK" : "WIDE",
                     newsOK ? "OK" : "BLOCK(" + g_newsNextName + ")",
                     openOK ? "OPEN-ALLOWED" : "OPEN-BLOCKED");

      if(openOK)
        {
         if(forceStart)
            PrintFormat(">>> OPEN-ON-START: forcing confirmation grids (time filter bypassed once)");
         if(bCount == 0)
            OpenLayer1(ORDER_TYPE_BUY, MagicBuy);
         if(sCount == 0)
            OpenLayer1(ORDER_TYPE_SELL, MagicSell);
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
void OpenLayer1(ENUM_ORDER_TYPE type, int magic)
  {
   double lot = LotSize(1);

   //--- respect combined-lots cap even on a fresh open ---
   if(TotalLots() + lot > MaxTotalLots)
     {
      if(DebugLog) PrintFormat("L1 BLOCK: lots cap %.2f+%.2f>%.2f", TotalLots(), lot, MaxTotalLots);
      return;
     }

   MqlTick tick;
   if(!SymbolInfoTick(g_sym, tick)) return;

   double price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;

   double tp;
   if(type == ORDER_TYPE_BUY)
      tp = ND(price + TP_Pips * 10.0 * g_point);
   else
      tp = ND(price - TP_Pips * 10.0 * g_point);

   tp = ClampTP(tp, type);

   double fill = 0;
   if(MarketSend(type, lot, tp, magic, "L1", fill))
     {
      PrintFormat(">>> NEW %s GRID: fill=%.5f TP=%.5f lot=%.2f",
                  type == ORDER_TYPE_BUY ? "BUY" : "SELL",
                  fill > 0 ? fill : price, tp, lot);
     }
  }

//+------------------------------------------------------------------+
//| Try adding a layer to an active grid                              |
//+------------------------------------------------------------------+
void TryAddLayer(ENUM_ORDER_TYPE type, int magic,
                 double wavg, double lastPx, double totalLots, int count)
  {
   if(count >= MaxLayers) return;

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

   int    nextLayer = count + 1;
   double lot       = LotSize(nextLayer);

   double allLots = TotalLots();
   if(allLots + lot > MaxTotalLots)
     {
      PrintFormat("LOTS CAP: %.2f + %.2f > %.2f", allLots, lot, MaxTotalLots);
      return;
     }

   double fill = 0;
   if(!MarketSend(type, lot, 0, magic, "L" + IntegerToString(nextLayer), fill))
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
      if(MathAbs(curTP - tp) <= g_point) continue;

      MqlTradeRequest  req = {};
      MqlTradeResult   res = {};
      req.action   = TRADE_ACTION_SLTP;
      req.symbol   = g_sym;
      req.position = ticket;
      req.tp       = tp;

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
bool MarketSend(ENUM_ORDER_TYPE type, double lot, double tp, int magic,
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
         req.tp = 0;
      else if(res.retcode == TRADE_RETCODE_NO_MONEY)
         return false;

      if(!SymbolInfoTick(g_sym, tick)) return false;
      req.price = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;
      if(!MQLInfoInteger(MQL_TESTER)) Sleep(200);
     }

   //--- Fallback: no TP ---
   req.tp = 0;
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
double ND(double v) { return NormalizeDouble(v, g_digits); }

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
   datetime from = (fromTime > 0) ? (fromTime - 60) : (TimeCurrent() - 86400);
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
   if(!missing && tpMax - tpMin <= g_point) return;

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
   s += "════ BayesianGrid v6.13 PROD ════\n";
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
