"""extract.py — lezen van tekst en JSON: datums (parse_dt, to_local, date_from_url), tijden en prijzen uit vrije tekst
(extract_from_text_ex met tijd- en prijsherkomst), servicekosten, prijsweergave, JSON-LD-blokken, titelsleutels voor dedupe.
Pure functies zonder netwerk; dit is de laag waar de meeste parserlessen uit README.md zijn vastgelegd."""
from __future__ import annotations

import json
import re
from typing import NamedTuple
from datetime import datetime, date, timedelta, timezone

from dateutil import parser as dtparser

from taxonomy import _fold, NOISE_PAREN
from common import Event, NL_MONTHS, TODAY, clean


def normalize_price(p: str | None) -> str | None:
    """Eén consequente prijsweergave. Bedragen onder € 1 (0, 0,00, 0,28 servicekosten) zijn 'gratis'."""
    if not p:
        return None
    low = p.lower()
    if "gratis" in low or re.search(r"\bfree\b", low):
        return "gratis"
    m = re.search(r"(\d{1,3})(?:[.,](\d{1,2}))?", p)
    if m:
        val = float(m.group(1)) + (float("0." + m.group(2)) if m.group(2) else 0.0)
        if val < 1:
            return "gratis"
    return p


def canonical_price(p: str | None) -> str | None:
    """Ruwe prijstekst uit een bron (Tribe 'cost': "Voorverkoop €21,00 Deurticket €26,00") naar één bedrag volgens dezelfde
    regels als eventpagina's: online/voorverkoop-prijs, inclusief servicekosten; 'uitverkocht' en 'gratis' blijven."""
    if not p:
        return None
    p = re.sub(r"\$\s?(?=\d)", "€ ", p)   # FLUOR (Tribe met verkeerd valutasymbool): "$18,50" is € 18,50
    p = re.sub(r"^(€ ?\d+,\d)$", r"\g<1>0", p.strip())   # "€ 19,5" (oude cache) -> "€ 19,50"
    if re.fullmatch(r"€ ?\d+(?:[.,]\d{1,2})?|gratis|uitverkocht|~.*", p.strip()):
        return p
    low, fee = _strip_service_fee(p.lower())
    pm = _pick_price(low)
    if pm:
        return fmt_price(_add_fee(pm, fee))
    if re.search(r"\bgratis\b|\bfree\b", low):
        return "gratis"
    return p


def display_price(p: str | None) -> str | None:
    """Eén weergave voor elke prijs: altijd een komma, twee decimalen (€ 24,50), hele bedragen zonder decimalen (€ 24);
    een paar cent onder/boven een heel bedrag (€ 19,98, € 20,02) wordt afgerond op het hele getal. 'gratis' en
    'uitverkocht' blijven staan; tekst zonder herkenbaar bedrag ook."""
    if not p:
        return p
    t = p.strip()
    if t in ("gratis", "uitverkocht") or t.startswith("~"):
        return t
    m = re.fullmatch(r"€?\s?(\d{1,4})(?:[.,](\d{1,2}))?\s?(?:€|euro)?", t, re.I)
    if not m:
        return p
    val = float(m.group(1)) + (float("0." + m.group(2).ljust(2, "0")) if m.group(2) else 0.0)
    if abs(val - round(val)) <= 0.04:
        return f"€ {int(round(val))}"
    return f"€ {val:.2f}".replace(".", ",")


def fmt_price(p) -> str | None:
    if p in (None, "", 0, "0", "0.00", "0,00"):
        return None
    if isinstance(p, (int, float)):
        return f"€ {p:.2f}".replace(".", ",").removesuffix(",00")   # 24.5 -> € 24,50 (niet € 24,5)
    p = clean(str(p)) or ""
    if re.fullmatch(r"\d+([.,]\d{1,2})?", p):
        return "€ " + p.replace(".", ",").removesuffix(",00")
    return p or None


def _ampm_to_24h(text: str) -> str:
    """"8:30 PM" / "8 pm" / "12 AM" -> "20:30" / "20:00" / "00:00" (Engelstalige podiumsites en ticketshops)."""
    def rep(m):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower().replace(".", "")
        if h > 12 or mi > 59:
            return m.group(0)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mi:02d}"
    return re.sub(r"\b(\d{1,2})(?::(\d{2}))?\s?([ap]\.?m\.?)\b", rep, text, flags=re.I)


