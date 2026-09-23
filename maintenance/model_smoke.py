"""Optional real API checks. Never use list.xlsx or browser/session information."""
import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import Record, SafetyStop
from model_review import KeyStore, ModelClient, make_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Read key without echo, encrypt with Windows DPAPI")
    parser.add_argument("--models", action="store_true", help="Query available model IDs; no task data")
    parser.add_argument("--smoke", action="store_true", help="One synthetic advisory request; consumes API tokens")
    args = parser.parse_args()
    if sum((args.setup, args.models, args.smoke)) != 1:
        parser.error("Choose exactly one action")
    store = KeyStore(ROOT / "runtime")
    if args.setup:
        # Do not accept credentials as command-line arguments or echo them.
        key = getpass.getpass("SJTU API key (hidden): ")
        store.save(key)
        del key
        print("Key encrypted for the current Windows user. Not saved as plaintext.")
    elif args.models:
        print(json.dumps({"available": ModelClient(store).models()}, ensure_ascii=False))
    else:
        row = Record(2, "Synthetic", "synthetic-only", "Synthetic paper for API verification", "", "",
                     "00000", 0, "", "待处理", "缺少原文与人员对应证据", "2")
        context = make_context(row)
        result = ModelClient(store).review(context)
        if result["advice"]["verdict"] != "证据不足":
            raise SafetyStop("Synthetic insufficient-evidence check failed")
        print(json.dumps({"model": result["model"], "verdict": result["advice"]["verdict"],
                          "valid_schema": True, "usage": result["usage"], "data": "synthetic_only"}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (SafetyStop, OSError) as error:
        print(str(error) if isinstance(error, SafetyStop) else "Local configuration failed; no key was printed.", file=sys.stderr)
        sys.exit(1)
