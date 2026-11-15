"""Pretend to be the shelf camera, from the command line.

Examples:
    python sensor/simulate.py --product milk_lactose_free --level low
    python sensor/simulate.py --product milk_lactose_free --level full
    python sensor/simulate.py --product milk_lactose_free --level low --repeat 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensor.send_event import send_stock_level  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a fake shelf-sensor event to Ukko.")
    parser.add_argument("--product", default="milk_lactose_free", help="a key from config/shelf.yaml")
    parser.add_argument("--level", default="low", choices=["full", "low", "empty"])
    parser.add_argument("--confidence", type=float, default=0.93)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="where Ukko is running")
    parser.add_argument("--repeat", type=int, default=1, help="send the same event several times (to show duplicates are ignored)")
    args = parser.parse_args()

    for number in range(args.repeat):
        answer = send_stock_level(args.product, args.level, args.confidence, args.url, device_id="simulator")
        print(f"{args.product} = {args.level}: {answer if answer else 'no answer'}")
        if number + 1 < args.repeat:
            time.sleep(1)


if __name__ == "__main__":
    main()
