#!/usr/bin/env python3
"""Bewaart de eventpagina's van tests/groundtruth.json als fixtures (tests/fixtures/<n>.html.gz + index.json), zodat
parserwijzigingen offline tegen echte pagina's getest kunnen worden (tests/test_fixtures.py) zonder ze opnieuw op te halen.

Draai lokaal (heeft netwerk nodig):  python3 tools/snapshot_fixtures.py [--only Venue ...] [--refresh]
Zonder --refresh worden bestaande fixtures niet overschreven. Alleen items met een `url` worden bewaard.
De fixtures zijn een momentopname: bij een verlopen event verdwijnt de pagina; de test slaat items zonder fixture over."""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from net import get  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def main(argv: list[str]) -> int:
    refresh = "--refresh" in argv
    only = [a for a in argv if not a.startswith("--")]
    FIX.mkdir(exist_ok=True)
    idx_path = FIX / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {}
    items = json.loads((ROOT / "tests" / "groundtruth.json").read_text(encoding="utf-8"))["items"]
    venues = {v["name"]: v for v in yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8"))}
    saved = skipped = failed = 0
    for it in items:
        url = it.get("url")
        if not url or (only and it["venue"] not in only):
            continue
        key = hashlib.sha1(url.encode()).hexdigest()[:12]
        fn = FIX / f"{key}.html.gz"
        if fn.exists() and not refresh:
            skipped += 1
            continue
        v = venues.get(it["venue"], {})
        try:
            r = get(url, delay=float(v.get("crawl_delay", 1.0)))
            html = r.text
        except Exception as ex:  # noqa: BLE001
            print(f"  ✕ {it['venue']}: {type(ex).__name__} {str(ex)[:80]}")
            failed += 1
            continue
        fn.write_bytes(gzip.compress(html.encode("utf-8")))
        index[key] = {"venue": it["venue"], "url": url, "fetched": time.strftime("%Y-%m-%d"), "bytes": len(html)}
        saved += 1
        print(f"  ✓ {it['venue']}: {len(html) // 1024} kB")
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{saved} bewaard, {skipped} al aanwezig, {failed} mislukt -> {FIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
