# Reorder model — Ukko / Stockpot

## 1. Context

Ukko is a kitchen restocking agent built at a hackathon. A camera counts products on a shelf. When a product is low, an agent looks up offers from Valio's mock API, runs web checks, applies spending rules, and orders or asks for approval.

Today "low" is a fixed threshold, the order quantity is close to fixed ("24 pcs when low"), and usage is an assumed constant ("typical, not yet measured, 8.0 pcs/day"). This spec replaces those three things with a model that uses how fast stock is actually being used.

*What must not change:* the request pipeline, the policy and approval rules, the signed spending limits (AP2), payments, and the sensor stability rules ("low for 3 looks", "must be seen full first"). This spec only changes how "low" is decided, how much is ordered, and what is shown and logged about that.

We are a small team with mixed coding experience and a deadline tomorrow morning. Write simple, readable Python. Make small changes. Do not refactor unrelated code.

## 2. The model in plain words

- *When to order.* Stock divided by usage rate is the time until the shelf is empty. We reorder when the stock we have (plus what is already on order) would not safely last until the next delivery.
- *How much to order.* Enough to last until delivery, plus a set number of days after it, plus a safety buffer, minus what we already have.

### Formulas

```
inventory position   IP = on_hand_units + on_order_units

expected demand over the lead time
                     D_lead = d * (sum of the daily multipliers for each day until delivery)

average adjusted daily demand
                     d_eff = D_lead / lead_days

safety buffer        B = z * sigma_factor * sqrt(d_eff) * sqrt(lead_days)

reorder point        R = D_lead + B

REORDER WHEN         IP <= R

order-up-to level    S = R + d * (sum of the daily multipliers for the cover days after delivery)

raw order quantity   Q = S - IP        (then apply the caps in section 6)
```

where `lead_days = L + extra_lead_days`.

## 3. Parameters, defaults and the reasoning for each

Put the global ones in one config file and the per-product ones next to the existing shelf/product config. Every one of these must be explained in the README (section 9).

### Global

| Name | Default | Meaning and reasoning |
|---|---|---|
| `model_day_seconds` | `1800` | *Demo clock: 30 real minutes = 1 model day.* Lets a short demo show days of usage. Set to `86400` for real time. See section 4. |
| `alpha` | `0.3` | How fast the usage estimate adapts. It averages over roughly the last 3 to 5 days. Higher reacts faster but chases noise. |
| `sigma_factor` | `1.5` | Day-to-day variability. For counts of independent events the variance equals the mean (Poisson), so the standard deviation is `sqrt(d)`. The 1.5 allows for busy and quiet days. This is an assumption, not a measurement. |
| `z` by service class | critical `2.326` (99%), standard `1.645` (95%), perishable `1.282` (90%) | The chance of not running out before delivery. The right level balances the cost of running out against the cost of excess: a staple the menu depends on deserves a high level, and something that spoils deserves a lower one. |
| `default_cover_days` | `7` | Days of stock to hold after a delivery arrives. Larger means fewer deliveries but more stock at risk of expiring. |
| `default_lead_days` | `2` | Days until delivery, used only until a real Valio quote gives the actual delivery date. |
| multiplier caps | `0.7` to `1.5` per day | Limits how far web signals can move demand. |
| `extra_lead_days` cap | `0` to `3` | Limits how far web signals can lengthen the lead time. |

### Per product

| Name | Default | Meaning and reasoning |
|---|---|---|
| `typical_daily_use` | `8` | Assumed usage before anything is measured (the cold start). It fades out as real usage is observed (section 5). |
| `service_class` | `standard` | One of `critical`, `standard`, `perishable`. |
| `units_per_visible_item` | `24` for the demo, `1` for real use | Each item the camera sees stands for this many units (a case). *Why:* with only 1 to 4 single items on a demo shelf, the safety buffer is larger than the whole shelf, so everything would always be "low". Counting each visible item as a case makes the numbers behave as they would in a real stockroom. Show this on the dashboard so it is not hidden. |
| `pack_size` | `24` | Orders are rounded to whole packs. |
| `max_stock_units` | `144` | Storage limit. We never order beyond it. |
| `shelf_life_days` | none | If set, we never order more than will be used before it expires. |
| `cover_days` | uses the global default | Per-product override. |

