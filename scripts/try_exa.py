"""Run the three web checks on their own and print what they find.

Needs EXA_API_KEY and GEMINI_API_KEY in .env. Without them each check comes
back as "check_failed" with the reason, which is also worth seeing once.

Run:
    python scripts/try_exa.py
    python scripts/try_exa.py --product "Oatly iKaffe Oat drink 1L" --brand Oatly --constraint VEGAN --price 1.78
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import WebFinding  # noqa: E402
from app.web import exa_checks  # noqa: E402


def show(finding: WebFinding) -> None:
    print(f"\n== {finding.check_type}: {finding.severity.upper()}")
    print(f"   searched: {finding.query}")
    print(f"   summary:  {finding.summary}")
    for source in finding.sources:
        print(f"   source:   {source.title} ({(source.published_date or 'no date')[:10]})")
        print(f"             {source.url}")
        print(f"             \"{source.snippet}\"")


def main() -> None:
    parser = argparse.ArgumentParser(description="Try Stockpot's web checks.")
    parser.add_argument("--product", default="Arla Semi skimmed milkdrink 1 L UHT lactose free")
    parser.add_argument("--brand", default="Arla")
    parser.add_argument("--constraint", default="FREE_FROM_LACTOSE", help="a requirement id, checked for the product")
    parser.add_argument("--price", type=Decimal, default=Decimal("2.48"), help="quoted price per litre or kilo, in EUR")
    parser.add_argument("--per", default="litre", choices=["litre", "kilo", "pack"])
    args = parser.parse_args()

    show(exa_checks.recall_check(args.product, args.brand))
    show(exa_checks.substitute_check(args.product, args.constraint))
    show(exa_checks.price_check(args.product, args.price, args.per))


if __name__ == "__main__":
    main()
