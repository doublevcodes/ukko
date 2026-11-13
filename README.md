# Stockpot

A stock-watching buying agent for professional kitchens (Junction 2026).

A camera notices a shelf product running low. Stockpot looks the product up at the wholesaler (Valio Aimo), compares
options, runs live web checks, and then either orders on its own or asks a person, following clear rules. Every step
is logged in plain language. A person can also type a request, such as "20 kg of butter by Friday, organic if possible".

It is two small programs that run on one laptop:

| Program | Port | What it does |
|---|---|---|
| **Valio agent gateway** (`gateway/`) | 8001 | The supplier side: catalogue search, live signed offers, a mock merchant that takes AP2-authorised orders, the supplier's own log, and the same interface as MCP tools. |
| **Stockpot buying agent** (`app/`) | 8000 | The buyer side: the pipeline, the rules, web checks, the dashboard and phone approval. It reaches the gateway over HTTP only, like any outside agent. |

`SPEC.md` is the full design.

## Set up (once)

You need [uv](https://docs.astral.sh/uv/) and the three keys.

```bash
uv venv --python 3.12 .venv                      # the AP2 SDK needs Python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate

cp .env.example .env                             # then fill in VALIO_API_KEY, GEMINI_API_KEY, EXA_API_KEY
python scripts/try_valio.py --download           # fetches the catalogue (87 MB) and two small data files into data/
```

## Run

Two terminals, both with `source .venv/bin/activate`:

```bash
uvicorn gateway.main:app --port 8001                 # terminal 1: the gateway
uvicorn app.main:app --host 0.0.0.0 --port 8000      # terminal 2: Stockpot
```

- Dashboard: http://localhost:8000
- Supplier's view (orders and what agents did): http://localhost:8001
- The gateway's interface, described: http://localhost:8001/docs

If port 8000 is used by something else, pick another one and tell Stockpot, so the phone link is right:

```bash
STOCKPOT_PORT=8010 uvicorn app.main:app --host 0.0.0.0 --port 8010
```

The header of the dashboard shows red labels if the gateway, the Gemini key or the Exa key is missing.

## The demo, step by step

1. Open the dashboard. The Inventory table shows the milk as "Not seen yet".
2. Take cartons off the shelf, or fake the camera: `python sensor/simulate.py --product milk_lactose_free --level low`
3. The request opens in the **Replenishment needed** card. Open "What the agent did, step by step" (or watch the
   Activity panel) for the live offers from Valio, the plans, the web checks and the rules.
4. Only 384 of the 400 litres are in stock, so Stockpot picks plan B: the usual milk plus a substitute for the rest.
   The total is above 500 EUR and there is a substitute, so it asks for approval.
5. Click **Approve & pay**, or scan the QR code and approve from a phone on the same network.
6. The order confirmation appears. Open http://localhost:8001 to see the same order from the supplier's side.
7. To run it again: `python sensor/simulate.py --level full`, then `--level low`. (A repeated "low" without a refill is ignored on purpose.)

To show the agent ordering on its own, open **Rules** and raise the limit or switch a rule off, then repeat.
To show a typed request, use the "What do you need?" box in the Replenishment needed card.

**Safe mode / Autopilot** in the header is the same spend-limit rule: Safe mode sets the limit to 0 EUR, so every
order waits for a person; Autopilot puts the earlier limit back.

**Live camera.** The dashboard shows the shelf camera's video (`CAMERA_FEED_URL` in `.env`, relayed by Stockpot at
`/camera/feed`). While a dashboard is open, Stockpot asks Gemini every few seconds where the products from
`config/shelf.yaml` stand in the picture, and draws a box over each one it finds. Hover a box to outline it. Click it
to highlight that product's tile, camera reading and inventory row and to show its latest request; click anywhere else
in the picture to clear. A click never orders anything. This needs `CAMERA_FEED_URL` and `GEMINI_API_KEY`: without the
camera there is no video, and without the key there are no boxes.

Everything on the dashboard is live. To see it filled in without a sensor or any keys, open `/?sample=1` (a saved
example of one request, with two example boxes in the camera panel).

## Check each part on its own

```bash
python scripts/try_valio.py      # each Valio endpoint once
python scripts/try_gateway.py    # search, signed offers, both AP2 flows, a refused order, a forged signature
python scripts/try_ap2.py        # the AP2 SDK alone, with throwaway keys
python scripts/try_exa.py        # the three web checks, with sources
python scripts/try_mcp.py        # connects to the gateway as an outside agent over MCP
pytest                           # the tests use a fake Valio and never touch the network
```

## Who decides what

- **The rules decide, not the language model.** `app/policy.py` is plain code. The model only reads typed text,
  picks which search hits match it, summarises web results and words the explanation. (It also finds the products
  in the camera picture for the dashboard; that cannot start or change an order.)
- **The agent orders alone** only when every rule passes: total within the limit, no substitute, on time, full
  quantity, usual size, an allowed product group, and web checks without concerns.
- **A person is asked** in every other case, and a hard requirement (such as lactose-free) is never broken.
- **Rules are edited in the dashboard** (Rules button) and saved to `config/policy.yaml`.
- **Web text is treated as data.** A warning or a confirmation without a source is rejected by code, and a check
  that cannot run is shown as failed, never skipped.

## How ordering is authorised (AP2)

Valio's mock API has no ordering endpoint, so the gateway contains a mock merchant. The authorisation is real:
it uses the [Agent Payments Protocol](https://github.com/google-agentic-commerce/AP2) SDK. No money moves.

- Saving the rules makes the **manager key** sign the agent's standing limits: which supplier, how much per order,
  how much in total.
- When the rules say "go", the **agent key** signs that one order under those limits. The gateway checks the
  signatures and the limits itself and refuses anything outside them, whatever Stockpot's own rules said.
- When a person approves, the **manager key** signs that exact cart and amount.
- Before any order the gateway re-prices the cart. If a price rose or stock fell, nothing is ordered and Stockpot
  plans again (and asks again, if a person had approved).
- Offers, checkouts and receipts are signed by the supplier, and Stockpot verifies them.
  Anyone can: the public keys are at http://localhost:8001/.well-known/jwks.json

## Not production yet

- The manager key lives in the Stockpot backend. In production it belongs on the manager's phone or a hardware key.
- The phone approval link has no login; the one-time code in the link is the only guard.
- Buyers register their key with a plain call (`POST /buyers`), standing in for real customer onboarding.
- The gateway logs which agent is asking but does not restrict what each agent may do.
- All prices exclude VAT, and orders are in whole sales units.
- Valio's mock gives one delivery date per product and ignores the requested date. Products whose order cut-off has
  passed are treated as not orderable; set `ENFORCE_ORDER_CUTOFF=false` in `.env` if Valio's mentors say otherwise.

## Starting clean

Restarting Stockpot clears its requests (the audit file `data/audit_log.jsonl` is kept).
To empty the supplier's orders and log as well, stop the gateway and delete `data/gateway/orders.jsonl` and
`data/gateway/supplier_log.jsonl`.
