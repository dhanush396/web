# High-frequency grid for BayesianGrid: research, design, and the Step 0 result

## 1. The algo being modified (v6.12/v6.13 core, kept as is)

- **Two independent grids**, one BUY and one SELL, isolated by magic number (`MagicBuy`, `MagicSell`). Both open together on a new bar when idle.
- **Layering:** each grid adds a layer every `GridSpacingPts` against itself, up to `MaxLayers`.
- **Lot ladder:** `BaseLot` for `FlatLayers`, then `+LotIncrement` per layer.
- **One shared TP per grid:** live volume-weighted average entry ± `TP_Pips`, recomputed from live positions after every add. `SetTP` never touches the other grid.
- **No per-position stop.** The time filter gates new grids only.

**Goal:** a high-frequency grid scalper on $100.

- Hold time of 1–30 minutes.
- Starts at 0.01 lot and scales up.
- Trades only in chosen sessions or on M1/M5.
- Keeps the news filter.
- Must not blow up.

## 2. Research (workflow `hf-grid-research`, 7 agents)

Five independent sweeps read **333 distinct sources**. The full list is in `research/results/hf_research/sources.txt`; the per-finding citations are in `research_findings.json`.

| Sweep | Mechanisms | Sources read |
|---|---|---|
| Institutional / academic: market making, OU mean reversion, grid-trading papers | 19 | 74 |
| MQL5 articles, Market products, forums | 26 | 94 |
| Open-source grid / scalper code (read only, nothing cloned) | 28 | 66 |
| Practitioner post-mortems (ForexFactory, Myfxbook, Reddit, blogs) | 20 | 59 |
| Regime filters and retail execution | 18 | 46 |

### What survives across sources

