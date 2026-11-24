"""Small local public-data diagnostics entry point."""
from __future__ import annotations
import argparse
from .mexc_api import MEXCPublicClient

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-preflight", action="store_true")
    args = parser.parse_args()
    if not args.public_preflight:
        parser.print_help()
        return 0
    instruments = MEXCPublicClient().instruments()
    print({"status":"TECHNICAL_PUBLIC_DATA_DISCOVERY_PASS","active_cross_margin_contracts":len(instruments),"orders_submitted":0,"private_endpoints_used":False})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
