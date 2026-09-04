#!/usr/bin/env python3
"""Bouwt docs/index.html en docs/agenda.ics uit data/events.json en data/report.json."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

events = json.loads((ROOT / "data" / "events.json").read_text(encoding="utf-8"))
report = json.loads((ROOT / "data" / "report.json").read_text(encoding="utf-8"))

# Oudere data (van vóór de genre-taxonomie) ter plekke aanvullen, zodat een lokale build altijd
# hoofdgenres, eventtype en artiesten toont. Nieuwe runs van fetch.py leveren dit al mee.
if events and "genre_norm" in events[0] and "subgenres" not in events[0]:
    from taxonomy import normalize_subgenres, subgenre_label, subgenre_group
    for e in events:
        e["subgenres"], _ = normalize_subgenres(e.get("genres") or [], e.get("genre_norm") or [])
    report["subgenre_labels"] = {k: subgenre_label(k) for e in events for k in e["subgenres"]}
    report["subgenre_groups"] = {k: subgenre_group(k) for e in events for k in e["subgenres"]}
if events and "genre_norm" not in events[0]:
    from taxonomy import classify_kind, extract_artists, normalize_genres, price_number, _taxonomy, normalize_subgenres, subgenre_label, subgenre_group
    for e in events:
        e["artists"] = extract_artists(e["title"], e.get("subtitle"))
        e["genre_norm"], _ = normalize_genres(e.get("genres") or [], e["title"], e.get("subtitle") or "")
        e["kind"] = classify_kind(e["title"], e.get("subtitle"), e.get("genres") or [], e["genre_norm"], e["start"])
        e["price_num"] = price_number(e.get("price"))
        e["free"] = e["price_num"] == 0.0
        e.setdefault("section", "poppodium")
        e["subgenres"], _ = normalize_subgenres(e.get("genres") or [], e["genre_norm"])
    report["subgenre_labels"] = {k: subgenre_label(k) for e in events for k in e.get("subgenres", [])}
    report["subgenre_groups"] = {k: subgenre_group(k) for e in events for k in e.get("subgenres", [])}
    if "genre_groups" not in report:
        groups, _ = _taxonomy()
        report["genre_groups"] = {k: v.get("label", k) for k, v in groups.items()}

# hoofdgenres altijd volgens de actuele taxonomie (labels) en hernoemde groepen in oudere data omzetten
from taxonomy import _taxonomy as _tax
from artists import GROUP_RENAMES
report["genre_groups"] = {k: v.get("label", k) for k, v in _tax()[0].items()}
for e in events:
    if e.get("genre_norm"):
        seen_g = []
        for g in e["genre_norm"]:
            g2 = GROUP_RENAMES.get(g, g)
            if g2 not in seen_g:
                seen_g.append(g2)
        e["genre_norm"] = seen_g

# --- HTML ---------------------------------------------------------------------
tpl = (ROOT / "template.html").read_text(encoding="utf-8")
def safe_json(obj) -> str:
    # '</script>' in data mag de pagina niet breken
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
html = tpl.replace("__EVENTS__", safe_json(events)).replace("__REPORT__", safe_json(report))
(DOCS / "index.html").write_text(html, encoding="utf-8")
(DOCS / ".nojekyll").write_text("")

# --- iCalendar ----------------------------------------------------------------
def ics_escape(s: str) -> str:
    return re.sub(r"([,;\\])", r"\\\1", (s or "")).replace("\n", "\\n")

def fold(line: str) -> str:
    out, enc = [], line.encode("utf-8")
    while len(enc) > 73:
        cut = 73
        while cut > 0 and (enc[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(enc[:cut].decode("utf-8")); enc = b" " + enc[cut:]
    out.append(enc.decode("utf-8"))
    return "\r\n".join(out)

lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//poppodia-agenda//NL", "CALSCALE:GREGORIAN",
         "X-WR-CALNAME:Podiumagenda", "X-WR-TIMEZONE:Europe/Amsterdam", "REFRESH-INTERVAL;VALUE=DURATION:P1D"]
stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
for e in events:
    start = datetime.fromisoformat(e["start"])
    all_day = e["start"][11:16] in ("", "00:00")
    end = datetime.fromisoformat(e["end"]) if e.get("end") else start + timedelta(hours=3)
    if end <= start:
        end = start + timedelta(hours=3)
    uid = re.sub(r"[^A-Za-z0-9]", "", e["venue"] + e["url"])[-60:] + "@poppodia-agenda"
    summary = f"{e['title']} — {e['venue']}" + (f" [{e['status']}]" if e.get("status") else "")
    desc = " · ".join(x for x in [e.get("subtitle"), ", ".join(e.get("genres") or []), e.get("price")] if x)
    lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
              (f"DTSTART;VALUE=DATE:{start:%Y%m%d}" if all_day else f"DTSTART;TZID=Europe/Amsterdam:{start:%Y%m%dT%H%M%S}"),
              (f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}" if all_day else f"DTEND;TZID=Europe/Amsterdam:{end:%Y%m%dT%H%M%S}"),
              fold(f"SUMMARY:{ics_escape(summary)}"), fold(f"LOCATION:{ics_escape(e['venue'] + ', ' + e['city'])}"),
              fold(f"DESCRIPTION:{ics_escape(desc + (chr(10) if desc else '') + e['url'])}"), f"URL:{e['url']}",
              ("STATUS:CANCELLED" if e.get("status") == "afgelast" else "STATUS:CONFIRMED"), "END:VEVENT"]
lines.append("END:VCALENDAR")
(DOCS / "agenda.ics").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

print(f"docs/index.html en docs/agenda.ics gebouwd: {len(events)} events")
