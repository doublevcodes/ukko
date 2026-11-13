"""Call each Valio endpoint once and print the result.

Run:  python scripts/try_valio.py
      python scripts/try_valio.py --download     (also fetch the three data files into data/)
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import settings, valio_client  # noqa: E402

SAMPLE_PRODUCTS = ["469363", "464201", "463038"]


def show(title: str, value: object) -> None:
    print(f"\n== {title}")
    print(json.dumps(value, indent=2, ensure_ascii=False)[:1500])


def main() -> None:
    try:
        show("GET /api/ping", valio_client.ping())
        when = date.today() + timedelta(days=3)
        show(f"POST /api/availability for {when}", valio_client.availability(SAMPLE_PRODUCTS, when))
        show(f"POST /api/price for {when}", valio_client.price(SAMPLE_PRODUCTS, when)["products"][0])
        if "--download" in sys.argv:
            files = [
                (valio_client.download_delivery_reliability, settings.RELIABILITY_CSV),
                (valio_client.download_carbon_footprint, settings.CARBON_CSV),
                (valio_client.download_products, settings.CATALOGUE_PATHS[0]),
            ]
            for download, destination in files:
                path = download(destination)
                print(f"\nDownloaded {path} ({path.stat().st_size:,} bytes)")
        else:
            print("\nAdd --download to fetch the catalogue, delivery-reliability and carbon-footprint files.")
    except valio_client.ValioError as error:
        print(f"\nValio API problem: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