def parse_dt(value, default_year: int | None = None) -> datetime | None:
    """Zet allerlei datumvormen om naar datetime. Geeft None bij mislukking."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if value > 1e12:  # milliseconden
                value = value / 1000
            return to_local(datetime.fromtimestamp(value, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    s = _ampm_to_24h(str(value).strip())
    if not s:
        return None
    # ISO of bijna-ISO
    try:
        dt = dtparser.isoparse(s)
        if dt.tzinfo is not None:
            # naar Nederlandse lokale tijd (benadering: CET/CEST via systeem-tz van de runner is UTC,
            # daarom expliciet +1/+2 aan de hand van zomertijd)
            dt = to_local(dt)
        return dt
    except (ValueError, OverflowError):
        pass
    # Nederlandse datum: "vr 4 sep 2026", "donderdag 29 april 2027 20:30", "do 25.03.27", "04-09-2026"
    low = s.lower()
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\D+(\d{1,2})[:.](\d{2}))?", low)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})(?:\D+(\d{1,2})[:.](\d{2}))?", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
    # "Sun18.10", "Sun 18 . 10", "Sat05.09just added" (Cinetol), "05.09 zaterdag" (Grenswerk): dag.maand met weekdag ervoor of erna
    m = re.fullmatch(r"(?:[a-z]{2,9}\.?\s*)?(\d{1,2})\s?[./]\s?(\d{1,2})\.?(?:\s+(\d{1,2})[:.](\d{2}))?(?:\s*(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|monday|tuesday|wednesday|thursday|friday|saturday|sunday|ma|di|wo|do|vr|za|zo|mon|tue|wed|thu|fri|sat|sun)\.?)?(?:\s*(?:just added|nieuw|new|uitverkocht|sold out)\s*)?", low)
    if m and 1 <= int(m.group(2)) <= 12:
        d, mo = int(m.group(1)), int(m.group(2))
        hh, mm = (int(m.group(3)), int(m.group(4))) if m.group(3) else (0, 0)
        try:
            dt = datetime(default_year or TODAY.year, mo, d, hh, mm)
        except ValueError:
            return None
        if dt.date() < TODAY - timedelta(days=30):
            dt = dt.replace(year=dt.year + 1)
        return dt
    m = re.search(r"(\d{1,2})\s+([a-z]+)\.?\s*(\d{4})?(?:\D*?(\d{1,2})[:.](\d{2})(?!\d))?", low)
    if m and m.group(2) in NL_MONTHS:
        d, mo = int(m.group(1)), NL_MONTHS[m.group(2)]
        y = int(m.group(3)) if m.group(3) else (default_year or TODAY.year)
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            dt = datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
        if not m.group(3) and dt.date() < TODAY - timedelta(days=30):
            dt = dt.replace(year=dt.year + 1)
        return dt
    # laatste redmiddel (dateutil, fuzzy) alleen als er echt een maand of volledige datum in staat: bij "Sun18.10" vulde
    # dateutil de ontbrekende maand met de huidige maand aan (Cinetol: alle events een maand te vroeg)
    if not (re.search(r"\b(" + "|".join(NL_MONTHS) + r")\b", low) or re.search(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}|\d{4}-\d{2}-\d{2}", low)):
        return None
    try:
        return dtparser.parse(s, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def to_local(dt: datetime) -> datetime:
    """UTC/tz-aware -> naive Nederlandse tijd (CET/CEST).
    Een offset van +01:00 of +02:00 is een Nederlandse kloktijd: die wordt letterlijk genomen, ook als de offset voor die
    datum fout is. Sites zetten de offset vaak vast in het sjabloon (Nieuwe Nor: "2026-11-07T22:00+02:00" in november,
    terwijl het dan +01:00 is; de pagina zegt 22:00) — omrekenen zou dan een uur ernaast zitten."""
    off = dt.utcoffset()
    if off is not None and off in (timedelta(hours=1), timedelta(hours=2)):
        return dt.replace(tzinfo=None)
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    # zomertijd: laatste zondag maart 01:00 UTC t/m laatste zondag oktober 01:00 UTC
    y = utc.year
    def last_sunday(month):
        d = date(y, month, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    start = datetime.combine(last_sunday(3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(last_sunday(10), datetime.min.time()) + timedelta(hours=1)
    offset = 2 if start <= utc < end else 1
    return utc + timedelta(hours=offset)


def date_from_url(url: str) -> datetime | None:
    """Datum uit de slug: …-04-09-2026, …-04-09-26 of …-2026-09-04 (ISO eerst, anders wordt 2026-09-18 als 26-09-18 gelezen)."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:/|$|[-_])", url) or re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", url)   # Nieuwe Nor: /programma/2026/10/03/slug
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})(?:/|$)", url) or re.search(r"(\d{2})-(\d{2})-(\d{2})(?:/|$)", url)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def jsonld_blocks(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            try:
                j = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        items = j if isinstance(j, list) else [j]
        for it in items:
            if isinstance(it, dict):
                if "@graph" in it and isinstance(it["@graph"], list):
                    out.extend(x for x in it["@graph"] if isinstance(x, dict))
                elif "ItemList" in str(it.get("@type", "")) and isinstance(it.get("itemListElement"), list):
                    # ItemList van ListItems met een Event als item (Stager-ticketshops: <podium>.stager.co/shop/default/events)
                    for li in it["itemListElement"]:
                        if isinstance(li, dict):
                            out.append(li.get("item") if isinstance(li.get("item"), dict) else li)
                else:
                    out.append(it)
    return out


