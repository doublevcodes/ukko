# Demo video shot list

60–90 seconds, 16:9 screen recording of the laptop dashboard (QuickTime or OBS). One continuous take after a dry run. Captions beat a mic.

## Prep

1. Gateway on :8001, Stockpot on :8000, Stripe **test** key in `.env`.
2. Settings → Payment → Add card once (`4242 4242 4242 4242`).
3. Spend limit / stock set so the first run needs Approve (Plan B / substitute story).
4. Prefer `python sensor/simulate.py --product … --level low` over live vision.

## Shots

| Time | What to show |
| --- | --- |
| 0–10s | Inventory goes low; Activity timeline starts |
| 10–35s | Offers → Plan B (exact + substitute) → web sources → policy reasons |
| 35–50s | Click **Approve** (no card form). Confirmation: Stripe + AP2 `human_present` |
| 50–65s | http://localhost:8001 supplier view: same order, Stripe payment line |
| 65–85s | Refill `full` then `low`, Autopilot on → auto-approve + silent Stripe charge (`human_not_present`) |

## Avoid

- `.env` / Stripe Dashboard secret keys on screen
- Long Exa waits (collapse web checks or pre-warm)
- Unreliable live camera mid-take

Export H.264 mp4; zoom so policy sentences stay readable on a projector.
