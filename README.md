# Ukko

A stock-watching buying agent for professional kitchens (Junction 2026).

## Public URLs

| What | URL |
|---|---|
| **Dashboard (production)** | https://ukko-delta.vercel.app |
| **Same deploy (alias)** | https://stockpot-delta.vercel.app |
| **Supplier gateway** | https://ukko-delta.vercel.app/gateway |
| **Gateway API docs** | https://ukko-delta.vercel.app/gateway/docs |

Locally (see [Run](#run)): dashboard http://localhost:8000 · gateway http://localhost:8001 · MCP http://127.0.0.1:8002/mcp

A camera notices a shelf product running low. Ukko looks the product up at the wholesaler (Valio Aimo), compares
options, runs live web checks, and then either orders on its own or asks a person, following clear rules. Every step
is logged in plain language. A person can also type a request, such as "20 kg of butter by Friday, organic if possible".

It is two small programs that run on one laptop:

| Program | Port | What it does |
|---|---|---|
| **Valio agent gateway** (`gateway/`) | 8001 | The supplier side: catalogue search, live signed offers, a mock merchant that takes AP2-authorised orders, the supplier's own log, and the same interface as MCP tools. |
| **Ukko buying agent** (`app/`) | 8000 | The buyer side: the pipeline, the rules, web checks, the dashboard and phone approval. It reaches the gateway over HTTP only, like any outside agent. |

The supplier trust protocol (signed offers, verification checks, and what a pass does and does not prove) is in [`TRUST.md`](TRUST.md).

## Set up (once)

You need [uv](https://docs.astral.sh/uv/) and the three keys.

```bash
uv venv --python 3.12 .venv                      # the AP2 SDK needs Python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate

cp .env.example .env                             # then fill in VALIO_API_KEY, GEMINI_API_KEY, EXA_API_KEY, STRIPE_SECRET_KEY
python scripts/try_valio.py --download           # fetches the catalogue (87 MB) and two small data files into data/
```

Stripe: use **test mode** keys from the [Stripe Dashboard](https://dashboard.stripe.com/test/apikeys) (`STRIPE_SECRET_KEY` and `STRIPE_PUBLISHABLE_KEY`). After Ukko is running, open **Settings → Payment → Add card (once)** — the card form opens in the dialog (no redirect). Use `4242 4242 4242 4242`, any future expiry, any CVC. Every later order (auto-approve or Approve) charges that card.

## Run

Two terminals, both with `source .venv/bin/activate`:

```bash
uvicorn gateway.main:app --port 8001                 # terminal 1: the gateway
uvicorn app.main:app --host 0.0.0.0 --port 8000      # terminal 2: Ukko
python -m gateway.mcp_server                         # terminal 3: MCP for outside agents
```

- Dashboard: http://localhost:8000
- Supplier's view (orders and what agents did): http://localhost:8001
- The gateway's interface, described: http://localhost:8001/docs
- MCP discovery: http://localhost:8001/.well-known/mcp.json
- MCP endpoint (streamable HTTP): http://127.0.0.1:8002/mcp

If port 8000 is used by something else, pick another one and tell Ukko, so the phone link is right:

```bash
UKKO_PORT=8010 uvicorn app.main:app --host 0.0.0.0 --port 8010
```

The header of the dashboard shows red labels if the gateway, Gemini, Exa or Stripe keys are missing, or if no kitchen card is on file.

## The demo, step by step

1. Open the dashboard. The Inventory table shows the milk as "Not seen yet".
2. Take cans off the shelf, or fake the camera: `python sensor/simulate.py --product hell_energy --level low`
3. The request opens in the **Replenishment needed** card. Open "What the agent did, step by step" (or watch the
   Activity panel) for the live offers from Valio, the plans, the web checks and the rules.
4. Only 384 of the 400 litres are in stock, so Ukko picks plan B: the usual milk plus a substitute for the rest.
   The total is above 500 EUR and there is a substitute, so it asks for approval.
5. Click **Approve** (or **Approve & pay** on the phone). The saved Stripe test card is charged; no card form appears.
6. The order confirmation shows the Stripe payment method and AP2 flow. Open http://localhost:8001 to see the same order from the supplier's side.
7. To run it again: `python sensor/simulate.py --level full`, then `--level low`. (A repeated "low" without a refill is ignored on purpose.)

To show the agent ordering on its own, open **Rules** and raise the limit or switch a rule off, then repeat.
To show a typed request, use the "What do you need?" box in the Replenishment needed card.

**Safe mode / Autopilot** in the header is the same spend-limit rule: Safe mode sets the limit to 0 EUR, so every
order waits for a person; Autopilot puts the earlier limit back.

**Live camera.** The dashboard shows the shelf camera's video (`CAMERA_FEED_URL` in `.env`, relayed by Ukko at
`/camera/feed`). While a dashboard is open, Ukko asks Gemini every few seconds where the foods from
`config/camera_foods.yaml` stand in the picture, draws a box over each one it finds, and estimates how many units are
visible. Edit that file to teach the camera what it may see. Each food can name an exact Valio `product_id`, or leave
it blank so a reorder searches for the closest catalogue match. Counts and confidence update the shelf rows. A drop
from full to low/empty can start a reorder. Hover a box to outline it; click it to highlight that product's tile and
inventory row. This needs `CAMERA_FEED_URL` and `GEMINI_API_KEY`.

Everything on the dashboard is live. To see it filled in without a sensor or any keys, open `/?sample=1` (a saved
example of one request, with two example boxes in the camera panel).

## Check each part on its own

```bash
python scripts/try_valio.py      # each Valio endpoint once
python scripts/try_gateway.py    # search, signed offers, both AP2 flows, a refused order, a forged signature
python scripts/try_ap2.py        # the AP2 SDK alone, with throwaway keys
python scripts/try_exa.py        # the three web checks, with sources
python scripts/try_mcp.py        # outside agent over MCP stdio (spawns the server itself)
python scripts/try_mcp_http.py   # outside agent over the network MCP at :8002/mcp
pytest                           # the tests use a fake Valio and never touch the network
```

## Outside agents via MCP

Ukko talks to the gateway over HTTP. **Other** agents (Cursor, Claude Desktop, custom clients) can use the same supplier API as MCP tools.

1. Gateway on :8001, then `python -m gateway.mcp_server` (listens on `0.0.0.0:8002/mcp` by default).
2. Point an MCP client at `http://127.0.0.1:8002/mcp` (streamable HTTP).
3. Or open http://localhost:8001/.well-known/mcp.json for the published URL and tool list.

Example Cursor MCP entry (`~/.cursor/mcp.json` or project config):

```json
{
  "mcpServers": {
    "valio-aimo": {
      "url": "http://127.0.0.1:8002/mcp"
    }
  }
}
```

Tools: `search_products`, `list_requirement_ids`, `get_offers`, `create_checkout`, `complete_checkout`, `get_order_status`. Ordering still needs AP2 mandates; search and offers work with no keys.

## How Ukko decides when and how much to reorder

Ukko reorders when stock on hand plus stock already on order would not safely last until the next delivery. The order size tops the shelf back up to cover the lead time, a cover window after delivery, and a safety buffer — then rounds to whole packs and respects storage and shelf-life caps.

Formulas (see also `config/shelf.yaml` → `reorder_model`):

```
IP = on_hand_units + on_order_units
D_lead = d × (sum of daily multipliers until delivery)
d_eff = D_lead / lead_days
B = z × sigma_factor × √d_eff × √lead_days
R = D_lead + B
Reorder when IP ≤ R
S = R + d × (sum of multipliers for cover days after delivery)
Q = S − IP   (then storage / shelf-life caps and pack rounding)
```

`on_hand_units` = visible camera count × `units_per_visible_item` (demo default 24 = one case per can).

### Global parameters (`config/shelf.yaml` → `reorder_model`)

| Name | Default | Meaning |
|---|---|---|
| `model_day_seconds` | `1800` | Demo clock: 30 real minutes = 1 model day. Set `86400` for real time. |
| `alpha` | `0.3` | How fast the usage estimate adapts (~last 3–5 model days). |
| `sigma_factor` | `1.5` | Day-to-day variability vs Poisson √d (assumption until measured). |
| `z` by service class | critical `2.326` (99%), standard `1.645` (95%), perishable `1.282` (90%) | Chance of not stocking out before delivery. |
| `default_cover_days` | `7` | Days of stock to hold after a delivery arrives. |
| `default_lead_days` | `2` | Lead time until a Valio quote gives a real delivery date. |
| multiplier caps | `0.7`–`1.5` | Limits how far web demand signals can move daily demand. |
| `extra_lead_days` cap | `0`–`3` | Limits how far signals can lengthen lead time. |

### Per-product parameters (`camera_foods.yaml` / shelf defaults)

| Name | Default | Meaning |
|---|---|---|
| `typical_daily_use` | from food / `4` | Cold-start usage before anything is measured. |
| `service_class` | `standard` | `critical` / `standard` / `perishable` — picks `z`. |
| `units_per_visible_item` | `24` | Each camera-seen item counts as this many order units (a case). |
| `pack_size` | `24` | Orders round to whole packs. |
| `max_stock_units` | `144` | Storage cap. |
| `shelf_life_days` | none | Optional: never order more than will be used before expiry. |
| `cover_days` | `7` | Per-product override of cover window. |

### Simplifications and limits

- The variability rule (`1.5 × √d`) is an assumption until measured.
- The normal-distribution buffer is rough when quantities are very small.
- Web-signal percentages (when wired) are judgment-based starting values.
- The demo runs on a fast clock, and each visible item stands for a case.

### Switch to real time

In `config/shelf.yaml`: set `reorder_model.model_day_seconds: 86400` and per-product / default `units_per_visible_item: 1`.

Piece-counted camera products (`unit: pcs`) use this model. Litre/kg sensor products still use the older fixed `reorder_quantity` path.

## Who decides what

- **The rules decide, not the language model.** `app/policy.py` is plain code. The model only reads typed text,
  picks which search hits match it, summarises web results and words the explanation. (It also finds the products
  in the camera picture for the dashboard. A drop from full to low can start a reorder.)
- **The agent orders alone** only when every rule passes: total within the limit, no substitute, on time, full
  quantity, usual size, an allowed product group, and web checks without concerns.
- **A person is asked** in every other case, and a hard requirement (such as lactose-free) is never broken.
- **Rules are edited in the dashboard** (Rules button) and saved to `config/policy.yaml`.
- **Web text is treated as data.** A warning or a confirmation without a source is rejected by code, and a check
  that cannot run is shown as failed, never skipped.

## How ordering is authorised (AP2) and paid (Stripe)

Valio's mock API has no ordering endpoint, so the gateway contains a mock merchant. The authorisation is real:
it uses the [Agent Payments Protocol](https://github.com/google-agentic-commerce/AP2) SDK. The (test) money
movement is real Stripe: a kitchen card is saved once, then every order creates a PaymentIntent off-session.

- Saving the rules makes the **manager key** sign the agent's standing limits: which supplier, how much per order,
  how much in total.
- When the rules say "go", the **agent key** signs that one order under those limits. The gateway checks the
  signatures and the limits itself and refuses anything outside them, whatever Ukko's own rules said.
- When a person approves, the **manager key** signs that exact cart and amount.
- Immediately before the supplier records the order, Ukko charges the saved Stripe test card. If the charge
  fails, nothing is ordered.
- Before any order the gateway re-prices the cart. If a price rose or stock fell, nothing is ordered and Ukko
  plans again (and asks again, if a person had approved).
- Offers, checkouts and receipts are signed by the supplier (ES256), and Ukko verifies them.
  Anyone can: the public keys are at http://localhost:8001/.well-known/jwks.json
  See [`TRUST.md`](TRUST.md) for the minimum claim set and the seven verification checks.
  Settings → Trust test runs the same verifier against deliberately broken offers.
- Wholesale is often invoice-on-account; Stripe test mode is the demo PSP behind AP2 standing authority.

## Demo video (shot list)

About 60–90 seconds, 16:9 screen recording (QuickTime or OBS). Prefer `sensor/simulate.py` over live camera.

1. **0–10s** — Shelf goes low; Activity timeline starts.
2. **10–35s** — Timeline: Valio offers → Plan B → web sources → policy reasons.
3. **35–50s** — Click Approve (card already saved). Confirmation shows Stripe + AP2 `human_present`.
4. **50–65s** — Gateway at :8001: same order, Stripe payment line.
5. **65–85s** — Refill `full` then `low`, Autopilot on: auto-approve with silent Stripe charge (`human_not_present`).

Do not show `.env` keys. Pre-save the test card (or a 5s cold open with 4242…). Export H.264 mp4; zoom the dashboard so policy text is readable.

## Not production yet

- The manager key lives in the Ukko backend. In production it belongs on the manager's phone or a hardware key.
- The phone approval link has no login; the one-time code in the link is the only guard.
- Buyers register their key with a plain call (`POST /buyers`), standing in for real customer onboarding.
- The gateway logs which agent is asking but does not restrict what each agent may do.
- Stripe is test mode only; the kitchen card file lives in `data/stripe_customer.json`.
- All prices exclude VAT, and orders are in whole sales units.
- Valio's mock gives one delivery date per product and ignores the requested date. Products whose order cut-off has
  passed are treated as not orderable; set `ENFORCE_ORDER_CUTOFF=false` in `.env` if Valio's mentors say otherwise.

## Starting clean

Restarting Ukko clears its requests (the audit file `data/audit_log.jsonl` is kept).
To empty the supplier's orders and log as well, stop the gateway and delete `data/gateway/orders.jsonl` and
`data/gateway/supplier_log.jsonl`.
