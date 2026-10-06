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