1. **Institutional HF grids earn the spread; they don't pay it.**
   - A bank's EUR/USD book made its profit from spread revenue, and its speculative positions were not profitable within 30 minutes ([Mende & Menkhoff](https://ideas.repec.org/a/eee/finmar/v9y2006i3p223-245.html)).
   - Open HF-grid backtests are profitable only with exchange maker rebates ([hftbacktest](https://github.com/nkaz001/hftbacktest)).
   - A plain grid is a zero-expectation system ([Dynamic Grid Trading, arXiv 2506.11921](https://arxiv.org/abs/2506.11921)).
   - An MT5 market-order grid pays the spread on every fill.
2. **Inventory must be finite and known before the trade:**
   - Hard caps, with one-sided quoting at the bound ([Guéant–Lehalle–Fernandez-Tapia](https://arxiv.org/abs/1105.3115)).
   - Inventory skew ([Avellaneda–Stoikov](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)).
   - Time-based liquidation.
3. **Per-layer lot escalation is where grids die.** Martingale, LotExponent and "recovery" schemes appear in nearly every blow-up post-mortem.
4. **Bounded cycles:** a hard time stop, a basket stop anchored to layer 1, and a ladder sized to a dollar budget before the first fill.
5. **Avoid news, rollover and the London open.** Restrict trading to measured mean-reverting windows.

### The synthesized v7 design (16 modules, ranked survival-first)

M1 to M16, in priority order:

| # | Module |
|---|---|
| M1 | 30-minute basket clock, a no-add cutoff and a decaying TP |
| M2 | Basket stop at 3σ₁₅ from layer 1, plus a broker-side SL |
| M3 | Ladder auto-fitted to a 3.5% equity budget with a drawdown floor, starting at 0.01, with hard lot caps |
| M4 | Daily (6%) and weekly (10%) limits, a stops-per-day cap, and a −20% kill latch |
| M5 | Horizon-aware news filter: flatten before the release and wait for volatility to settle |
| M6 | Session windows with blackouts for rollover, the London open and fixes |
| M7 | σ-scaled spacing and TP with cost floors |
| M8 | Cost-to-edge gate on every order |
| M9 | Volatility and jump circuit breaker |
| M10 | Adds only on closed M1 bars, with minimum spacing in time |
| M11 | Inventory skew across the two grids |
| M12 | Optional stretch-and-reclaim entry |
| M13 | Offline variance-ratio enable per session |
| M14 | Execution robustness: rate-limited TP modifies, virtual TP, close with retries |
| M15 | Telemetry |
| M16 | Self-disabling edge monitor |

The full specification, with inputs, defaults and search ranges, is in `research/results/hf_research/v7_design_and_critique.json`.

### The adversarial critique: reject as specified until Step 0 passes

- **Parameter contradictions.** With the default costs, the cost gate blocks the default Asia window.
- **The ladder doesn't fit.** "0.01 → 0.01 → 0.02" does not fit a 3.5% budget on a $100 standard account at any volatility once the stop gap is respected. Flat 0.01 × 2 layers is the realistic ladder, unless the account is a cent account.
- **The core objection:** with gross snap-back of +0.15 to +0.5 pip against 0.8–1.4 pip of cost, every module only reshapes a negative expectancy. They make the EA survivable, not profitable.
- **The required first step:** measure the gross markout against the all-in cost before building anything.

## 3. Step 0: gross 5–30 minute reversal vs round-trip cost

`research/step0_markout.py`. Oanda EURUSD M1 mid prices, **2012–2016 in-sample only**; 2017–2020.05 is untouched.

Sign-adjusted means, in pips, with standard errors:

- **A:** the mean move h minutes after price stretched s pips within the last 15 minutes. This is what triggers a grid layer.
- **B:** the module M12 stretch-and-reclaim trigger.

Costs: raw = 0.2-pip spread × hour profile + $7/lot commission (0.7 pip) + 0.3-pip slippage; standard = 1.2-pip spread × hour profile + 0.3 pip.

| Best cells | n | Gross markout | Raw all-in cost | Edge / cost | Years with ≥ 2× cost (of 5) |
|---|---|---|---|---|---|
| B, late NY (19–24 server), 30 min | 6,689 | +0.85 ± 0.16 | 1.26 | 0.67 | 0 |
| B, rollover/Asia (00–03), 30 min | 1,668 | +0.76 ± 0.30 | 1.31 | 0.58 | 1 |
| A, s = 8 pips, Asia (03–09), 30 min | 5,963 | +0.64 ± 0.14 | 1.25 | 0.51 | 0 |
| A, s = 8 pips, Asia (03–09), 15 min | 5,963 | +0.57 ± 0.10 | 1.25 | 0.46 | 0 |
| A, s = 8 pips, London/ECB (12–15), 30 min | 15,408 | +0.46 ± 0.10 | 1.20 | 0.38 | 0 |
| NY overlap (15–19), either statistic | — | ‑0.27 to +0.11 | 1.20 | < 0.1 | 0 |

**Result.**

- The mean reversion is real and statistically significant in Asia and late NY.
- It is **33–49% smaller than the all-in round-trip cost of even a raw ECN account**, and far below a standard account's cost.
- It is measured on mid quotes, which overstate tradeable reversion.

So **no grid geometry, ladder or exit rule can make a 1–30 minute EURUSD grid profitable at retail execution costs.** Barriers and stops change the shape of the P/L distribution, not its mean (optional stopping). Full table: `research/results/hf_step0/step0_markout_2012_2016.csv`.

## 4. What can be done

1. **Build v7 anyway, as a survivable EA.** Bounded cycles cap any day at about −6%, and the kill latch fires at −20%. On this evidence expect a slow bleed of roughly the cost per trade, not profit.
2. **Re-run Step 0 on an instrument whose cost is small relative to its 30-minute volatility,** such as XAUUSD or index CFDs, using the broker's own data. This needs that data: an MT5 tick export from your broker, or permission to fetch a public M1 dataset.
3. **Re-run Step 0 on your broker's real bid/ask ticks for 2020–2026** (MT5 → Symbols → Ticks → Export). Mid-price Oanda data can't settle whether your broker's late-NY and Asia spreads leave anything.

Until Step 0 passes on some instrument and cost structure, the research says the answer is "do not trade it for profit".

## 5. v7 built and tested (your choice: build it and test other markets)

### What was built

- **EA v7.00** keeps your core (dual magic grids, live weighted-average TP) and adds the research modules. All are **off by default**:

  | Input(s) | What it does |
  |---|---|
  | `EntryTF` | M1/M5 entries |
  | `EntryMode=STRETCH` | Opens only the reversion side after a stretch + reclaim, with a trend veto |
  | `MaxHoldSec` | Closes a grid on the age of its oldest position, ms precision |
  | `StopPips` | Basket stop anchored to layer 1, plus a broker-side SL |
  | `MaxBasketRiskPct` | Pre-trade ladder fit to an equity budget |
  | `AddMinSec`, `NoAddAfterSec` | Add pacing and a no-add cutoff |
  | `MaxSpreadPts` | Spread guard, now on adds too |
  | `DailyLossPct` | Daily loss limit |
  | `PeakKillPct` | Persisted kill latch |
  | `SessionStart`/`SessionEnd` | Session window, with optional close at session end |
  | `LotMultiplier` | Geometric lot ladder |

  It also fixes `SetTP` wiping the SL.
- **The simulator mirrors every module.** It has second-level holding, randomised Brownian-bridge intrabar paths, and per-waypoint kill/daily checks; 16 tests pass.
- **`research/mt5_import.py`** loads MT5 bar or tick exports with the broker's own spread. That is the route to XAUUSD and indices: only EURUSD data exists in this environment.

### Results (`research/optimize_hf.py`, $100 standard account, start lot 0.01)

- **In-sample search:** 800 Bayesian (TPE) trials on 2012–2016 with raw-ECN costs (0.2-pip spread + $7/lot + slippage) and randomised intrabar paths.
  - **No configuration that trades at least 50 times a year is profitable.** The best is −1.4%/yr at 15–17% max DD.
  - The optimiser escaped by trading less, not more: late-NY stretch entries, 7.5–10 pip TP, 10-minute holds, about 0.2 trades a day.
- **Holdout:** 2017–2020.05, opened once for 5 frozen configs. All of them lose 2.2–2.5%/yr ($100 → ~$92).
- **Robustness:** path seeds, the OHLC path, spread ×1.5, slippage ×2 and commission $10 all leave the conclusion unchanged.

### Why HF-grid backtests elsewhere look spectacular

The same search under optimistic tester assumptions (no spread, commission or slippage; 4-point OHLC bars) finds the configuration below:

| Same config, 2012–2016 | $100 becomes | CAGR | Max DD |
|---|---|---|---|
| Zero cost + 4-point OHLC bars | **$49,157** | **+245.6%** | 1.2% |
| Zero cost + random intrabar path | $95 | −0.9% | 25% (kill) |
| Raw costs + 4-point OHLC bars | $78 | −4.9% | 25% (kill) |
| **Raw costs + random path (realistic)** | **$75** | **−5.5%** | 25% (kill) |

The +245% is entirely an artifact. A 30-second time stop on a fixed O→L→H→C bar path always exits on the bar's high or low, and no costs are charged. Remove the path artifact and even a zero-cost run is a coin flip; add real costs and it loses.

**This is how MT5 "1 minute OHLC" or "Open prices" tests and zero-spread settings produce the HF-grid equity curves sold on marketplaces.** Always judge an HF EA in the MT5 tester with *"Every tick based on real ticks"*, your broker's spread and commission, and *Random delay*.

## 6. Adversarial code review of v7 (workflow `review-v7-hf-grid`, 7 agents)

There is no MetaEditor in this environment. So four independent reviewers each read the code through one lens, and a skeptic per lens then tried to refute every finding:

1. MQL5 compile correctness
2. Live-trading logic
3. Simulator correctness
4. EA-vs-simulator parity

Results:

- **0 compile errors** in the 1,745-line EA. MetaEditor is still the final word.
- **34 findings, 33 confirmed, 1 uncertain.** All are fixed in EA v7.01 and the simulator; 20 tests pass, and the v6.13 presets reproduce.

Fixes that matter most for live money:

- **Partly failed stops.** If a basket or time stop only partly closed, the stop re-anchored to the surviving layer, the grid kept averaging, and `EnsureSL` loosened the broker SL. Each side now has a persisted layer-1 anchor and a flatten latch, and an SL is never moved further away.
- **Netting accounts.** The EA now refuses to run on a netting account, where the two grids would merge into one position.
- **Restarts.** The daily-loss state and the equity peak are persisted, so a restart no longer forgets a daily stop.
- **Close retries.** Closes back off for 2–30 s, and prices round to tick size, which matters for gold and indices.
- **Broker SL.** It is set right after each fill, from the actual fill price.

Fixes that matter for the backtest numbers:

- **Kill/daily limits.** They are solved at the exact crossing, and also fire on losses realised while flat.
- **Spread changes.** Stops and TPs crossed by a spread change are detected.
- **Hold deadline.** It is an event inside each price leg, so no TP is credited after `MaxHoldSec`.
- **Slippage.** It is charged on layer-1 opens and market exits (`MktSlip`).
- **Presets.** `.set` files now carry every v7 input, plus the optimiser's commission and slippage for the ladder fit.

The full findings and verdicts are in `research/results/hf_research/review_v7.json`.

## 7. The direct answer: grid size and TP for your algo in HF mode

The setup is your production logic: BUY+SELL together, layers at the grid size, TP from the live weighted average, start 0.01 and +0.01 per layer, up to 5 layers, no stop. The only HF changes are M1 entries, a 5-minute hold limit and the news filter. Data: EURUSD 2012–2016, $100. Files: `research/results/hf_std_raw/sweep_spacing_tp.csv` and `sweep_cost_breakeven.csv`.

**Return %/yr with zero trading cost** (rows = grid size, columns = TP):

| Grid \ TP | 1 pip | 2 pips | 3 pips | 5 pips | 8 pips | 12 pips |
|---|---|---|---|---|---|---|
| 2 pips (20 pts) | **+295** | +288 | +255 | −40 | −38 | −48 |
| 3 pips (30 pts) | +250 | +276 | +240 | −43 | −41 | −49 |
| 5 pips (50 pts) | +170 | +203 | +209 | +166 | −42 | −37 |
| 7.5 pips (75 pts) | −41 | +133 | +149 | +154 | −37 | −43 |
| 10 pips (100 pts) | −49 | −48 | −40 | +118 | +101 | +89 |
| 15 pips (150 pts) | −49 | −38 | −37 | −38 | +81 | +68 |

**With real raw-ECN costs, all 36 cells lose 36–50%/yr with 90–98% drawdown,** i.e. the account is wiped.

**Break-even cost** (spread + commission + slippage scaled together; return %/yr):

| Cell | 0 pips round trip | 0.13 | 0.26 | 0.45 | 0.65 | 1.30 (raw ECN) |
|---|---|---|---|---|---|---|
| Grid 2 / TP 1 | +295 | **+232** | −42 | −38 | −38 | −41 |
| Grid 5 / TP 3 | +209 | **+146** | −38 | −37 | −38 | −39 |
| Grid 2 / TP 2 | +288 | −38 | −40 | −37 | −37 | −37 |
| Grid 3 / TP 2 | +276 | −38 | −40 | −37 | −41 | −38 |
| Grid 7.5 / TP 5 | +154 | −38 | −49 | −40 | −37 | −50 |

**So the best grid size and TP on EURUSD are 2 pips / 1 pip, or 5 pips / 3 pips.** They only work if the *all-in* round trip is about **0.13 pip or less**.

- By ~0.26 pip, every cell blows the account.
- The cheapest retail raw accounts are ~0.6–1.3 pip round trip, about 5–10× the break-even.
- Even at zero cost, drawdowns are 45–55%.
- The zero-cost profits depend on the simulated sub-minute price path, so treat them as an upper bound.

This is exactly why institutional HF grids work and retail ones don't: they are *paid* the spread (maker rebates), so their cost is negative.

## 8. Run on Vantage Markets conditions

### Account conditions

Researched by workflow `vantage-conditions`: one sweep of Vantage's own pages, one of independent review and measurement sites, then a reconcile step. Where sources disagree, the costlier value is used. These figures are for the offshore/global entity on MT5:

| Account | Typical EURUSD spread | Commission | Leverage | Stop-out |
|---|---|---|---|---|
| Raw ECN | 0.2 pip (measured 0.08–0.3) | $6/lot round turn | 1:500 | 20% |
| Raw ECN, best case | 0.13 pip (ForexBrokers.com) | $6/lot round turn | 1:500 | 20% |
| Standard STP | 1.4 pip (Vantage says ~1.2) | $0 | 1:500 | 20% |
| Cent | like STP, balance in USC | $0 | 1:500 | 20% |
| Pro ECN (reference only, $10,000 minimum) | 0.2 pip | $3/lot round turn | 1:500 | 20% |

Sources are in `research/results/vantage/vantage_conditions_research.json`.

Caveats:

- Vantage does not publish EURUSD swaps or the cent contract size. Read them from MT5 → Symbol → Specification.
- Swap does not affect these HF tests. Positions are closed within 5 minutes and before 23:00 server time, so none are held overnight.

### Results

$100, EURUSD, your production logic in HF mode (M1 entries, 5-minute hold, 0.01 +0.01 per layer, 5 layers). The full table is in `research/results/vantage/vantage_backtests.csv`.

| Vantage account | Grid × TP cells ending above $100 (2012–16 / 2017–20) | Best cell | Grid 2 / TP 1 | Grid 5 / TP 3 |
|---|---|---|---|---|
| Raw ECN (0.2 + $6) | **0/36 / 0/36** | ends at ~$10 | $9.19 / $5.52 | $8.89 / $9.61 |
| Raw ECN best case (0.13 + $6) | **0/36 / 0/36** | ends at ~$10 | $4.69 / $9.34 | $8.93 / $10.04 |
| Standard STP (1.4) | **0/36 / 0/36** | ends at ~$10 | $7.88 / $10.02 | $1.28 / $10.30 |
| Cent (1.4, USC) | **0/36 / 0/36** | ends at ~$0.10 | $0.08 / $0.10 | $0.09 / $0.10 |
| Pro ECN (0.2 + $3) | **0/36 / 0/36** | ends at ~$10 | $9.75 / $9.92 | $8.86 / $9.33 |

The ~$10 floor is where the margin buffer stops new grids. On a cent account the same costs simply bleed the balance to nearly zero.

The slow late-NY stretch configuration from the in-sample search loses least: $100 → $84–86 on Raw ECN, Standard STP and Pro ECN, and → $98.6 on Cent, where the 0.01 lot is far smaller relative to equity. It is still a loss, and it trades about 0.1–0.2 times a day.

**Conclusion on Vantage:** even on the cheapest account ($6/lot + 0.13–0.2 pip ≈ 0.8 pip per round trip), costs are 3–6× the ~0.13–0.26 pip break-even of the best grid/TP cells.

The decisive check is your own Vantage MT5 Strategy Tester: "Every tick based on real ticks", with your account's real spread, commission and swap.

## 9. Probability view: can conditional probability make it better?

Script: `research/prob_edge.py`. Results: `research/results/probability/`. Data: EURUSD M1. 2012–2016 is used to choose; 2017–2020.05 is never used to choose. All-in cost is Vantage Raw ECN, 0.8 pip per round trip.

### Sample space of one basket (your production logic, HF mode)

Ω = {TP hit, time stop / session / news close}. Payoffs are all-in, in $ per basket with a 0.01 start lot.

| Grid / TP | Period | P(TP) | Avg TP win | Avg other exit | P(TP) needed to break even | E[basket] |
|---|---|---|---|---|---|---|
| 2 / 1 | 2012–16 | **74.4%** | +$0.070 | −$1.064 | **93.8%** | −$0.22 |
| 2 / 1 | 2017–20 | 65.9% | +$0.057 | −$0.697 | 92.4% | −$0.20 |
| 5 / 3 | 2012–16 | 31.6% | +$0.322 | −$0.355 | 52.5% | −$0.14 |
| 5 / 3 | 2017–20 | 21.8% | +$0.291 | −$0.254 | 46.6% | −$0.14 |
| 7.5 / 5.3 | 2012–16 | 12.6% | +$0.565 | −$0.234 | 29.3% | −$0.13 |
| 7.5 / 5.3 | 2017–20 | 7.2% | +$0.536 | −$0.179 | 25.1% | −$0.13 |

The break-even probability is p_be = L / (W + L). A 74% hit rate looks good, but at grid 2 / TP 1 the costs push p_be to about 94%.

### Unconditional events: the random-walk benchmark

Test one entry at every in-session M1 close, long and short, with news windows excluded. The events are:

- **W:** +a pips before −b pips.
- **L:** −b before +a.
- **T:** neither within H minutes.

For a price with no drift, gambler's ruin gives P(W | resolved) = b/(a+b). Optional stopping gives E[gross] = 0 for any a, b and H. EURUSD sits on that benchmark:

| a / b / H | P(W \| resolved) 2012–16 | 2017–20 | Random walk b/(a+b) | Gross E (pips) 2012–16 / 2017–20 |
|---|---|---|---|---|
| 1 / 2 / 5m | 0.656 | 0.681 | 0.667 | −0.08 / −0.04 |
| 3 / 5 / 30m | 0.639 | 0.652 | 0.625 | −0.02 / −0.00 |
| 5.3 / 7.5 / 45m | 0.606 | 0.617 | 0.586 | −0.01 / +0.00 |

Choosing grid size, TP or holding time only moves probability between W, L and T. Expectancy before cost stays at about 0. After cost it is −0.8 pip per trade. This is the probability version of "barriers don't create edge".

### Conditional probability: the lever that does work

Condition each trade on its state when it opens: z-stretch relative to the trade direction, session, volatility tercile, and 60-minute trend. This gives 134 cells per bracket.

**Fading a stretch has a real, stable conditional edge.** In-sample and out-of-sample have the same sign, and the effect mirrors when you trade with the stretch:

| Bracket | Trade fades a 0.5–1.5σ stretch: gross 2012–16 / 2017–20 | Trade follows the stretch: gross 2012–16 / 2017–20 |
|---|---|---|
| 1 / 2 / 5m | +0.03 / +0.04 pip | −0.22 / −0.14 pip |
| 3 / 5 / 5m | +0.18 / +0.11 pip | −0.22 / −0.13 pip |
| 3 / 5 / 30m | +0.27 / +0.18 pip | −0.29 / −0.18 pip |
| 5.3 / 7.5 / 45m | +0.34 / +0.20 pip | −0.33 / −0.20 pip |

**It does not pay 0.8 pip.**

- Only 7 of 670 cells were net-positive in 2012–16.
- Pooled out of sample, they lose −0.37 and −0.46 pip per trade (gross +0.43 and +0.34).
- The best single in-sample cell (01–05, low vol, trend >2, gross +1.28) dropped to +0.62 gross / −0.18 net.

The conditional edge is about +0.1 to +0.3 pip. Break-even needs an all-in cost of about 0.2–0.3 pip. Vantage's commission alone is 0.6 pip.

### Independence: losses cluster

Run consecutive, non-overlapping trades for each bracket:

| Bracket | P(L) | P(L \| previous L) | 5-loss streak observed | If independent, P(L)^5 |
|---|---|---|---|---|
| 3 / 5 / 5m, 2012–16 | 0.147 | 0.250 | 0.0011 | 0.0001 (**11× lower**) |
| 3 / 5 / 5m, 2017–20 | 0.092 | 0.213 | 0.0006 | 0.00001 |
| 1 / 2 / 5m, 2017–20 | 0.294 | 0.331 | 0.0052 | 0.0022 (2.4× lower) |
| 5.3 / 7.5 / 45m, 2017–20 | 0.303 | 0.346 | 0.0056 | 0.0026 (2.2× lower) |

Multiplying per-trade probabilities, i.e. assuming independence, understates loss streaks by 2–11×. It therefore understates ruin, and grid layers make this worse because they add size into the streak. Use the complement rule on the real streak frequency instead: P(at least one ruin in N baskets) = 1 − (1 − q)^N, which goes to 1 as N grows.

**Conclusion.**

- Conditional probability, P(reversal | stretch, session, volatility), is the right way to improve the algo. The stretch entry in v7 (`EntryMode = ENTRY_STRETCH`) is exactly that, and it is why the late-NY stretch configuration was the least-bad result.
- It raises expected gross from about 0 to about +0.2 pip.
- A 1–30 minute EURUSD grid becomes positive only if the conditional edge exceeds the all-in cost. That needs costs around 0.2–0.3 pip per round trip, or a signal about 3–4× stronger than any state found here.

## 10. v7.02: no holding period, exit at TP or only when the probability is down

You asked for three things:

- No holding-period exit.
- Every grid exits at its TP.
- A grid exits early only when its probability of reaching TP has dropped.

That is now built into both the EA and the simulator:

- `MaxHoldSec = 0`, `CloseAtSessionEnd = false`.
- A new **v7.02 Probability Exit** input group.

Scripts:

| Script | What it does |
|---|---|
| `research/prob_exit_fit.py` | Fits the probability model |
| `research/prob_exit_eval.py` | Runs the backtests |
| `research/prob_exit_report.py` | Prints every number quoted in this section from the result files |
| `research/results/prob_exit/` | Holds the results |

### What "the probability is down" means, exactly

At the open of every M1 bar, for each open grid, the EA computes **P = P(this grid reaches its TP before its stop)**:

    logit P = logit P_rw + b0 + bZ·(stretch in favour) + bTrend·(60-min trend in favour)
              + bVol·ln(σ1m/σlong) + bAge·ln(1+minutes open) + bLayers·(layers−1)

**P_rw is the random-walk probability, including your grid's own future adds.** It is gambler's ruin chained over the ladder:

- Between the TP and the next add level, a driftless price hits the TP first with probability (x−A)/(TP−A).
- Reaching A adds the next layer. That moves the average and the TP closer, and the same calculation repeats.
- Once the ladder is full, the stop is the lower barrier.

With all coefficients at zero, P is exactly what a random walk would give your ladder.

There are two exit rules:

- **FLOOR** (`ProbMin`): exit when P < ProbMin. This is the literal "probability is down" rule.
- **EDGE** (`ProbEdge`): exit when logit P − logit P_rw < −ProbEdge. The model must say the odds are *worse than a random walk*.

**The stop (`StopPips`, anchored to the layer-1 fill) is still required, and it is still an exit.** It is the "lose" event that makes the probability defined: without a stop, a random walk reaches the TP with probability 1, and the real barrier becomes your margin. It is checked on every tick. The probability rule is checked once per M1 bar, so price can reach the stop inside a bar before the rule has a chance to fire. In the preset cell below, 87.5% of grids end at TP, 1.5% at the probability exit and **11.0% at the stop**.

### The fitted model (2012–2016; 6.29 M decision points from 1.76 M grids; SEs clustered by trading day)

| Term | Coefficient | t |
|---|---|---|
| b0 | +0.036 | 1.9 |
| Stretch in favour (zs) | **+0.086** | 8.6 |
| Trend in favour (ts) | **+0.029** | 3.1 |
| ln(σ1m/σlong) | −0.001 | −0.0 |
| ln(1 + age) | −0.006 | −1.0 |
| Layers − 1 | +0.005 | 0.8 |

**The random walk alone is already calibrated.** Fitting logit P = a + c·logit P_rw gives a = 0.065 and c = 0.968; a perfect fit would be a = 0 and c = 1.

On 2017–2020.05:

- The fitted terms improve log-loss over the random walk by **0.002%**, which is nothing.
- Calibration deciles (model / random walk / observed): 0.389 / 0.373 / 0.384 … 0.818 / 0.815 / 0.822 … 0.962 / 0.963 / 0.962.

The probability that a grid reaches its TP is what a random walk predicts for your ladder. Stretch and trend are statistically real but far too small to matter.

A first version used v/(u+v) as the baseline, which ignores future adds. It appeared to beat the random walk by 3.5–4.8%. That gain was purely the ladder's mechanical effect: adds pull the TP closer. The adversarial review caught this (workflow `review-prob-exit-v702`).

### Backtests: does exiting on probability beat holding to TP?

Setup: your HF logic (M1 entries 01–23 h, news filter, 0.01 +0.01 ladder, MaxLayers 5, 5% basket risk budget), with no time exit.

Comparison 1: a fixed 0.01 ladder on a large balance, so every configuration trades the whole period. Vantage Raw ECN costs, stop 40 pips, **$ per year**:

| Grid / TP | Period | Hold to TP (no early exit) | EDGE 0.25 | FLOOR 0.25 | FLOOR 0.40 | EDGE 0 | Old 5-min time stop |
|---|---|---|---|---|---|---|---|
| 2 / 1 | 2012–16 | −24,867 | −24,873 | −27,908 | −31,025 | −36,447 | −52,174 |
| 2 / 1 | 2017–20 | −16,728 | −16,751 | −18,976 | −21,382 | −30,506 | −40,415 |
| 5 / 3 | 2017–20 | −4,893 | −4,916 | −5,710 | −6,350 | −17,655 | −17,125 |
| 7.5 / 5.3 | 2017–20 | −2,978 | −3,011 | −3,614 | −3,983 | −16,499 | −14,836 |
| 15 / 8 | 2012–16 | −2,021 | −2,032 | −2,372 | −2,643 | −14,825 | −14,248 |
| 15 / 8 | 2017–20 | **−1,177** | −1,176 | −1,402 | −1,478 | −14,904 | −13,863 |

Averaged over all 12 grid/TP/stop cells in 2017–20:

| Rule | Grids hitting TP | Avg hold | $ per grid |
|---|---|---|---|
| Hold to TP | 85.5% | 58 min | −$0.234 |
| FLOOR 0.25 | 82.8% | 52 min | −$0.250 |
| 5-min time stop | 25.0% | 4 min | −$0.150 |

The time stop loses less *per grid* but opens 3.6 times as many grids, so it loses far more per year.

Exit rules compared with holding to TP, across the 24 cell × period combinations, on real costs:

- Every FLOOR rule and EDGE 0 earn less in all 24.
- EDGE 0.25 earns more in 4, by under $1 to $10 a year, and less in 20, by $1 to $33 a year. It fires on only 0.4% of grids on average, so it is a statistical tie that leans slightly worse.

**Exiting on a low P gives up gross value, not just exit costs.** Even at zero cost, in 2017–20 FLOOR 0.25 earns $0.033 per grid against $0.048 for holding to TP, and it is lower in all 24 cells. About 89% of FLOOR's shortfall at real costs is this lost gross value; the rest is the exit's spread and slippage. Grids with a low P still reach TP slightly more often than the random walk predicts (bottom calibration decile: 0.373 predicted, 0.384 observed). Part of this could also be the fill model: a TP fills inside the bar, while a probability exit fills at the next bar open.

Comparison 2: $100 on Vantage Raw ECN.

- **0 of 192 runs end above $100.**
- Every run bleeds until the 5% risk budget cannot fit one 0.01 layer, and then stops trading.
- It stops at about $21, $41 or $81 for a 10, 20 or 40-pip stop.

### Conclusion

- **Doing what you asked works mechanically.** With no holding period, 85% of grids exit at TP on average over the 12 cells (54–99% depending on grid size and stop distance).
- **It still loses.** The rest end at the stop with the full ladder on. Each grid also pays spread, commission and slippage, plus an overnight swap now that grids can be held overnight. The result is −$0.23 per grid per 0.01 ladder.
- **"Exit when the probability is down" cannot fix that.** The probability of reaching TP is the random walk's. Cutting a grid when P is low gives up the slightly-better-than-random chance it still had, and pays the exit cost on top. That is why every FLOOR threshold does worse than simply holding to TP.
- **On these backtests, holding to TP (`ProbExitMode = PROB_OFF`, with the stop) is marginally the best.** If you want a probability exit anyway, `ProbExitMode = PROB_EDGE` with `ProbEdge = 0.25` and the fitted coefficients is the least harmful one. It ties with holding to TP and fires only when the model sees worse-than-random odds. Read the `PROB EXIT` lines in the journal to see when it fires.
- **The verdict on the money is unchanged.** The cost per round trip is larger than any edge the probability can find.

Preset: `presets/BG_HF_v702_tp_or_prob_exit.set` uses grid 15 / TP 8 / stop 40 / 5% risk / EDGE 0.25 with the fitted coefficients. It is the least-bad cell, not a profitable one: −$1,176 a year per 0.01 ladder in 2017–20.

- Although MaxLayers is 5, at most 3 layers are reachable: layer 4 would sit beyond the 40-pip stop.
- With a 5% risk budget, it trades a single 0.01 position below about $186 of equity, and stops opening grids below about $82.

Test it in your Vantage MT5 Strategy Tester with "Every tick based on real ticks".
