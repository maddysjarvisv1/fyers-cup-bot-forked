# GTT / Bracket / Trailing Stop-Loss & Profit-Taking — how to use Fyers' inbuilt risk tools

Research (2026-09-28) — Fyers official support KB + installed `fyers_apiv3` (3.1.15) source. Primary sources cited inline.

## What Fyers offers inbuilt (all exchange-side; no app/polling needed)

### 1. Smart Stop-loss & Target (replaced legacy Bracket BO / Cover CO)
Source: https://support.fyers.in/portal/en/kb/articles/how-to-place-a-bracket-order-in-fyers
- One order places an entry **with** Stop-loss + Target (TP) legs.
- **OCO (One-Cancels-Other):** when either exit leg executes, the other is cancelled automatically.
- Product types: Intraday, Delivery/Overnight, MTF.
- **Multi-day caveat:** SL/TP on Delivery & MTF requires **DDPI or POA**; then valid up to **365 days**.
- Converting Intraday→Overnight cancels existing SL/TP; corporate action cancels them too.

### 2. Trailing Stop-Loss (Smart Trail order)
Sources: KB article "How do I place a Trailing Stop-Loss order in FYERS?"; `fyersModel.py` `create_smartorder_trail` (Config `/smart-order/trail`).
- Auto-trails the stop **by a fixed `jump_diff`** (min 0.2) as price moves in your favour.
- Payload: `symbol`, `side`, `qty`, `orderType` (1 Limit / 2 Market), `stopPrice` (initial>,0), `jump_diff` (min 0.2), `productType`, `validity`, `limitPrice` (0 = market).
- Also `modify_smart_order` (change stopPrice / jump_diff) and `cancel_smart_order`.

### 3. TP/SL offsets on a normal/SL-M order
Source: `fyersModel.py` `place_order` (lines ~944-974), community example (https://fyers.in/community/t/modify-bracket-order-stoploss-api/21649).
- `takeProfit` + `stopLoss` offsets, `legType` = 1 **Points** or 2 **Percentage of entry** (default Points).
- `modify_order` can later update/remove TP (`takeProfit=0` removes); `legType` read-only once legs exist.
- Bracket legs appear in the orderbook with their own ids → modifiable individually.

### 4. GTT multi-leg (OCO) — `place_gtt_order`
- `orderInfo.leg1` (entry; `triggerPrice` must be **above** LTP for the entry leg) + optional `orderInfo.leg2` (exit; `triggerPrice` **below** LTP).
- `modify_gtt_order` updates legs; `gtt_orderbook`; `cancel_gtt_order`.
- Validity here is `IOC`/`DAY` (verified live via probe: also requires `leg1.price >= 0.0025`).

## Mapping to our cup rules (SL 6% · breakeven +10% · partial-½ +20% · trail 10% of high)

| Our rule | Inbuilt support | Notes |
|---|---|---|
| Entry buy above trigger | ✅ standing SL-M / GTT leg1 | already live (order 26092800112601) |
| Initial SL 6% below entry | ✅ inbuilt SL leg / stopLoss offset | atomic on a Smart order |
| Take-profit 20% | ✅ inbuilt TP leg / takeProfit offset | exits the **entire** remaining qty — no partial-½ |
| Breakeven +10% (SL→entry) | ⚠️ not one-shot | needs `modify_smart_order`/`modify_order` at the threshold |
| Book half at +20% then trail | ⚠️ **not** reproducible in a single Fyers order | two-stage logic needs the bot, or use TP=full-exit instead |
| Trail 10% of high | ⚠️ inbuilt trails by **fixed points (jump_diff)**, not % | points-based; a % trail needs the bot to re-set the stop |

**Honest conclusion:** Fyers inbuilt gives you **exit-management that survives app/bot death** — one atomic Smart SL+TP (OCO) and/or a Smart Trail order. What it cannot do in a single order is our **conditional staging** (breakeven→partial-½→trail). So the choice is:
- **Route A (simplest, fully inbuilt):** on entry place one Smart/Smart-Trail order = SL 6% + TP 20% (full exit) (+ optional trailing jump). Exchange-managed, zero bot needed after entry. Slightly simpler exits than ours (no partial-book, points-based trail).
- **Route B (hybrid, closest to current edge):** inbuilt OCO SL for guaranteed protection + the **bot** nudges thresholds (`modify_smart_order` SL / partial) to keep our staged logic. Best of both; needs the bot running.
- **Multi-day / overnight** only with **DDPI/POA** enabled (Delivery/MTF). Our current orders are CNC DAY (intraday).

## Recommended next step (Route B)
For the NEXT LALPATHLAB-type entry, place via API a single **Smart order (or GTT leg1+leg2)** carrying the 6% SL (inbuilt) — so protection is guaranteed even if the bot dies — and keep the bot for the +10% breakeven / +20% partial / % trail. For the CURRENT standing order (already placed as plain SL-M), on fill the bot's protective sell-stop covers the SL today; we can then re-issue the exit as a Smart/Trail order if desired.

## Open question to Fyers
Whether a **percentage-based** trail + **partial-qty** exit are available in one Smart order (community thread above is unresolved on modify-bracket). Worth a support ticket if Route B's trailing must be %-based.