def jsonld_events(html: str) -> list[dict]:
    return [n for n in jsonld_blocks(html) if "Event" in str(n.get("@type", ""))]


def _strip_tz(val):
    """Voor podia die lokale tijd met een verkeerde tijdzone publiceren (bv. "22:00:00+00:00" voor 22:00 NL)."""
    return re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", str(val).strip()) if isinstance(val, str) else val


MONTH_RE = r"(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|jan|feb|mrt|apr|jun|jul|aug|sep|sept|okt|nov|dec|january|february|march|may|june|july|october|mar|oct)"
WEEKDAY_RE = r"(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|ma|di|wo|do|vr|za|zo|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)"


_JSON_PRICE = re.compile(r'"(?:price|prijs|ticketPrice|ticket_price|minPrice|lowestPrice|priceFrom|price_from)"\s*:\s*"?(€?\s?\d{1,3}(?:[.,]\d{1,2})?|gratis|free)"?', re.I)


LINEUP_NOISE = re.compile(r"korting|\bjaar\b|ticket|info|praktisch|bereikbaar|parkeer|toegankelijk|faq|vragen|huisregels|garderobe|eten|drinken|route|membership|\blid\b|leden|cadeau|voucher|rolstoel|gehoor|zaal open|deuren|aanvang|meer weten|programma|nieuwsbrief", re.I)


def clean_lineup(names: list[str], title: str = "") -> list[str]:
    """Line-up-namen opschonen: geen ticket-/praktische-info-koppen (Tivoli-accordeon: 'Onder 30 jaar', 'Koop met Korting')."""
    out = []
    for x in names:
        x = clean(x)
        if not x or len(x) < 2 or len(x) > 60 or x.lower() == (title or "").lower() or LINEUP_NOISE.search(x):
            continue
        if x not in out:
            out.append(x)
    return out[:20]


def _decoded(html: str) -> str:
    """HTML plus een URL-gedecodeerde variant: sommige thema's (Vorstin) zetten JSON URL-encoded in een attribuut."""
    out = html
    if html.count("%22") > 20:
        from urllib.parse import unquote
        out += "\n" + unquote(html)
    if html.count("&quot;") > 20:
        import html as _html   # JSON in attributen (Het Podium: onclick="gtm.push({&quot;price&quot;:&quot;21.50&quot;})")
        out += "\n" + _html.unescape(html)
    return out


_ISO_TAIL = r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)"
_JSON_TIME_START = re.compile(r'"(?:program_start|programme_start|start_time|starttime|startTime|aanvang|showtime|show_time)"\s*:\s*"?(\d{12}|' + _ISO_TAIL + r'|\d{1,2}:\d{2})"?', re.I)
_JSON_TIME_DOORS = re.compile(r'"(?:door_open|doors_open|doorsOpen|doorTime|door_time|deuren|doors)"\s*:\s*"?(\d{12}|' + _ISO_TAIL + r'|\d{1,2}:\d{2})"?', re.I)