## 4. The model clock

- `model_days_elapsed = real_seconds_elapsed / model_day_seconds`.
- Use the model clock for exactly three things: measuring the usage rate, fading old usage, and marking an order as "delivery due" after its lead time has passed.
- Everything shown as a calendar date stays real: Valio's delivery date, event dates from web signals, audit timestamps.
- Write one small helper (for example `model_now()` and `model_days_between(a, b)`) and use it everywhere. Do not scatter the scale factor through the code.
- Show a small chip on the dashboard: "Demo clock: 1 day = 30 min" (hidden when `model_day_seconds` is 86400).

## 5. Estimating the usage rate `d`

Keep one estimate per product, in units per model day.

```
tau = -1 / ln(1 - alpha)          # alpha 0.3 gives tau ≈ 2.804 model days

start:      d = typical_daily_use,  last_update = model_now()

on every stable drop in the count (consumption of `units` units):
    dt = model days since last_update
    d  = d * exp(-dt / tau) + units / tau
    last_update = model_now()

when reading d between events:
    d_now = d * exp(-(model days since last_update) / tau)
```

Rules:
- Only stable readings count: use the count after the existing "N looks agree" rule, never raw frames.
- `units = (old_count - new_count) * units_per_visible_item`.
- *Increases are deliveries, not negative usage.* When the stable count goes up, do not change `d`. Reduce `on_order_units` by the increase (not below zero).
- The starting value fades with the same decay, so the assumption is replaced by measurements without a sudden switch.
- Keep a count of observed drops per product, so the dashboard can say "assumed" (0 drops) or "measured from N observations".

## 6. Deciding and sizing an order

Write these as *pure functions* (inputs in, numbers out, no network, no global state) in one module, so they are easy to test.

1. `lead_days`: from the latest Valio quote for the product, `max(1, (delivery_date - today).days)`; otherwise `default_lead_days`. Add `extra_lead_days`.
2. *Daily multipliers:* get a list with one number per day from today, from the demand-signals module if it exists. If it does not exist yet, write a stub that returns `1.0` for every day and `extra_lead_days = 0`, with a clear interface so the real one can replace it:
   `get_adjustments(product, days) -> (daily_multipliers, extra_lead_days, reasons)`
3. Compute `D_lead`, `d_eff`, `B`, `R` and `IP` as in section 2.
4. *Trigger:* if `IP <= R` and the existing sensor rules are satisfied, start a reorder. Because `on_order_units` is part of `IP`, an open order stops a second one from being placed.
5. *Quantity:* compute `S` and raw `Q`, then apply in this order:
   1. Cap at `max_stock_units - IP`.
   2. If `shelf_life_days` is set, cap at `d_eff * shelf_life_days`.
   3. Round *up* to a whole number of packs. If that breaks a cap, round *down*.
   4. If the result is below one pack: order one pack if the caps allow it; otherwise do not order, and log why.
6. When an order is placed, add its quantity to `on_order_units`. After `lead_days` model days, mark it "delivery due". Never add stock automatically: the camera is the source of truth.

Leave the policy rules alone. Tell me how "the usual amount" is currently defined for the "more than 3 times the usual amount" rule, and suggest how it should relate to this model, but do not change it without asking.

## 7. Worked examples (use these as unit tests)

Use a tolerance of 0.01.

| Case | Inputs | Expected |
|---|---|---|
| Reorder point, standard | d=8, lead 2 days, multipliers [1, 1], z=1.645, factor 1.5 | B = 9.87, R = 25.87 |
| Reorder point, critical | as above with z=2.326 | B = 13.96, R = 29.96 |
| Reorder point, perishable | as above with z=1.282 | B = 7.69, R = 23.69 |
| With demand signals | d=40, lead 2 days, multipliers [1.3, 0.925], z=1.645 | D_lead = 89.00, d_eff = 44.5, B = 23.28, R = 112.28 |
| Rate update | d=8, alpha=0.3, then 24 units used after 0.1 model days | tau = 2.804, d = 16.28 |
| Quantity | d=8, lead 2, cover 7, multipliers all 1, IP=24, pack 24, max stock 144 | S = 81.87, raw Q = 57.87, final Q = 72 |
| Quantity with shelf life | as above with shelf_life_days=5 | cap = 40, final Q = 24 |
| Trigger | IP=48, R=46.64 | no reorder |
| Trigger | IP=24, R=25.87 | reorder |
| Delivery seen | on_order=72, stable count rises by 3 items of 24 units | on_order = 0, d unchanged |
| Model clock | 900 real seconds at model_day_seconds=1800 | 0.5 model days |

