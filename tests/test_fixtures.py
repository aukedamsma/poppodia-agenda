#!/usr/bin/env python3
"""Parsertest tegen echte, bewaarde eventpagina's (tests/fixtures/, gemaakt met tools/snapshot_fixtures.py).
Per fixture wordt fetch_detail offline uitgevoerd (netwerk vervangen door de fixture) en vergeleken met het bijbehorende
groundtruth-item: dag, tijd en prijs. Zo is een parserwijziging direct te controleren op ~60 podia zonder iets op te halen.

  python3 tests/test_fixtures.py            # alle fixtures; exit 1 bij een afwijking op een gecontroleerd item
  python3 tests/test_fixtures.py Patronaat  # alleen dit podium

Baseline-items (niet met de hand gecontroleerd) tellen als 'veranderd', niet als fout."""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch  # noqa: E402
import yaml  # noqa: E402
from taxonomy import price_number  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


class _Resp:
    def __init__(self, text, url):
        self.text, self.url, self.status_code, self.ok, self.headers = text, url, 200, True, {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class _FixtureSession:
    """Serveert alleen de fixture-url; alles anders (ticketshop, API's) is offline en faalt netjes."""
    headers: dict = {}

    def __init__(self, url, html):
        self.url, self.html = url, html

    def get(self, url, **kw):
        if url.rstrip("/") == self.url.rstrip("/"):
            return _Resp(self.html, url)
        raise fetch.requests.ConnectionError(f"offline (fixture-test): {url}")

    def post(self, url, **kw):
        raise fetch.requests.ConnectionError(f"offline (fixture-test): {url}")


def run(only: list[str]) -> int:
    idx_path = FIX / "index.json"
    if not idx_path.exists():
        print("geen fixtures: draai eerst python3 tools/snapshot_fixtures.py")
        return 0
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    items = {it["url"]: it for it in json.loads((ROOT / "tests" / "groundtruth.json").read_text(encoding="utf-8"))["items"] if it.get("url")}
    venues = {v["name"]: v for v in yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8"))}
    real_session = fetch.net.SESSION
    ok = changed = failed = 0
    try:
        for key, meta in sorted(index.items(), key=lambda kv: kv[1]["venue"]):
            it = items.get(meta["url"])
            v = venues.get(meta["venue"])
            if not it or not v or (only and meta["venue"] not in only):
                continue
            if fetch.is_ticket_url(meta["url"]):
                print(f"- {meta['venue']:22} {it['title'][:35]:35} ticketshop-pagina (geen eventpagina): niet getest, prijs/tijd komen uit de shop-API")
                continue
            html = gzip.decompress((FIX / f"{key}.html.gz").read_bytes()).decode("utf-8", "replace")
            fetch.net.SESSION = _FixtureSession(meta["url"], html)
            fetch._STAGER_SESSIONS.clear()
            ev = fetch.fetch_detail(v, meta["url"], {}, None)
            problems = []
            if ev is None:
                problems.append("geen event uit de pagina")
            else:
                if ev.start[:10] != it["date"]:
                    problems.append(f"dag {ev.start[:10]} i.p.v. {it['date']}")
                if it.get("time") and ev.start[11:16] != it["time"]:
                    problems.append(f"tijd {ev.start[11:16]} i.p.v. {it['time']}")
                if it.get("price") is not None:
                    got = price_number(fetch.display_price(fetch.normalize_price(fetch.canonical_price(ev.price))) or "")
                    if got is None or abs(got - float(it["price"])) > 0.01:
                        problems.append(f"prijs {ev.price} i.p.v. {it['price']}")
            tag = "baseline" if it.get("baseline") else "GECONTROLEERD"
            if problems:
                if it.get("baseline"):
                    changed += 1
                else:
                    failed += 1
                print(f"{'~' if it.get('baseline') else '✕'} {meta['venue']:22} {it['title'][:35]:35} {'; '.join(problems)}  [{tag}]")
            else:
                ok += 1
                print(f"✓ {meta['venue']:22} {it['title'][:35]:35} {ev.start[11:16]} {ev.price or '-'}")
    finally:
        fetch.net.SESSION = real_session
    print(f"\n{ok} kloppen, {changed} baseline-items veranderd, {failed} gecontroleerde items FOUT")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
