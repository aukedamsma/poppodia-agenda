"""Reeksengeheugen (state/series.json): onthoudt terugkerende events per podium en hun prijs/aanvangstijd.

Voorbeelden van reeksen: "VroegZat" (FLUOR), "Paardcafé: …" (PAARD), "Kelderbar Open" (Vera), "Cheeky Monday" (Melkweg),
"Jazz Jam #12" (nummering wordt genegeerd). Per reeks:
  title      voorbeeldtitel
  prices     {prijsstring: aantal waarnemingen}     bv. {"€ 7,50": 9, "gratis": 1}
  times      {"HH:MM": aantal}                      aanvangstijden
  seen       aantal afzonderlijke events, first_seen, last_seen

Gebruik: events zonder prijs (of zonder tijd) krijgen de dominante waarde uit de reeks, mits die minstens
MIN_OBS keer is gezien en minstens MIN_SHARE van de waarnemingen uitmaakt. Zulke waarden zijn een schatting
en worden gemarkeerd (Event.price_est / Event.time_est), zodat de site ze herkenbaar toont (~ € 7,50).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from taxonomy import NOISE_PAREN, NOISE_SUFFIX, _fold

ROOT = Path(__file__).parent
STATE = ROOT / "state"
PATH = STATE / "series.json"
SEEN_PATH = STATE / "series_seen.json"
TODAY = date.today().isoformat()
MIN_OBS = 2
MIN_SHARE = 0.6

_EDITION = re.compile(r"\s*(#|nr\.?|no\.?|vol\.?|volume|editie|edition|deel|part|aflevering|afl\.?|week|ronde)\s*\d+\b", re.I)
_DATE = re.compile(r"\b(\d{1,2}[-/. ](\d{1,2}|jan|feb|mrt|maart|apr|mei|jun|jul|aug|sep|sept|okt|nov|dec)[a-z]*([-/. ]\d{2,4})?|20\d\d|'\d\d)\b", re.I)
_SEASON = re.compile(r"\b(voorjaar|najaar|zomer|winter|lente|herfst|spring|summer|autumn|fall|winter|seizoen|season)\b", re.I)
_MONTHS = re.compile(r"\b(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|january|february|march|april|may|june|july|august|october)\b", re.I)


def series_key(venue: str, title: str) -> str | None:
    """Sleutel voor een reeks: podium + genormaliseerde titel zonder nummering/datum/editie.
    "Cheeky Monday: MURDOCK!" -> "melkweg|cheeky monday"; "Jazz Jam #14" -> "…|jazz jam"."""
    t = NOISE_PAREN.sub("", title or "")
    t = NOISE_SUFFIX.sub("", t)
    if ":" in t:
        left = t.split(":", 1)[0].strip()
        if 3 <= len(left) <= 40:
            t = left
    t = _EDITION.sub("", t)
    t = _DATE.sub("", t)
    t = _MONTHS.sub("", t)
    t = _SEASON.sub("", t)
    t = re.sub(r"\b\d+\b", "", t)
    t = _fold(t).strip(" -–—:|,.!#")
    if len(t) < 3:
        return None
    return f"{_fold(venue)}|{t}"


def load() -> tuple[dict, set]:
    db = {}
    if PATH.exists():
        try:
            db = json.loads(PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            db = {}
    seen = set(json.loads(SEEN_PATH.read_text())) if SEEN_PATH.exists() else set()
    return db, seen


def save(db: dict, seen: set) -> None:
    STATE.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(db, ensure_ascii=False, indent=0), encoding="utf-8")
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-40000:]))


def record(db: dict, seen: set, venue: str, title: str, event_key: str, price: str | None, start: str) -> None:
    """Neem een waargenomen (echte, niet geschatte) prijs/tijd op. Elk event telt één keer."""
    key = series_key(venue, title)
    if not key or event_key in seen:
        return
    time = start[11:16] if len(start) > 10 and start[11:16] != "00:00" else None
    if not price and not time:
        return
    seen.add(event_key)
    s = db.setdefault(key, {"title": title, "prices": {}, "times": {}, "seen": 0, "first_seen": TODAY, "last_seen": TODAY})
    s["seen"] += 1
    s["last_seen"] = TODAY
    if price:
        s["prices"][price] = s["prices"].get(price, 0) + 1
    if time:
        s["times"][time] = s["times"].get(time, 0) + 1


def _dominant(counts: dict) -> str | None:
    total = sum(counts.values())
    if total < MIN_OBS:
        return None
    val, n = max(counts.items(), key=lambda x: x[1])
    return val if n / total >= MIN_SHARE and n >= MIN_OBS else None


def guess(db: dict, venue: str, title: str) -> tuple[str | None, str | None]:
    """(prijs, tijd) uit het reeksengeheugen, of (None, None)."""
    key = series_key(venue, title)
    s = db.get(key) if key else None
    if not s:
        return None, None
    return _dominant(s.get("prices", {})), _dominant(s.get("times", {}))
