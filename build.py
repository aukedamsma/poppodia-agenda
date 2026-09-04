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
