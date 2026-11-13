"""PS-135: print the target catalog audit (photonscript/shared/catalog_audit).

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\check_catalog.py [--user <data_dir>]

--user adds <data_dir>/user_catalog.json to the rows checked. Exit code 1
when there are findings, 0 when the catalog is clean.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photonscript.shared import astronomy  # noqa: E402
from photonscript.shared.catalog_audit import (audit,  # noqa: E402
                                               format_findings)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="data dir holding user_catalog.json")
    args = ap.parse_args()
    rows = list(astronomy.SEASONAL_TARGETS)
    if args.user:
        p = Path(args.user) / "user_catalog.json"
        if p.exists():
            rows += json.loads(p.read_text(encoding="utf-8")).get("targets", [])
    findings = audit(rows)
    print(format_findings(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