## 8. Dashboard changes

Inventory table, replacing the fixed "24 pcs when low" text:
- *Usage per day*, labelled "assumed" or "measured (N observations)".
- *Days of cover* = `IP / (d * today's multiplier)`. Show a dash when usage is near zero.
- *Reorder point* in units, next to current stock and stock on order.
- Keep a product's row visible after ordering, with status "On order" and the delivery date.
- A "Why?" expander per product that shows the calculation in words, using the same text as the audit entry.
- The demo-clock chip, and a note that each visible item counts as `units_per_visible_item` units.

Settings: add a read-only "Reorder model" panel listing the parameters from section 3 with a one-line reason for each. Making them editable is optional and comes last.

## 9. Where the reasoning goes

*Audit trail.* Write plain-language entries, with the actual numbers, at these moments:
- When a reorder is triggered, for example: "Usage is 16.3 a day (measured from 2 observations). Delivery takes 2 days, so we expect to use 32.6 before it arrives. Safety buffer 14.1 (95% service level). Reorder point 46.6. Stock 24 + on order 0 = 24, which is below 46.6, so reordering."
- When the quantity is set, for example (same product as above): "Lasting 2 days until delivery and 7 days after needs 160.6. Minus 24 in stock, that is 136.6. Storage holds 144, so the order is limited to 120 (5 packs of 24)." Always mention any cap that applied and why.
- When web signals change the result: say which signal, which days, the percentage, and the source.
- When stock rises and is treated as a delivery.
- Do *not* write an entry for every small change in the usage estimate.

*README.* Add a section "How Ukko decides when and how much to reorder" containing:
- the idea in two sentences, then the formulas from section 2;
- the parameter tables from section 3, including the reasoning column;
- *simplifications and limits*, stated honestly:
  - the variability rule (`1.5 * sqrt(d)`) is an assumption until measured;
  - the normal-distribution buffer is rough when quantities are very small;
  - the web-signal percentages are judgment-based starting values, to be replaced by measured ratios;
  - the demo runs on a fast clock, and each visible item stands for a case;
- how to switch to real time (`model_day_seconds = 86400`, `units_per_visible_item = 1`).

*Code.* Each formula gets a short comment saying what it is and why, pointing to the README section.

## 10. Steps

Do one step at a time. After each: summarise what changed in plain English, give the exact commands to test it, and wait for "continue".

- *R0. Read and report (no code changes).* Find where the following live and tell me: the sensor readings and the stability rules, the current "low" decision, the current order quantity, the assumed usage rate, product/shelf config, on-order tracking, the audit writer, and the inventory table. Say where each part of this spec will go and flag anything that conflicts.
- *R1.* The model clock helper and the new config values, with tests.
- *R2.* The pure functions from sections 5 and 6, with all the tests from section 7. Nothing is wired in yet.
- *R3.* Wire in the usage estimate: update it on stable drops, treat rises as deliveries, track `on_order_units`.
- *R4.* Replace the "low" decision and the order quantity with the new functions. Add the audit entries.
- *R5.* Dashboard columns, the "Why?" expander, the demo-clock chip, and the read-only settings panel.
- *R6.* The README section.

## 11. Working rules

- No new dependencies. `math` from the standard library is enough.
- Do not change policy, approval, AP2 signing, payments, or the Valio and Exa clients.
- Do not invent file names or structures: use what R0 finds.
- Keep the calculation functions pure and under about 40 lines each.
- When something fails, show the exact error and the smallest fix.
- If any instruction here conflicts with how the code already works, stop and ask.