def _hm_from_json(val: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\d{12}", val)
    if m:
        return int(val[8:10]), int(val[10:12])
    if re.search(r"Z$|[+-]\d{2}:?\d{2}$", val):
        # tijdzone in de waarde (Melkweg "startTime":"2026-09-08T17:30:00.000000Z" = 19:30 Nederlandse tijd): omrekenen,
        # anders overschreef deze tekst-JSON-tijd de al correcte lijsttijd met de UTC-uren (Melkweg liep 2 uur voor)
        dt = parse_dt(val)
        return (dt.hour, dt.minute) if dt else None
    m = re.search(r"(\d{1,2}):(\d{2})$|T(\d{2}):(\d{2})", val)
    if m:
        g = [x for x in m.groups() if x is not None]
        return int(g[0]), int(g[1])
    return None


def times_from_embedded_json(html: str) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """(aanvang, deuren) uit ingebedde JSON, bv. "program_start":"202609111915","door_open":"202609111830"."""
    h = _decoded(html)
    st = next((_hm_from_json(m.group(1)) for m in _JSON_TIME_START.finditer(h)), None)
    dr = next((_hm_from_json(m.group(1)) for m in _JSON_TIME_DOORS.finditer(h)), None)
    return st, dr


def price_from_embedded_json(html: str) -> str | None:
    """Prijs uit ingebedde JSON (Next.js/Nuxt-data) als de zichtbare tekst hem niet heeft (Melkweg: "price":"€ 24,05").
    Voorkeur voor het ticket dat als 'primary' is gemarkeerd, anders de laagste prijs > 0."""
    html = _decoded(html)
    hits = [(m.start(), m.group(1)) for m in _JSON_PRICE.finditer(html)]
    if not hits:
        return None
    for pos, val in hits:
        if re.search(r'"primary"\s*:\s*true', html[pos:pos + 200]):
            return fmt_price(val)
    nums = []
    for _, val in hits:
        if re.search(r"gratis|free", val, re.I):
            continue
        m = re.search(r"\d{1,3}(?:[.,]\d{1,2})?", val)
        if m:
            nums.append((float(m.group(0).replace(",", ".")), val))
    if nums:
        return fmt_price(min(nums)[1])
    return "gratis"


class Extracted(NamedTuple):
    dt: datetime | None
    start: tuple[int, int] | None
    doors: tuple[int, int] | None
    price: str | None
    kind: str | None         # herkomst van de aanvang (zie extract_from_text_ex)
    price_kind: str | None   # herkomst van de prijs: "labeled" (context voorverkoop/tickets/entree/gratis) of "text" (los bedrag)


# rangorde van prijsherkomst: wat je nú online betaalt (ticketshop-API) gaat vóór gestructureerde brondata (lijst/JSON-LD),
# dat vóór een gelabelde tekstprijs, dat vóór een los bedrag in de tekst
PRICE_RANK = {"shop": 5, "list": 4, "jsonld": 4, "labeled": 3, "embedded": 2, "text": 1}


def _price_rank(src: str | None) -> int:
    return PRICE_RANK.get(src or "list", 4)


_SALE_CTX = re.compile(r"(?:kaartverkoop|voorverkoop|ticketverkoop|ticket ?sale|presale|pre-sale|on sale|in de verkoop|verkoop)\b.{0,40}?\b(?:start|begint|gaat|vanaf|opent|is|op)\W{0,12}(?:(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|ma|di|wo|do|vr|za|zo)\.?\W{0,3})?$", re.I | re.S)


def extract_from_text(txt: str) -> tuple[datetime | None, tuple[int, int] | None, tuple[int, int] | None, str | None]:
    """(datum, aanvangstijd, deurtijd, prijs) uit vrije tekst van een eventpagina (zie extract_from_text_ex)."""
    return tuple(extract_from_text_ex(txt)[:4])


def extract_from_text_ex(txt: str) -> Extracted:
    """(datum, aanvangstijd, deurtijd, prijs, herkomst-van-de-aanvang, herkomst-van-de-prijs) uit vrije tekst van een eventpagina.
    Datum: liefst met weekdag of 'datum:' ervoor, anders de eerste dag+maand(+jaar).
    Herkomst (tijdprovenance, bepaalt of de tekst een gestructureerde tijd mag corrigeren):
      "label"      expliciet gelabeld ("Aanvang 20:30", "20:30 start")            -> mag een lijsttijd corrigeren (binnen 3 uur)
      "after_date" tijd direct achter de datum ("vrijdag 2 oktober 2026 20:30")   -> alleen als er nog geen tijd is
      "paren"      "20:30 (Doors 19:30)"                                          -> idem
      "schedule"   eerste tijd na de deuren in een tijdschema                     -> idem, of als de lijsttijd de deurtijd is
    Prijsherkomst: "labeled" (bedrag met context voorverkoop/tickets/entree/regulier, of 'gratis') gaat vóór JSON-LD-offers;
    "text" (los bedrag zonder context) alleen als er niets beters is."""
    low = _ampm_to_24h(txt.lower())
    dt = None
    for pat in (rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b{WEEKDAY_RE}\.\s?(\d{{1,2}})\.\s?{MONTH_RE}\b\.?(?:\s?(\d{{4}}))?",   # "Do.10.Sep" (Q-factory)
                rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}}[./]\d{{1,2}}(?:[./]\d{{2,4}})?)\b",
                rf"\bdatum\W{{0,6}}(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\.?\s+(\d{{4}})\b",
                rf"\b{MONTH_RE}\s+(\d{{1,2}}),\s+(\d{{4}})\b",   # "oktober 31, 2026" (Baroeg: WordPress-datumformaat "F j, Y")
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\b\.?"):
        # de startdatum van de kaartverkoop is niet de eventdatum (Simplon: "de kaartverkoop van dit evenement start
        # donderdag 25 juni om 11:00u" -> event op 25 juni 2027)
        ms = [x for x in re.finditer(pat, low) if not _SALE_CTX.search(low[max(0, x.start() - 50): x.start()])]
        m = ms[0] if ms else None
        if m and pat.startswith(rf"\b{MONTH_RE}"):
            # WordPress-postdatum "F j, Y" (Baroeg: publicatiedatum "augustus 20, 2026" vóór de eventdatum "oktober 31, 2026
            # 19:00"): in dit formaat is de datum waar direct een tijd achter staat die van het event
            m = next((x for x in ms if re.match(r"\W{0,4}(?:om\s+)?\d{1,2}[:.u]\d{2}\b", low[x.end(): x.end() + 12])), m)
        if m:
            groups = [g for g in m.groups() if g]
            if re.fullmatch(MONTH_RE, groups[0]) and len(groups) == 3:
                groups = [groups[1], groups[0], groups[2]]   # maand-dag-jaar -> dag maand jaar
            dt = parse_dt(" ".join(groups[-3:] if len(groups) >= 3 and groups[-1].isdigit() and len(groups[-1]) == 4 else groups[-2:]))
            if dt:
                break
    # eindtijden zijn geen aanvang: Tivoli zet de waarde vóór het label ("21:00 Deuren open 21:00 Aanvang 02:00 Verwachte
    # eindtijd") waardoor "Aanvang 02:00" anders als start wordt gelezen; ook "einde 23:00" / "tot 03:00" vallen weg
    def _drop_end(mm):  # "23:00 Einde" na "Aanvang" is wél de aanvang (label ervoor): laten staan
        if re.match(r"\W{0,6}\d{1,2}[:.u]\d{2}", low[mm.end(): mm.end() + 9]):
            return mm.group(0)   # label-vóór-waarde-layout ("Aanvang 23:00 Einde 04:00"): het label hoort bij de tijd erna
        before = low[max(0, mm.start() - 22): mm.start()]
        if re.search(r"\d[:.u]\d{2}\W{0,6}(?:aanvang|start|begin|deuren|doors|open)\W{0,6}$", before):
            return " "   # waarde-vóór-label-layout ("21:00 Aanvang 02:00 Verwachte eindtijd"): 02:00 is de eindtijd
        return mm.group(0) if re.search(r"(?:aanvang|start|begin|deuren|doors|open)\W{0,6}$", before) else " "
    low = re.sub(r"\b(\d{1,2})[:.u](\d{2})\W{0,6}(?:verwachte\s+|geschatte\s+|verw\.\s*)?(?:eindtijd|einde|eind|end ?time|ends)\b", _drop_end, low)
    low = re.sub(r"\b(?:verwachte\s+|geschatte\s+)?(?:eindtijd|einde|end ?time|ends|tot|until|till|t/m)\W{0,6}(\d{1,2})[:.u](\d{2})\b", " ", low)
    def hm(pat):
        mm = re.search(pat, low)
        return (int(mm.group(1)), int(mm.group(2))) if mm else None
    start = hm(r"(?:aanvang|start|begin|show|showtime|concert|hoofdprogramma|programma|\btijd)\W{0,12}(?:om\W{0,3})?(\d{1,2})[:.u](\d{2})") or hm(r"(\d{1,2})[:.u](\d{2})\W{0,6}(?:aanvang|start|begin|showtime)\b")
    kind = "label" if start else None
    # tijd direct achter de datum zonder label ("zaterdag 5 september 2026 14:30 uur", Estrado): dat is de aanvang.
    # Dezelfde datum kan vaker op de pagina staan ("vr 02 okt … vrijdag 2 oktober 2026 20:30 uur"): elke vermelding proberen
    if not start and dt is not None:
        for mm2 in re.finditer(rf"\b(?:{WEEKDAY_RE}\.?\s+)?(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+\d{{4}})?|\b{MONTH_RE}\s+(\d{{1,2}}),\s+(\d{{4}})", low):
            gs = [g for g in mm2.groups() if g and not re.fullmatch(WEEKDAY_RE, g)]
            if gs and re.fullmatch(MONTH_RE, gs[0]) and len(gs) == 3:
                gs = [gs[1], gs[0], gs[2]]
            d2 = parse_dt(" ".join(gs))
            if not d2 or d2.date() != dt.date():
                continue
            after = low[mm2.end(): mm2.end() + 18]
            mt = re.match(r"\W{0,4}(?:om\s+)?(\d{1,2})[:.u](\d{2})\b(?!\s*-\s*\d)", after)
            if mt and int(mt.group(1)) < 24 and int(mt.group(2)) < 60:
                start, kind = (int(mt.group(1)), int(mt.group(2))), "after_date"
                break
    # "20:30 (Doors: 19:30)" (Mezz, Stager-widgets): de tijd vóór het haakje is de aanvang, die erin de deurtijd
    mp = re.search(r"(\d{1,2})[:.u](\d{2})\s*\((?:doors?|deuren|zaal open)\W{0,4}(\d{1,2})[:.u](\d{2})\)", low)
    if mp and not start:
        start, kind = (int(mp.group(1)), int(mp.group(2))), "paren"
    d1 = hm(r"(?:deuren (?:openen|gaan open)|deur|deuren|doors|zaal open|open)\W{0,14}(?:om\W{0,3})?(\d{1,2})[:.u](\d{2})")
    d2 = hm(r"(\d{1,2})[:.u](\d{2})\W{0,6}(?:zaal open|deuren open|deuren|doors)\b")
    doors = min(d1, d2) if d1 and d2 else (d1 or d2)   # "19:30 Zaal Open 20:00 Support": de vroegste is de deurtijd
    if start and doors and start[0] < 6 and doors[0] >= 17:
        start, kind = None, None   # "aanvang" vóór 06:00 terwijl de deuren 's avonds opengaan: dat was een eindtijd
    if doors and (not start or start == doors):
        # tijdschema zonder het woord 'aanvang' (Nobel: "19:00 - Deuren open 19:30 - Hakselaer 20:45 - …"):
        # de eerste tijd ná de deurtijd is de start van het programma
        dm = re.search(r"(?:deur|deuren|doors|zaal open|open)", low)
        if dm:
            after = low[dm.end():dm.end() + 160]
            later = [(int(h), int(m)) for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", after) if (int(h), int(m)) > doors and int(h) < 24 and int(m) < 60]
            if later:
                start, kind = min(later), "schedule"
    low, fee = _strip_service_fee(low)
    pm, pkind = _pick_price_ex(low)
    price = None
    if _free_mentioned(low) and not pm:
        price, pkind = "gratis", "labeled"   # gratis blijft gratis, ook met "incl. € 1,75 servicekosten" (FLUOR)
    elif pm and float(pm.replace(",", ".")) == 0:
        price, pkind = "gratis", "labeled"   # Tivoli "€ 0,-": een bedrag van nul is gratis, geen ontbrekende prijs
    elif pm:
        price = fmt_price(_add_fee(pm, fee))
        if fee:
            pkind = "labeled"   # "€ 22 + € 2,50 servicekosten": de context maakt het onmiskenbaar een ticketprijs
    return Extracted(dt, start, doors, price, kind if start else None, pkind if price else None)


_FEE_WORDS = r"(?:servicekosten|service ?fee|service ?kosten|servicetoeslag|administratiekosten|transactiekosten|reserveringskosten|booking fee|fee)"
_AMT = r"(\d{1,3}(?:[.,]\d{2})?)"


def _strip_service_fee(low: str) -> tuple[str, float]:
    """De prijs is wat je online betaalt, dus inclusief servicekosten. "€ 24,00 inclusief € 2 servicekosten" -> 24;
    "€ 22 excl. € 2,50 servicekosten" / "€ 22 + € 2,50 servicekosten" -> 24,50. Het servicekostenbedrag zelf wordt uit de
    tekst gehaald zodat het nooit als ticketprijs wordt gelezen. Alleen expliciet exclusieve bedragen worden opgeteld;
    "excl. servicekosten" zonder bedrag telt niets op (niet schatten). Geeft (tekst zonder fee-fragmenten, op te tellen fee)."""
    fee = 0.0
    def _val(v: str) -> float:
        return float(v.replace(",", "."))
    for m in re.finditer(r"(?:excl\.?|exclusief|exclusive of|\+|plus)\s*€?\s?" + _AMT + r"\s*(?:aan\s+)?" + _FEE_WORDS + r"\b", low):
        fee = max(fee, _val(m.group(1)))
    for m in re.finditer(r"(?:excl\.?|exclusief|\+|plus)\s*" + _FEE_WORDS + r"\W{0,6}(?:van\s+)?€?\s?" + _AMT, low):
        fee = max(fee, _val(m.group(1)))
    low = re.sub(r"(?:incl\.?|inclusief|inclusive of|excl\.?|exclusief|exclusive of|\+|plus)?\s*€?\s?\d{1,3}(?:[.,]\d{2})?\s*(?:aan\s+)?" + _FEE_WORDS + r"\b", " ", low)
    low = re.sub(_FEE_WORDS + r"\W{0,12}€?\s?\d{1,3}(?:[.,]\d{2})?", " ", low)
    return low, fee


def _fee_inclusive(txt: str) -> bool:
    """Staat er op de pagina een prijs 'incl. € x servicekosten'?"""
    return bool(re.search(r"incl\w*\.?\s*(?:€\s?\d[\d.,]*\s*)?(?:aan\s+)?" + _FEE_WORDS, txt, re.I))


def _add_fee(amount: str, fee: float) -> str:
    if not fee:
        return amount
    val = float(amount.replace(",", ".")) + fee
    return f"{val:.2f}"


# bedragen die geen ticketprijs zijn: lockers, garderobe, munten, parkeren, lidmaatschap, nieuwsbrief-acties (Nieuwe Nor:
# "De kosten voor gebruik van een locker zijn 3 euro" in de FAQ onder elk event -> € 3 als prijs voor 30 events)
_NON_TICKET_CTX = re.compile(r"locker|kluis|garderobe|munt|consumptie|drankje|bier|parkeer|parking|fiets|borg|lidmaatschap|ledenkaart|clubkaart|nieuwsbrief|donatie|bijdrage|vrijwillig|toeslag|verzend|bezorg|per uur|p/u|winnen|win ", re.I)
_NON_TICKET_AFTER = re.compile(r"\d[\d,.]*\s?(?:€|euro)?\s?(?:,-)?\s*(?:voor|per|p\.p\.\s+voor)\s+(?:een\s+|de\s+|het\s+|1\s+)?(?:locker|kluis|garderobe|munt|consumptie|drankje|parkeer|parking|fiets|uur|nieuwsbrief)", re.I)
_NOT_FREE_CTX = re.compile(r"kans op|winnen|win |verloting|parkeer|parking|wifi|garderobe|locker|kluis|water|nieuwsbrief|fietsenstalling|oordop|app\b|download|verzend", re.I)


def _free_mentioned(low: str) -> bool:
    """Staat er 'gratis'/'free' over de toegang — niet over gratis tickets winnen, gratis parkeren, gratis wifi enz."""
    for m in re.finditer(r"\bgratis\b|\bfree\b", low):
        ctx = low[max(0, m.start() - 20): m.end() + 12]
        if not _NOT_FREE_CTX.search(ctx):
            return True
    return False


_DISCOUNT_CTX = re.compile(r"leden|members?|lid\b|cjp|student|scholier|jeugd|jongeren|kinderen|kids|t/m \d+ jaar|tot en met \d+|65\+|vrienden|donateur|korting|reduced|early\b|deurprijs|dagkassa|avondkassa|deurverkoop|aan de deur|at the door|door ?sale|\bdoor\s*:|deur\s*:|late\b", re.I)
_PREFERRED_CTX = re.compile(r"regulier|regular|normaal|standaard|voorverkoop|vvk|presale|pre-?sale|tickets?|entree|kaarten", re.I)


def _pick_price(low: str) -> str | None:
    return _pick_price_ex(low)[0]


def _pick_price_ex(low: str) -> tuple[str | None, str | None]:
    """De reguliere voorverkoopprijs uit tekst met meerdere bedragen, plus herkomst ("labeled" als het gekozen bedrag een
    ticketcontext had, anders "text"). So What!: "Leden: € 5,00 Regulier: € 10,00 Voorverkoop Regulier: € 8,00" -> € 8,00
    (niet de ledenprijs); De Piek: "Presale € 23,00 | Tickets € 25,00" -> € 23,00.
    Bedragen met een kortings- of deurprijscontext (leden, CJP, jeugd, dagkassa) vallen af zolang er andere zijn;
    daarna wint het eerste bedrag met een 'gewone' context, anders het eerste bedrag."""
    raw = []
    # "Stage 3 € 15,50" (Patronaat: zaalnaam vóór de prijs): "3 €" is geen bedrag als het €-teken zelf een bedrag inleidt
    # "15 euro 25+" (Bitterzoet: leeftijd achter de prijs) blijft wél 15: cijfers gevolgd door + zijn geen bedrag
    for pat in (r"€\s?(\d{1,3}(?:[.,]\d{2})?)(?:,-)?", r"(\d{1,3}(?:[.,]\d{2})?)\s?(?:€|(?<![a-z])euro\b)(?!\s?\d+(?:[.,]\d{2})?\b(?!\+))",
                r"(?<![a-z])euro?\b\s?(\d{1,3}(?:[.,]\d{2})?)(?!\+)(?:,-)?", r"(?:tickets?|entree|kaarten|prijs|vvk|voorverkoop)\W{0,12}(\d{1,3}[.,]\d{2})\b"):
        for m in re.finditer(pat, low):
            # positie van het bedrag zelf, niet van het patroon: bij "voorverkoop € 18,50" hoort 'voorverkoop' bij de context
            raw.append((m.start(1), m.end(), m.group(1)))
    if not raw:
        return None, None
    raw.sort()
    cands = []
    prev_end = None
    for start, end, val in raw:
        if cands and start < cands[-1][3]:
            continue  # zelfde bedrag, door twee patronen gevonden
        # context = het label direct vóór dit bedrag (tot het vorige bedrag): "Door: €16,00 Early: €12,00 Regular: €14,50"
        lo = max(0, start - 28, prev_end if prev_end is not None else 0)
        ctx = low[lo:start]
        # label ná het bedrag ("€ 19,95 vvk, € 24,95 deur", Iduna): telt als context als er vóór het bedrag geen label staat
        post = re.split(r"€|\d{1,3}[.,]\d{2}", low[end:end + 16])[0]
        if not (_DISCOUNT_CTX.search(ctx) or _PREFERRED_CTX.search(ctx)) and (_DISCOUNT_CTX.search(post) or _PREFERRED_CTX.search(post)):
            ctx = ctx + " " + post
        cands.append((start, val, ctx, end))
        prev_end = end
    cands = [(a, b, c) for a, b, c, _ in cands]
    # geen ticketprijs: bedrag met locker/garderobe/munt/parkeer-context (ook ná het bedrag: "3 euro voor een locker")
    cands = [c for c in cands if not (_NON_TICKET_CTX.search(re.split(r"[.!?|]", c[2])[-1][-24:]) or _NON_TICKET_AFTER.match(low[c[0]: c[0] + 60]))]
    if not cands:
        return None, None
    regular = [c for c in cands if not _DISCOUNT_CTX.search(c[2])]
    pool = regular or cands
    # de prijs van nú online een gewoon ticket bestellen: voorverkoop/online gaat vóór 'regulier' (kan dagkassa zijn), dat vóór de rest
    online = [c for c in pool if re.search(r"voorverkoop|vvk|presale|pre-?sale|online", c[2], re.I)]
    preferred = online or [c for c in pool if _PREFERRED_CTX.search(c[2])]
    if preferred:
        return preferred[0][1], "labeled"
    return pool[0][1], "text"


_TICKET_URL = re.compile(r"stager\.co|tickets?\.|shop\.|eventix|paylogic|ticketmaster|weeztix|tixly|yourticketprovider|cm\.com|ticketswap|eventbrite|pop-agenda", re.I)


def is_ticket_url(u: str | None) -> bool:
    return bool(u and _TICKET_URL.search(u))


def _richness(e: Event) -> int:
    return len(e.genres) * 2 + (3 if e.price else 0) + (3 if e.start[11:16] not in ("", "00:00") else 0) + len(e.lineup) + (1 if e.subtitle else 0)


def _title_key(t: str) -> str:
    t = NOISE_PAREN.sub("", t or "")
    t = re.sub(r"\b(uitverkocht|sold out|afgelast|verplaatst|nieuwe datum|support|presents?|live|concert|tour|show|20\d\d)\b", " ", t, flags=re.I)
    t = re.sub(r"['’`´\"“”.,:;!?+&]", "", t)   # 90's / 90’s / 90s
    return re.sub(r"\s+", " ", _fold(t)).strip()


def _same_event(a: Event, b: Event, strict: bool = False) -> bool:
    """Zelfde event op dezelfde dag bij hetzelfde podium? Titels gelijk, of de ene bevat de andere ("In The Flesh?" vs
    "In The Flesh - The Dutch Pink Floyd"), of ze delen het grootste deel van hun woorden, of — als de titels niets
    gemeen hebben (site: "Zeeuwse coverbands", ticketshop: "Band on the Run") — beide hebben dezelfde aanvangstijd."""
    ta, tb = _title_key(a.title), _title_key(b.title)
    if ta and tb:
        if ta == tb or ta in tb or tb in ta:
            return True
        wa, wb = set(ta.split()), set(tb.split())
        common = wa & wb
        # gedeelde woorden moeten het grootste deel van BEIDE titels zijn: "Bnnyhunna • Haarlem Vinyl Fest" en
        # "Chef'Special • Haarlem Vinyl Fest" (Patronaat) zijn twee events, "Lily Fitts" en "Lily Fitts + Family Stereo" één
        if len(common) >= 2 and len(common) >= max(len(wa), len(wb)) * 0.75:
            return True
    if strict:
        return False   # over podia heen (coproducties) telt alleen de titel; zelfde tijd is geen bewijs
    sa, sb = a.start[11:16], b.start[11:16]
    if sa and sb and sa not in ("", "00:00") and sb not in ("", "00:00"):
        ha, ma = int(sa[:2]), int(sa[3:]); hb, mb = int(sb[:2]), int(sb[3:])
        if abs((ha * 60 + ma) - (hb * 60 + mb)) <= 30 and {a.source, b.source} & {"stager", "jsonld", "flight_json"} and a.source != b.source:
            return True   # zelfde tijd, verschillende bronnen (site vs ticketshop): één event met twee namen
    return False
