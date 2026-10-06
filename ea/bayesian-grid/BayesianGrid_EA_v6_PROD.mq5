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
//+------------------------------------------------------------------+
#property copyright   "Jeckov Kanani — Bayesian Grid v6.12 (Production)"
#property version     "6.12"
#property strict

input group           "══════ Grid Parameters ══════"
input double          BaseLot              = 0.08;     // Base lot (layers 1-5)
input int             FlatLayers           = 5;        // Flat-lot layers
input double          LotIncrement         = 0.07;     // Increment per layer after flat
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
input double          RefCapital           = 0.0;      // Reference capital for DD% (0 = use balance at init)
input bool            ResetHaltOnInit      = true;     // Clear emergency-halt latch on (re)load

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
   g_refCapital = (RefCapital > 0) ? RefCapital : AccountInfoDouble(ACCOUNT_BALANCE);
   if(g_refCapital <= 0) g_refCapital = 1; // guard against /0

   //--- Restore persisted stats / halt latch ---
   if(PersistStats)
     {
      if(GlobalVariableCheck(GvName("wins")))    g_wins       = (int)GlobalVariableGet(GvName("wins"));
      if(GlobalVariableCheck(GvName("losses")))  g_losses     = (int)GlobalVariableGet(GvName("losses"));
      if(GlobalVariableCheck(GvName("spnl")))    g_sessionPnL = GlobalVariableGet(GvName("spnl"));
      if(GlobalVariableCheck(GvName("peak")))    g_peakBal    = MathMax(g_peakBal, GlobalVariableGet(GvName("peak")));
      if(!ResetHaltOnInit && GlobalVariableCheck(GvName("halt")))
         g_halt = (GlobalVariableGet(GvName("halt")) > 0.5);
     }
   if(ResetHaltOnInit)
     {
      g_halt = false;
      if(GlobalVariableCheck(GvName("halt"))) GlobalVariableDel(GvName("halt"));
     }

   //--- Recovery: detect existing grids and seed their start times ---
   int bc = PosCount(MagicBuy);
   int sc = PosCount(MagicSell);
   g_buyGridStart  = (bc > 0) ? OldestOpenTime(MagicBuy)  : 0;
   g_sellGridStart = (sc > 0) ? OldestOpenTime(MagicSell) : 0;

   PrintFormat("═══ BayesianGrid v6.12 PRODUCTION ═══");
   PrintFormat("Sym=%s Pt=%.5f Digits=%d Spread=%d StopsLvl=%d",
               g_sym, g_point, g_digits,
               (int)SymbolInfoInteger(g_sym, SYMBOL_SPREAD),
               (int)SymbolInfoInteger(g_sym, SYMBOL_TRADE_STOPS_LEVEL));
   PrintFormat("Fill=%d VolMin=%.2f VolStep=%.2f",
               (int)SymbolInfoInteger(g_sym, SYMBOL_FILLING_MODE),
               SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN),
               SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP));
   PrintFormat("Base=%.2f Flat=%d Inc=%.2f Spacing=%d TP=%.1fpips Max=%d",
               BaseLot, FlatLayers, LotIncrement, GridSpacingPts, TP_Pips, MaxLayers);
   PrintFormat("Risk: MaxLots=%.2f MarginMult=%.1f MaxSpread=%d DDhalt=%.1f%% Basket=%.1f%% RefCap=%.2f",
               MaxTotalLots, MarginBufferMult, MaxSpreadPts, MaxEquityDD_Pct, EmergencyCloseDD_Pct, g_refCapital);
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
      if(PersistStats) GlobalVariableSet(GvName("halt"), 1);
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

   if(s_prevBuyCount <= 0 && bCount > 0)  g_buyGridStart  = TimeCurrent();
   if(s_prevSellCount <= 0 && sCount > 0) g_sellGridStart = TimeCurrent();

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

   //--- Active grids: add layers (NEVER gated by time/halt — must reach TP) ---
   if(bCount > 0)
      TryAddLayer(ORDER_TYPE_BUY, MagicBuy, bWavg, bLastPx, bLots, bCount);

   if(sCount > 0)
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

      // SMOKE TEST: force the very first grid open after load, bypassing ONLY the time filter.
      static bool s_startupOpened = false;
      bool forceStart = (OpenOnStart && !s_startupOpened && bCount == 0 && sCount == 0);

      bool openOK   = (timeOK || forceStart) && riskOK && ddOK && haltOK && spreadOK;

      if(DebugLog)
         PrintFormat("[BAR] Buy=%d Sell=%d Time=%s%s Margin=%s GridDD=%.1f%%(%s) AcctDD=%.1f%% Halt=%s Spread=%s -> %s",
                     bCount, sCount,
                     timeOK ? "OK" : "BLOCK",
                     forceStart ? "(START-FORCE)" : "",
                     riskOK ? "OK" : "BLOCK",
                     gridDD, ddOK ? "OK" : "HALT",
                     acctDD,
                     haltOK ? "OK" : "LATCHED",
                     spreadOK ? "OK" : "WIDE",
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
      lot = BaseLot + (layer - FlatLayers) * LotIncrement;

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
      if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      p += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP)
         + HistoryDealGetDouble(d, DEAL_COMMISSION);
     }
   return p;
  }

void CloseAll(int magic)
  {
   ENUM_ORDER_TYPE_FILLING fp = FillPolicy();
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
      r.type_filling = fp;

      long pt = PositionGetInteger(POSITION_TYPE);
      r.type  = (pt == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      r.price = (pt == POSITION_TYPE_BUY) ? SymbolInfoDouble(g_sym, SYMBOL_BID)
                                           : SymbolInfoDouble(g_sym, SYMBOL_ASK);
      ResetLastError();
      OrderSend(r, x);
     }
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
   s += "════ BayesianGrid v6.12 PROD ════\n";
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
   string guards = "";
   if(MaxEquityDD_Pct > 0)      guards += StringFormat("DDhalt %.0f%% ", MaxEquityDD_Pct);
   if(EmergencyCloseDD_Pct > 0) guards += StringFormat("Basket %.0f%% ", EmergencyCloseDD_Pct);
   if(MaxSpreadPts > 0)         guards += StringFormat("MaxSprd %d ", MaxSpreadPts);
   if(CloseOnFriday)            guards += StringFormat("FriClose@%d ", FridayCloseHour);
   if(guards == "")             guards = "none (no SL active)";
   s += "Guards: " + guards + "\n";
   if(g_halt) s += ">>> EMERGENCY HALT LATCHED <<<\n";
   s += "═════════════════════════════";

   Comment(s);
  }
//+------------------------------------------------------------------+
