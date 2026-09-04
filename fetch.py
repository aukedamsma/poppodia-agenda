#!/usr/bin/env python3
"""
Haalt de concertagenda's van poppodia op en schrijft ze naar data/events.json.

Per podium (venues.yaml) worden strategieën geprobeerd, van schoon naar ruw:
  jsonld        schema.org Event-objecten op de agendapagina zelf
  embedded      JSON in de pagina (__NEXT_DATA__, Angular ng-state, andere application/json)
  tribe         The Events Calendar REST API (/wp-json/tribe/events/v1/events)
  wp_event      WordPress REST API voor een event-posttype (/wp-json/wp/v2/<type>)
  jsonld_detail eventlinks op de agendapagina volgen en per eventpagina de JSON-LD lezen (gecached)
  html          CSS-selectors uit venues.yaml

Een falend podium blokkeert nooit de rest; de uitkomst per podium staat in data/report.json.
"""
from __future__ import annotations

import json
from collections import Counter
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import artists as artistdb
import series as seriesdb
from taxonomy import (normalize_tag, classify_kind_ex, extract_artists, normalize_genres, price_number, group_label, _taxonomy, genre_hints, artist_key,
                      normalize_subgenres, learn_subgenres, promote_subgenres, learn_kinds, promote_kinds, subgenre_label, subgenre_group, _fold, NOISE_PAREN)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = ROOT / "state"
DATA.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)

UA = "poppodia-agenda/1.0 (persoonlijke concertagenda; 1 run per dag; contact via github.com/aukedamsma/poppodia-agenda)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})
TIMEOUT = 25
TODAY = date.today()
HORIZON = TODAY + timedelta(days=400)

EVENT_LINK_HINTS = ("/agenda/", "/event/", "/events/", "/evenement", "/programma/", "/program/", "/voorstelling", "/concert", "/shows/", "/show/", "/production/", "/productie/", "/activiteit")

NL_MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maart": 3, "mrt": 3, "april": 4, "apr": 4,
    "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "augustus": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    # Engels (sommige podia tonen Engelse datums)
    "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5, "june": 6, "july": 7, "august": 8,
    "october": 10, "oct": 10,
}


@dataclass
class Event:
    venue: str
    city: str
    title: str
    start: str                     # ISO 8601, lokale tijd (Europe/Amsterdam) zonder tz of met tz
    url: str
    end: str | None = None
    subtitle: str | None = None
    genres: list[str] = field(default_factory=list)
    price: str | None = None
    status: str | None = None      # afgelast / uitverkocht / verplaatst
    source: str = ""               # strategie waarmee gevonden
    first_seen: str | None = None  # wordt gezet uit state/seen.json
    artists: list[str] = field(default_factory=list)      # herkende artiesten, eerste = headliner
    genre_norm: list[str] = field(default_factory=list)   # hoofdgenres (genres.yaml)
    kind: str = "concert"          # concert | club | festival | other
    price_num: float | None = None
    free: bool = False
    lineup: list[str] = field(default_factory=list)       # line-up van de eventpagina (JSON-LD performer of `lineup`-selector)
    location: str | None = None    # locatie zoals het podium die noemt, als die afwijkt van het podium zelf (Paradiso in Tolhuistuin)
    subgenres: list[str] = field(default_factory=list)    # canonieke subgenres (genres.yaml -> subgenres, + zelfgeleerd)
    price_est: bool = False        # prijs geschat uit eerdere edities van dezelfde reeks (state/series.json)
    time_est: bool = False         # aanvangstijd idem
    section: str = "poppodium"     # poppodium | overig (uit venues.yaml)


# ----------------------------------------------------------------------------
# hulpfuncties
# ----------------------------------------------------------------------------

_LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    if len(_LOG) < 5000:
        _LOG.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8", "Upgrade-Insecure-Requests": "1"}
_BROWSER_UA_HOSTS: set[str] = set()   # hosts die onze eigen user-agent weigeren (Cloudflare 403: Het Podium, So What!)


def get(url: str, delay: float = 0.0, **kw) -> requests.Response:
    if delay:
        time.sleep(delay)
    host = urlparse(url).netloc
    if host in _BROWSER_UA_HOSTS and "headers" not in kw:
        kw["headers"] = BROWSER_HEADERS
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    if getattr(r, "status_code", 200) in (403, 406, 429, 503) and host not in _BROWSER_UA_HOSTS:
        # de site weigert de eigen user-agent van de agenda; probeer één keer als gewone browser (robots.txt staat crawlen toe)
        kw["headers"] = BROWSER_HEADERS
        r2 = SESSION.get(url, timeout=TIMEOUT, **kw)
        if r2.ok:
            _BROWSER_UA_HOSTS.add(host)
            log(f"    {host}: eigen user-agent geweigerd ({r.status_code}), verder als browser")
            r = r2
    r.raise_for_status()
    return r


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = unescape(unescape(str(s)))  # WP REST levert soms dubbel gecodeerde titels ("&amp;#038;")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


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


def fmt_price(p) -> str | None:
    if p in (None, "", 0, "0", "0.00", "0,00"):
        return None
    if isinstance(p, (int, float)):
        return f"€ {p:g}".replace(".", ",")
    p = clean(str(p)) or ""
    if re.fullmatch(r"\d+([.,]\d{1,2})?", p):
        return "€ " + p.replace(".", ",").removesuffix(",00")
    return p or None


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
    s = str(value).strip()
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
    m = re.fullmatch(r"(?:[a-z]{2,9}\.?\s+)?(\d{1,2})[./](\d{1,2})\.?(?:\s+(\d{1,2})[:.](\d{2}))?", low)
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
    try:
        return dtparser.parse(s, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def to_local(dt: datetime) -> datetime:
    """UTC/tz-aware -> naive Nederlandse tijd (CET/CEST)."""
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


def in_window(dt: datetime | None) -> bool:
    return dt is not None and TODAY - timedelta(days=1) <= dt.date() <= HORIZON


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


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


def event_from_jsonld(n: dict, v: dict, page_url: str, source: str) -> Event | None:
    if v.get("time_is_local"):
        n = {**n, "startDate": _strip_tz(n.get("startDate")), "endDate": _strip_tz(n.get("endDate")), "doorTime": _strip_tz(n.get("doorTime"))}
    start = parse_dt(n.get("startDate"))
    if not start:
        return None
    url = n.get("url") or page_url
    if isinstance(url, dict):
        url = url.get("@id") or page_url
    if source in ("jsonld_detail", "x") and isinstance(url, str) and "://" in url and "://" in page_url \
            and urlparse(url).netloc.replace("www.", "").lower() != urlparse(page_url).netloc.replace("www.", "").lower():
        url = page_url   # Metropool: JSON-LD op de eventpagina wijst naar de site van een ander podium (zelfde CMS-leverancier); de pagina zelf is de bron
    offers = as_list(n.get("offers"))
    price = None
    if offers and isinstance(offers[0], dict):
        p = offers[0].get("price") or offers[0].get("lowPrice")
        price = f"€ {p}" if p not in (None, "", "0", 0) else ("gratis" if p in ("0", 0) else None)
    status = None
    es = str(n.get("eventStatus", ""))
    if "Cancelled" in es:
        status = "afgelast"
    elif "Postponed" in es or "Rescheduled" in es:
        status = "verplaatst"
    genres = [clean(g) for g in as_list(n.get("genre")) + as_list(n.get("keywords")) if clean(g)]
    if len(genres) == 1 and "," in genres[0]:
        genres = [g.strip() for g in genres[0].split(",")]
    lineup = [clean(p.get("name")) if isinstance(p, dict) else clean(str(p)) for p in as_list(n.get("performer"))]
    lineup = [x for x in lineup if x and len(x) <= 60]
    perf = ", ".join(lineup) or None
    if not genres and n.get("description"):
        genres = genre_hints(str(n.get("description")))
    return Event(
        venue=v["name"], city=v["city"], title=clean(n.get("name")) or "(zonder titel)",
        start=start.isoformat(timespec="minutes"), url=urljoin(page_url, str(url)),
        end=(parse_dt(n.get("endDate")) or start).isoformat(timespec="minutes") if n.get("endDate") else None,
        subtitle=perf if perf and perf.lower() != (clean(n.get("name")) or "").lower() else None,
        genres=genres, price=price, status=status, source=source, lineup=lineup,
        location=_ld_location(n, v),
    )


def _ld_location(n: dict, v: dict) -> str | None:
    """Locatienaam uit JSON-LD als die afwijkt van het podium zelf (Rotown programmeert in V11, LantarenVenster, Annabel…)."""
    loc = n.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    name = loc.get("name") if isinstance(loc, dict) else (loc if isinstance(loc, str) else None)
    name = clean(name) if name else None
    if not name or _fold(name) == _fold(v["name"]) or _fold(v["name"]) in _fold(name) or _fold(name) in _fold(v["name"]):
        return None
    return name


# ----------------------------------------------------------------------------
# strategieën
# ----------------------------------------------------------------------------

def strat_microdata(v: dict, html: str) -> list[Event]:
    """schema.org-microdata in de HTML: <div itemscope itemtype=".../Event"> met itemprop name/startDate/url/offers."""
    s = soup_of(html)
    out = []
    for it in s.select('[itemtype*="schema.org/Event"], [itemtype*="schema.org/MusicEvent"], [itemtype*="schema.org/Festival"]'):
        def prop(name):
            el = it.find(attrs={"itemprop": name})
            if el is None:
                return None
            return el.get("content") or el.get("datetime") or el.get("href") or clean(el.get_text())
        # name: de eerste itemprop=name die niet in een geneste location zit
        name = None
        for el in it.find_all(attrs={"itemprop": "name"}):
            if not el.find_parent(attrs={"itemprop": "location"}) and not el.find_parent(attrs={"itemprop": "performer"}):
                name = el.get("content") or clean(el.get_text())
                if name:
                    break
        start = parse_dt(prop("startDate"))
        if not (name and start):
            continue
        url = prop("url") or v["url"]
        price = None
        for pe in it.find_all(attrs={"itemprop": ["price", "lowPrice"]}):
            price = fmt_price(pe.get("content") or clean(pe.get_text()))
            if price:
                break
        genres = [clean(g.get("content") or g.get_text()) for g in it.find_all(attrs={"itemprop": "genre"})]
        sub_el = it.select_one(".subtitle, .subtitel, .sub, .support, .tagline")
        status = None
        es = prop("eventStatus") or ""
        if "Cancelled" in es:
            status = "afgelast"
        elif it.find(class_=re.compile(r"sold-?out|uitverkocht", re.I)):
            status = "uitverkocht"
        end = parse_dt(prop("endDate"))
        out.append(Event(venue=v["name"], city=v["city"], title=clean(name) or "?", start=start.isoformat(timespec="minutes"),
                         url=urljoin(v["url"], url), end=end.isoformat(timespec="minutes") if end else None,
                         subtitle=clean(sub_el.get_text()) if sub_el else None, genres=[g for g in genres if g],
                         price=price, status=status, source="microdata"))
    return out


def strat_jsonld(v: dict, html: str) -> list[Event]:
    evs = [event_from_jsonld(n, v, v["url"], "jsonld") for n in jsonld_events(html)]
    return [e for e in evs if e]


# --- ingebedde JSON -----------------------------------------------------------

DATE_KEYS = re.compile(r"^(start|event|begin)?_?(date|datum|start|starts?_?at|time|startdate|eventdate|date_start|start_date|machine)$", re.I)
TITLE_KEYS = ("title", "name", "naam", "titel")


def _find_date(obj: dict) -> datetime | None:
    for k, val in obj.items():
        if DATE_KEYS.match(k) and isinstance(val, (str, int, float)):
            dt = parse_dt(val)
            if dt and 2000 < dt.year < 2100:
                if isinstance(val, (int, float)) and dt.minute == 0 and dt.hour in (0, 1, 2, 12, 13, 14):
                    dt = dt.replace(hour=0, minute=0)  # timestamp op middernacht/middag UTC = alleen een datum
                return dt
    for k, val in obj.items():
        if isinstance(val, dict) and re.search(r"date|start|datum", k, re.I):
            dt = _find_date(val)
            if dt:
                return dt
    return None


def _find_title(obj: dict) -> str | None:
    for k in TITLE_KEYS:
        val = obj.get(k)
        if isinstance(val, str) and val.strip():
            return clean(val)
        if isinstance(val, dict):
            for kk in ("rendered", "nl", "en"):
                if isinstance(val.get(kk), str) and val[kk].strip():
                    return clean(val[kk])
    return None


def _find_url(obj: dict) -> str | None:
    for k in ("url", "link", "permalink", "slug", "endpoint", "path", "href"):
        val = obj.get(k)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict) and isinstance(val.get("nl"), str):
            return val["nl"]
    return None


def _find_genres(obj: dict) -> list[str]:
    out = []
    for k in ("eventType", "event_type", "category", "type", "profile"):
        val = obj.get(k)
        if isinstance(val, dict):
            n = val.get("label") or val.get("name") or val.get("title")
            if isinstance(n, str) and n.lower() not in ("event", "events", "evenement"):
                out.append(clean(n))
    for k in ("genres", "genre", "tags", "categories", "categorieen", "styles"):
        for g in as_list(obj.get(k)):
            if isinstance(g, str) and not g.isdigit():
                out.append(clean(g))
            elif isinstance(g, dict):
                n = g.get("name") or g.get("title") or g.get("label")
                if isinstance(n, dict):
                    n = n.get("nl") or n.get("en")
                if isinstance(n, str):
                    out.append(clean(n))
    return [g for g in out if g]


def _walk_event_lists(obj, depth=0, found=None):
    """Zoekt lijsten van dicts die op events lijken (titel + datum)."""
    if found is None:
        found = []
    if depth > 40:
        return found
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 5:
            # JSON:API-stijl {type, attributes}
            flat = [x.get("attributes", x) if isinstance(x.get("attributes"), dict) else x for x in dicts]
            hits = [x for x in flat if _find_title(x) and _find_date(x)]
            if len(hits) >= max(5, len(flat) // 2):
                found.append(hits)
                return found
        for x in obj:
            _walk_event_lists(x, depth + 1, found)
    elif isinstance(obj, dict):
        for val in obj.values():
            _walk_event_lists(val, depth + 1, found)
    return found


def strat_embedded(v: dict, html: str) -> list[Event]:
    s = soup_of(html)
    blobs = []
    for sc in s.find_all("script"):
        t = (sc.get("type") or "").lower()
        if sc.get("id") == "__NEXT_DATA__" or "json" in t and "ld+json" not in t:
            blobs.append(sc.string or sc.get_text())
    # JSON in HTML-attributen (Vue-props zoals :all-items='[{&quot;...', data-events="...")
    for m in re.finditer(r"=(['\"])((?:\[\{|\{)&quot;.{200,}?)\1", html, re.S):
        blobs.append(unescape(m.group(2)))
    # ook window.__NUXT__ / __INITIAL_STATE__ als JSON-literal
    for m in re.finditer(r"window\.__(?:NUXT|INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE)__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S):
        blobs.append(m.group(1))
    # alle eventlijsten samenvoegen (Melkweg: per maand een lijst; alleen de grootste nemen gaf 235 van 283 events);
    # dubbelen (zelfde url/id of titel+datum) vallen weg
    best: list[dict] = []
    seen_keys: set = set()
    for raw in blobs:
        if not raw or len(raw) < 200:
            continue
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for lst in sorted(_walk_event_lists(j), key=len, reverse=True):
            for o in lst:
                k = _find_url(o) or o.get("id") or (str(_find_title(o)), str(_find_date(o)))
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                best.append(o)
    out = []
    for o in best:
        title, dt = _find_title(o), _find_date(o)
        if not (title and dt):
            continue
        if dt.hour == 0 and dt.minute == 0:
            # tijd staat soms los van de datum (Effenaar: times.starts_at)
            tm = None
            for k in ("times", "time", "tijden"):
                if isinstance(o.get(k), dict):
                    tm = o[k].get("starts_at") or o[k].get("start") or o[k].get("aanvang")
            tm = tm or o.get("starts_at") or o.get("start_time") or o.get("startTime") or o.get("aanvang")
            if isinstance(tm, str) and re.fullmatch(r"\d{1,2}[:.]\d{2}(:\d{2})?", tm.strip()):
                hh, mm = re.split(r"[:.]", tm.strip())[:2]
                dt = dt.replace(hour=int(hh), minute=int(mm))
        if v.get("url_template") and o.get("id") is not None:
            url = v["url_template"].format(**{k: str(val) for k, val in o.items() if isinstance(val, (str, int))})
        else:
            url = _find_url(o) or v["url"]
        if url and not url.startswith("http"):
            # Effenaar: slug; Melkweg: /nl/agenda/slug
            url = urljoin(v["url"], url)
        profile = o.get("profile") or o.get("type") or o.get("category")
        extra_genres = [clean(profile)] if isinstance(profile, str) and profile.lower() not in ("event", "events", "evenement") else []
        status = None
        for k in ("status", "state", "eventStatus"):
            sv = o.get(k)
            if isinstance(sv, str) and re.search(r"afgelast|cancel|uitverkocht|sold", sv, re.I):
                status = "afgelast" if re.search(r"afgelast|cancel", sv, re.I) else "uitverkocht"
        if o.get("isCancelled") is True or o.get("cancelled") is True:
            status = "afgelast"
        elif o.get("isSoldOut") is True or o.get("soldOut") is True or o.get("sold_out") is True:
            status = status or "uitverkocht"
        if o.get("isPublished") is False or o.get("publish") is False:
            continue
        price = o.get("price") or o.get("ticket_price") or o.get("ticketPrice") or o.get("priceFrom")
        if price in (None, "", 0) and o.get("freeEvent") is True:
            price = "gratis"
        sub = o.get("subtitle") or o.get("tagline") or o.get("one_liner")
        if not sub and isinstance(o.get("description"), str) and 0 < len(o["description"]) < 140:
            sub = o["description"]
        end = None
        for k in ("endDate", "end_date", "end", "ends_at"):
            if isinstance(o.get(k), str):
                e = parse_dt(o[k])
                end = e.isoformat(timespec="minutes") if e else None
                break
        out.append(Event(
            venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"),
            url=url, end=end, subtitle=clean(sub) if isinstance(sub, str) else None,
            genres=list(dict.fromkeys(_find_genres(o) + extra_genres)), price=fmt_price(price),
            status=status, source="embedded",
        ))
    return out


# --- WordPress: The Events Calendar -----------------------------------------

def strat_tribe(v: dict, base: str) -> list[Event]:
    api = v.get("api") or urljoin(base, "/wp-json/tribe/events/v1/events")
    out, page = [], 1
    while page <= 20:
        r = get(api, params={"per_page": 50, "page": page, "start_date": TODAY.isoformat(), "status": "publish"}, delay=0.5)
        j = r.json()
        for e in j.get("events", []):
            dt = parse_dt(e.get("start_date"))
            if not dt:
                continue
            cats = [clean(c.get("name")) for c in e.get("categories", []) if isinstance(c, dict)]
            cost = clean(e.get("cost")) if e.get("cost") else None
            out.append(Event(venue=v["name"], city=v["city"], title=clean(e.get("title")) or "?", start=dt.isoformat(timespec="minutes"),
                             url=e.get("url") or base, end=(parse_dt(e.get("end_date")) or dt).isoformat(timespec="minutes") if e.get("end_date") else None,
                             genres=[c for c in cats if c and c.lower() not in ("concert", "evenement", "event")], price=cost, source="tribe"))
        if page >= int(j.get("total_pages") or 1):
            break
        page += 1
    return out


# --- WordPress: eigen event-posttype -----------------------------------------

def _stager_acf(acf: dict) -> dict:
    """Stager-WordPress-koppeling (Muziekgieterij e.a.): ACF-velden stager_* met programma-start, deuren, tickets, status.
    Geeft {start, doors, price, subtitle, status} voor zover aanwezig."""
    out: dict = {}
    if not isinstance(acf, dict) or not any(k.startswith("stager_") for k in acf):
        return out
    for k in ("stager_program_start", "stager_doors_open", "stager_production_start"):
        dt = parse_dt(acf.get(k)) if acf.get(k) else None
        if dt:
            out.setdefault("start", dt)
            if k == "stager_doors_open":
                out["doors"] = dt
    if acf.get("stager_production_free") is True:
        out["price"] = "gratis"
    else:
        prices = []
        for t in as_list(acf.get("stager_tickets") or []):
            if not isinstance(t, dict) or t.get("stager_ticket_valid") is False:
                continue
            p = t.get("stager_ticket_price")
            if isinstance(p, (int, float)) and p > 0 and str(t.get("stager_ticket_type", "REGULAR")).upper() in ("REGULAR", "EARLYBIRD", "EARLY_BIRD", "PRESALE", "DOOR"):
                prices.append((0 if str(t.get("stager_ticket_type")).upper() == "REGULAR" else 1, float(p)))
        if prices:
            out["price"] = fmt_price(min(prices)[1])
    sub = acf.get("stager_production_subtitle")
    if isinstance(sub, str) and sub.strip():
        out["subtitle"] = sub
    if acf.get("production_tickets_soldout") is True:
        out["status"] = "uitverkocht"
    elif acf.get("stager_production_postponed") is True or acf.get("stager_production_moved") is True:
        out["status"] = "verplaatst"
    return out


def _acf_date(acf: dict) -> datetime | None:
    if not isinstance(acf, dict):
        return None
    st = _stager_acf(acf)
    if st.get("start"):
        return st["start"]
    for k in ("date_time", "start_date", "startdate", "date", "datum", "event_date", "start", "begin"):
        if acf.get(k):
            dt = parse_dt(acf[k])
            if dt:
                t = acf.get("time_start") or acf.get("start_time") or acf.get("aanvang")
                if dt.hour == 0 and isinstance(t, str) and re.match(r"\d{1,2}[:.]\d{2}", t):
                    hh, mm = re.split(r"[:.]", t)[:2]
                    dt = dt.replace(hour=int(hh), minute=int(mm))
                return dt
    return None


def strat_wp_event(v: dict, base: str, detail_cache: dict) -> list[Event]:
    api = v.get("api")
    if not api:
        rest_base = None
        try:
            types = get(urljoin(base, "/wp-json/wp/v2/types"), delay=0.5).json()
            cands = [t for t in types.values() if re.search(r"event|evenement|agenda|programma|voorstelling|concert|show", t.get("rest_base", "") + t.get("slug", ""), re.I)
                     and not re.search(r"log|activity|ticket|serie|categor|type$|types$|keynius|label|genre|locat", t.get("rest_base", ""), re.I)]
            # Bibelot: 'eventtype' (zakelijke eventsoorten) en 'keynius-event' (lockers) zijn geen agenda; 'programma' wel
            pref = ["events", "event", "evenementen", "evenement", "programma", "agenda", "voorstellingen", "voorstelling", "concerten", "concert", "shows", "show"]
            cands.sort(key=lambda t: pref.index(t["rest_base"]) if t.get("rest_base") in pref else 99)
            if cands:
                rest_base = cands[0]["rest_base"]
        except (requests.RequestException, ValueError):
            pass
        if not rest_base:
            # typenlijst afgeschermd: gangbare namen proberen
            for guess in ("event", "events", "evenement", "evenementen", "agenda", "programma", "voorstelling", "voorstellingen", "concert", "concerten", "show", "shows"):
                try:
                    r = SESSION.get(urljoin(base, f"/wp-json/wp/v2/{guess}?per_page=1"), timeout=TIMEOUT)
                    if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                        rest_base = guess
                        break
                except (requests.RequestException, ValueError):
                    continue
        if not rest_base:
            return []
        api = urljoin(base, f"/wp-json/wp/v2/{rest_base}?per_page=100&_embed=1")
    out, page = [], 1
    while page <= int(v.get("max_pages", 3)):
        sep = "&" if "?" in api else "?"
        r = get(f"{api}{sep}page={page}", delay=0.5)
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        for it in items:
            title = _find_title(it)
            link = it.get("link")
            if not (title and link):
                continue
            acf = it.get("acf") or it.get("meta") or {}
            dt = _acf_date(acf) or date_from_url(link)
            genres = []
            want_tax = v.get("genre_taxonomies")  # bv. [production_genre]; anders alles behalve categorie/tag/taal
            for grp in (it.get("_embedded", {}) or {}).get("wp:term", []):
                for term in grp:
                    tax = term.get("taxonomy")
                    ok = (tax in want_tax) if want_tax else (tax not in ("category", "post_tag", "language"))
                    if ok and term.get("name"):
                        genres.append(clean(term["name"]))
            stg = _stager_acf(acf)
            price = stg.get("price") or (acf.get("price") if isinstance(acf, dict) else None)
            sub = stg.get("subtitle") or (acf.get("one_liner") or acf.get("subtitle") or acf.get("support_act") if isinstance(acf, dict) else None)
            if not dt and v.get("detail_jsonld", True):
                # datum staat alleen op de eventpagina -> JSON-LD of HTML daar lezen
                ev = fetch_detail(v, link, detail_cache, title=title)
                if ev:
                    ev.genres = ev.genres or genres
                    ev.source = "wp_event+detail"
                    out.append(ev)
                continue
            if not dt:
                continue
            if isinstance(price, str) and (price.startswith("€") or price == "gratis"):
                price_s = price
            else:
                price_s = (f"€ {price}" if price not in (None, "", 0) else None) if isinstance(price, (str, int, float)) else None
            out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=link,
                             subtitle=clean(sub) if isinstance(sub, str) else None, genres=genres,
                             price=normalize_price(price_s) if price_s else None, status=stg.get("status"), source="wp_event"))
        if len(items) < 100 and "per_page=100" in api:
            break
        if int(r.headers.get("X-WP-TotalPages", 1)) <= page:
            break
        page += 1
    return out


def _path(obj, path: str):
    """Waarde uit geneste dicts/lijsten via een puntpad: 'label.title', 'dates.0.start'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _fill(template: str, item: dict):
    """'https://site/programma/{seo_slug}/' -> ingevuld; een pad zonder accolades is een directe veldnaam."""
    if "{" not in template:
        return _path(item, template)
    missing = False

    def rep(m):
        nonlocal missing
        val = _path(item, m.group(1))
        if val in (None, ""):
            missing = True
        return str(val) if val is not None else ""
    out = re.sub(r"\{([^}]+)\}", rep, template)
    return None if missing else out


def strat_json_api(v: dict, base: str, detail_cache: dict) -> list[Event]:
    """Eigen JSON-eindpunt van een podium (venues.yaml: type: json_api). Veel sites laden hun agenda via een
    AJAX-call die een JSON-lijst teruggeeft (Boerderij: includes/ajax/events.php). De veldnamen verschillen per site,
    dus `fields` beschrijft de afbeelding; de waarden zijn puntpaden of templates met {veld}:

      type: json_api
      api: https://…/includes/ajax/events.php?limit=69420
      items: data.events            # optioneel: pad naar de lijst (standaard: het antwoord zelf, of het eerste lijstveld)
      fields:
        title: title                # verplicht
        date: event_date            # verplicht: ISO-datum of datum+tijd (parse_dt)
        url: "https://…/programma/{seo_slug}/"   # verplicht
        subtitle: subtitle
        price: ticket_price
        time: start_time            # optioneel: "20:30" los van de datum
        genres: genre               # string of lijst
        status: label.title         # 'Uitverkocht' / 'Afgelast' e.d.
      page_param: offset            # optioneel: paginering via ?offset=N (met page_size) of ?page=N
    Ontbreken tijd/prijs, dan haalt de generieke verrijking ze van de eventpagina (JSON-LD of tekst)."""
    api = v["api"]
    f = v.get("fields") or {}
    out: list[Event] = []
    page, offset = 1, 0
    while page <= int(v.get("max_pages", 5)):
        url = api
        if v.get("page_param") and page > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{v['page_param']}={offset if v['page_param'] == 'offset' else page}"
        r = get(url, delay=float(v.get("crawl_delay", 0.5)))
        try:
            data = r.json()
        except ValueError:
            data = json.loads(r.text.strip())
        items = _path(data, v["items"]) if v.get("items") else data
        if isinstance(items, dict):
            # {"events": [...]} of een dict van dicts met de slug als sleutel (Vorstin ?agenda_json)
            lst = next((x for x in items.values() if isinstance(x, list)), None)
            items = lst if lst is not None else [x for x in items.values() if isinstance(x, dict)]
        if not isinstance(items, list) or not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            title = clean(str(_fill(f.get("title", "title"), it) or ""))
            link = _fill(f.get("url", "url"), it)
            raw_date = _fill(f.get("date", "date"), it)
            if not (title and link and raw_date):
                continue
            raw_date = str(raw_date)
            if re.fullmatch(r"\d{12}", raw_date):   # 202609111915 (Vorstin program_start)
                dt = datetime.strptime(raw_date, "%Y%m%d%H%M")
            elif re.fullmatch(r"\d{8}", raw_date):
                dt = datetime.strptime(raw_date, "%Y%m%d")
            else:
                dt = parse_dt(raw_date)
            if not dt:
                continue
            if f.get("time") and (dt.hour, dt.minute) == (0, 0):
                tm = _fill(f["time"], it)
                m = re.match(r"(\d{1,2})[:.u](\d{2})", str(tm or ""))
                if m:
                    dt = dt.replace(hour=int(m.group(1)), minute=int(m.group(2)))
            link = urljoin(base, str(link))
            genres = _fill(f["genres"], it) if f.get("genres") else None
            genres = [clean(str(g)) for g in as_list(genres) if g] if genres else []
            raw_status = clean(str(_fill(f["status"], it) or "")) if f.get("status") else None
            status = None
            price = fmt_price(_fill(f["price"], it)) if f.get("price") else None
            if raw_status:
                low = raw_status.lower()
                if re.search(r"uitverkocht|sold[ _-]?out", low):
                    status = "uitverkocht"
                elif re.search(r"afgelast|cancel", low):
                    status = "afgelast"
                elif re.search(r"verplaatst|postponed|moved", low):
                    status = "verplaatst"
                elif re.search(r"gratis|free", low) and not price:
                    price = "gratis"        # Mezz: event.status = "Gratis"
            if price and not re.search(r"\d|gratis|free", price, re.I):
                price = None                # statusachtige tekst ("Tickets via TIDT") is geen prijs
            sub = _fill(f["subtitle"], it) if f.get("subtitle") else None
            out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=link,
                             subtitle=clean(str(sub)) if sub else None, genres=genres,
                             price=normalize_price(price) if price else None, status=status, source="json_api"))
        if not v.get("page_param") or len(items) < int(v.get("page_size", len(items) or 1)):
            break
        offset += len(items)
        page += 1
    return out


def strat_stager(v: dict, base: str, cache: dict) -> list[Event]:
    """Stager-ticketshop (<podium>.stager.co) via de shop-API die de webshop zelf gebruikt. Veel poppodia verkopen via
    Stager (Vera, Simplon, Boerderij, So What!, Bibelot, Willemeen, Hall of Fame, Muziekgieterij…). De shoppagina heeft
    JSON-LD met 50 events (zonder prijs); de API geeft álle komende events én per event de ticketprijzen:
      1. GET  /shop/default/events            -> data-flags {"shopId": 301}
      2. POST /shop/v1/session/new?shopId=301&locale=NL&hasOrderToken=false  (body {})  -> accessToken.jwt (anonieme sessie)
      3. GET  /shop/v1/events?offset=0&limit=20  (Bearer)  -> eventId, name, startsOn (UTC), soldOut; doorgaan tot leeg
      4. GET  /shop/v1/events/{id}/tickets-overview -> ticketGroups[{name, priceInCents}] -> reguliere online prijs
    Lukt de sessie niet, dan valt de aanroeper terug op de JSON-LD van de shoppagina (type: jsonld)."""
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    html = get(f"{root}/shop/default/events", delay=0.3).text
    m = re.search(r'data-flags="([^"]+)"', html)
    if not m:
        raise ValueError("Stager: geen data-flags/shopId op de shoppagina")
    import html as _html
    flags = json.loads(_html.unescape(m.group(1)))
    shop_id = flags.get("shopId")
    r = SESSION.post(f"{root}/shop/v1/session/new", params={"shopId": shop_id, "locale": "NL", "hasOrderToken": "false"},
                     json={}, timeout=TIMEOUT)
    r.raise_for_status()
    token = r.json()["accessToken"]["jwt"]
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    items = []
    offset = 0
    while offset < 2000:
        rr = SESSION.get(f"{root}/shop/v1/events", params={"offset": offset, "limit": 20}, headers=hdr, timeout=TIMEOUT)
        rr.raise_for_status()
        chunk = rr.json()
        if not isinstance(chunk, list) or not chunk:
            break
        items += chunk
        offset += len(chunk)
        time.sleep(0.3)
    out = []
    for it in items:
        start = parse_dt(it.get("startsOn"))
        if not start or not it.get("eventId"):
            continue
        url = f"{root}/shop/default/events/{it['eventId']}"
        # prijs per event, één keer per dag gecached
        ck = "stager|" + url
        c = cache.get(ck)
        if c and c.get("fetched") == TODAY.isoformat():
            price = c.get("price")
        else:
            price = None
            try:
                tv = SESSION.get(f"{root}/shop/v1/events/{it['eventId']}/tickets-overview", headers=hdr, timeout=TIMEOUT)
                time.sleep(0.25)
                if tv.ok:
                    price = _stager_price(tv.json().get("ticketGroups") or [])
            except (requests.RequestException, ValueError):
                pass
            cache[ck] = {"fetched": TODAY.isoformat(), "price": price}
        out.append(Event(venue=v["name"], city=v["city"], title=clean(it.get("name")) or "?", start=start.isoformat(timespec="minutes"),
                         url=url, price=price, status="uitverkocht" if it.get("soldOut") else None, source="stager"))
    return out


def _stager_price(groups: list[dict]) -> str | None:
    """Reguliere online prijs uit Stager-ticketgroepen: kortingsgroepen (leden, studenten, early bird, deur) vallen af als
    er andere zijn; daarna 'voorverkoop/regulier' vóór de rest; alles 0 = gratis."""
    cands = []
    for g in groups:
        if not isinstance(g, dict) or g.get("priceInCents") is None:
            continue
        cands.append((int(g.get("weight") or 0), g.get("name") or "", int(g["priceInCents"])))
    if not cands:
        return None
    cands.sort()
    paid = [c for c in cands if c[2] > 0]
    if not paid:
        return "gratis"
    regular = [c for c in paid if not _DISCOUNT_CTX.search(c[1])] or paid
    online = [c for c in regular if re.search(r"voorverkoop|vvk|presale|pre-?sale|online", c[1], re.I)]
    pref = online or [c for c in regular if _PREFERRED_CTX.search(c[1])] or regular
    return fmt_price(pref[0][2] / 100)


# --- eventlinks volgen + JSON-LD op detailpagina --------------------------------

NON_EVENT_PATH = re.compile(r"/page/?\d+/?$|/page/\d+|/en/|/english|/tag/|/tags/|/genre/|/genres/|/categor|/zoek|/search|/filter|/feed|/wp-|/nieuws|/news|/blog|/over|/about|/contact|/vacature|/verhuur|/faq|/privacy|/cookie|/algemene|/login|/account|/cart|/winkel|/shop|/merch|/pers|/partners|/steun|/vrienden|/locatie|/route|/tickets?$|/programma/?$|/agenda/?$|/events?/?$|/evenementen/?$|/agenda/(concerten|exposities?|expo|film|films|kids|jeugd|kidsjeugd|theater|cabaret|comedy|dans|workshops?|cursussen|festivals?|clubs?|party|feesten|overig|alles|all)/?$|\.(pdf|jpe?g|png|ics)$", re.I)


def event_links(v: dict, html: str, base: str) -> list[str]:
    """Kandidaat-eventlinks op een overzichtspagina.
    Met `link_pattern` is het simpel; zonder: links onder hetzelfde pad als de agendapagina (bijv. /programma/<slug>/),
    of met een event-achtig padsegment, minus paginering/filters/andere secties."""
    s = soup_of(html)
    pat = re.compile(v["link_pattern"]) if v.get("link_pattern") else None
    host = urlparse(base).netloc.replace("www.", "")
    base_path = urlparse(base).path.rstrip("/")
    seen, out = set(), []
    for a in s.find_all("a", href=True):
        raw = a["href"].split("#")[0]
        if not re.search(r"[?&](p|post_type|event_id|id)=", raw):
            raw = raw.split("?")[0]
        href = urljoin(base, raw)
        if urlparse(href).netloc.replace("www.", "") != host:
            continue
        path = urlparse(href).path + ("?" + urlparse(href).query if "post_type=" in href else "")
        if pat:
            if not pat.search(href):
                continue
        else:
            if path.rstrip("/") in ("", base_path):
                continue
            under_base = bool(base_path) and path.startswith(base_path + "/")
            hinted = any(h in path for h in EVENT_LINK_HINTS)
            if not (under_base or hinted):
                continue
            if NON_EVENT_PATH.search(path):
                continue
            # een eventpagina heeft minstens één 'slug'-segment onder de sectie
            depth = path.rstrip("/").count("/")
            if depth < 2:
                continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


FLIGHT_VERSION = 2  # idem, maar alleen voor pagina's die via event_from_flight_json (Paradiso) zijn gelezen
CACHE_VERSION = 6  # verhogen als fetch_detail/extract_from_text meer of betere velden oplevert: oude cache-items worden dan opnieuw opgehaald


_PUBLISH_CLASS = re.compile(r"publish|updated|entry-date|post-date|author-date|posted|meta-date|byline", re.I)


def _event_time_tag(s: BeautifulSoup):
    """De <time datetime> van het event, niet die van het blogbericht. WordPress-thema's zetten de publicatiedatum
    in <time class="entry-date updated"> (Gelderlandfabriek: 18 augustus, terwijl het event 19 september is); die
    valt buiten het venster en het event verdween. Voorkeur: een <time> zonder publicatie-klasse met een datum in het
    venster; anders de eerste zonder publicatie-klasse; anders niets (dan telt de datum uit de tekst)."""
    tags = s.find_all("time", attrs={"datetime": True})
    cands = []
    for t in tags:
        cls = " ".join(t.get("class", [])) + " " + " ".join(t.parent.get("class", []) if t.parent else [])
        if _PUBLISH_CLASS.search(cls):
            continue
        cands.append(t)
    for t in cands:
        if in_window(parse_dt(t["datetime"])):
            return t
    return cands[0] if cands else None


def fetch_detail(v: dict, url: str, cache: dict, title: str | None = None) -> Event | None:
    """Leest een eventpagina; cache op URL zodat dit maar zelden opnieuw hoeft."""
    c = cache.get(url)
    if c and c.get("fetched") and c.get("event") and c["event"].get("start", "9999") >= TODAY.isoformat():
        stale = c.get("v", 1) < CACHE_VERSION or (c["event"].get("source") == "flight_json" and c.get("fv", 1) < FLIGHT_VERSION)
        if stale:
            c = None  # verouderd cache-item van een oudere parser (bv. zonder prijs/line-up, of met gelekte startMain): opnieuw ophalen
    if c and c.get("fetched") and not c.get("event") and c.get("v", 1) < CACHE_VERSION:
        c = None  # mislukking van een oudere parser: na een fix direct opnieuw proberen (Gelderlandfabriek, Q-factory)
    if c and c.get("fetched"):
        # geslaagde resultaten 10 dagen bewaren; mislukkingen maar 1 dag, zodat een fix snel doorwerkt
        ttl = int(v.get("detail_ttl_days", 10)) if c.get("event") else 1
        if c.get("event") and c["event"].get("start", "9999") < TODAY.isoformat():
            ttl = 3650  # voorbij: nooit meer ophalen
        if date.fromisoformat(c["fetched"]) > TODAY - timedelta(days=ttl):
            return Event(**c["event"]) if c.get("event") else None
    try:
        html = get(url, delay=float(v.get("crawl_delay", 0.6))).text
    except requests.RequestException as ex:
        cache[url] = {"fetched": TODAY.isoformat(), "event": None, "error": str(ex)[:200]}
        return None
    ev = None
    for n in jsonld_events(html):
        ev = event_from_jsonld(n, v, url, "jsonld_detail")
        if ev:
            break
    if ev is None:
        ev = event_from_flight_json(html, url, v)
    txt = page_text(html)
    tdt, tstart, tdoors, tprice = extract_from_text(txt)
    if not tprice:
        tprice = price_from_embedded_json(html)
    if not tstart and not tdoors:
        tstart, tdoors = times_from_embedded_json(html)
    if ev is None:
        s = soup_of(html)
        t = _event_time_tag(s)
        h1 = s.find("h1")
        if title is None and h1:
            title = clean(h1.get_text())
        if title is None:
            og = s.find("meta", property="og:title")
            title = re.split(r"\s[|–-]\s", clean(og["content"]))[0] if og and og.get("content") else None
        # datum uit losse tekst is onbetrouwbaar als die precies vandaag is zonder tijd: veel sites tonen de
        # datum van vandaag in een header/agenda-widget (ECI: 74 "voorstellingen" op één dag)
        if tdt and tdt.date() == TODAY and not (tstart or tdoors):
            tdt = None
        sel_dt = None
        if v.get("detail_date"):
            # vaste plek van de datum op de eventpagina (P60: eerste .datum- element; de rest zijn 'binnenkort'-tips)
            el = s.select_one(v["detail_date"])
            if el is not None:
                sel_dt = parse_dt(clean(el.get_text())) or extract_from_text(el.get_text(" "))[0]
        dt = sel_dt or (parse_dt(t["datetime"]) if t else None) or date_from_url(url) or tdt
        if dt and (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            dt = dt.replace(hour=(tstart or tdoors)[0], minute=(tstart or tdoors)[1])
        if dt and title:
            ev = Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                       price=tprice, source="detail_text")
    if ev is not None:
        if v.get("lineup") and not ev.lineup:
            ev.lineup = clean_lineup([x.get_text() for x in soup_of(html).select(v["lineup"])], ev.title)
        if not ev.genres:
            hints = genre_hints(txt[:1500], limit=4)
            ev.genres = hints if len(hints) <= 2 else []  # >2 treffers = waarschijnlijk een genremenu op de pagina (Tivoli), geen beschrijving
        # aanvang gaat vóór deuren-open: als de gevonden tijd gelijk is aan de deurtijd en er staat een aanvang, neem die
        st = datetime.fromisoformat(ev.start)
        if tstart and (((st.hour, st.minute) == (0, 0)) or (tdoors and (st.hour, st.minute) == tdoors and tstart != tdoors)):
            ev.start = st.replace(hour=tstart[0], minute=tstart[1]).isoformat(timespec="minutes")
        if not ev.price and tprice:
            ev.price = tprice
    cache[url] = {"fetched": TODAY.isoformat(), "v": CACHE_VERSION, "fv": FLIGHT_VERSION, "event": asdict(ev) if ev else None}
    return ev


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


_JSON_TIME_START = re.compile(r'"(?:program_start|programme_start|start_time|starttime|startTime|aanvang|showtime|show_time)"\s*:\s*"?(\d{12}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?|\d{1,2}:\d{2})"?', re.I)
_JSON_TIME_DOORS = re.compile(r'"(?:door_open|doors_open|doorsOpen|doorTime|door_time|deuren|doors)"\s*:\s*"?(\d{12}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?|\d{1,2}:\d{2})"?', re.I)


def _hm_from_json(val: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\d{12}", val)
    if m:
        return int(val[8:10]), int(val[10:12])
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


def page_text(html: str) -> str:
    """Zichtbare tekst van het hoofddeel van een pagina (zonder scripts/styles/nav zover herkenbaar)."""
    s = soup_of(html)
    for t in s(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    body = s.find("main") or s.find("article") or s.body or s
    return re.sub(r"\s+", " ", body.get_text(" "))


def extract_from_text(txt: str) -> tuple[datetime | None, tuple[int, int] | None, tuple[int, int] | None, str | None]:
    """(datum, aanvangstijd, deurtijd, prijs) uit vrije tekst van een eventpagina.
    Datum: liefst met weekdag of 'datum:' ervoor, anders de eerste dag+maand(+jaar)."""
    low = txt.lower()
    dt = None
    for pat in (rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b{WEEKDAY_RE}\.\s?(\d{{1,2}})\.\s?{MONTH_RE}\b\.?(?:\s?(\d{{4}}))?",   # "Do.10.Sep" (Q-factory)
                rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}}[./]\d{{1,2}}(?:[./]\d{{2,4}})?)\b",
                rf"\bdatum\W{{0,6}}(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\.?\s+(\d{{4}})\b",
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\b\.?"):
        m = re.search(pat, low)
        if m:
            groups = [g for g in m.groups() if g]
            dt = parse_dt(" ".join(groups[-3:] if len(groups) >= 3 and groups[-1].isdigit() and len(groups[-1]) == 4 else groups[-2:]))
            if dt:
                break
    def hm(pat):
        mm = re.search(pat, low)
        return (int(mm.group(1)), int(mm.group(2))) if mm else None
    start = hm(r"(?:aanvang|start|begin|show|showtime|concert|hoofdprogramma|programma|\btijd)\W{0,12}(?:om\W{0,3})?(\d{1,2})[:.u](\d{2})") or hm(r"(\d{1,2})[:.u](\d{2})\W{0,6}(?:aanvang|start|begin|showtime)\b")
    # tijd direct achter de datum zonder label ("zaterdag 5 september 2026 14:30 uur", Estrado): dat is de aanvang
    if not start and dt is not None and m:
        after = low[m.end(): m.end() + 18]
        mt = re.match(r"\W{0,4}(?:om\s+)?(\d{1,2})[:.u](\d{2})\b(?!\s*-\s*\d)", after)
        if mt and int(mt.group(1)) < 24 and int(mt.group(2)) < 60:
            start = (int(mt.group(1)), int(mt.group(2)))
    # "20:30 (Doors: 19:30)" (Mezz, Stager-widgets): de tijd vóór het haakje is de aanvang, die erin de deurtijd
    mp = re.search(r"(\d{1,2})[:.u](\d{2})\s*\((?:doors?|deuren|zaal open)\W{0,4}(\d{1,2})[:.u](\d{2})\)", low)
    if mp and not start:
        start = (int(mp.group(1)), int(mp.group(2)))
    d1 = hm(r"(?:deur|deuren|doors|zaal open|open)\W{0,14}(?:om\W{0,3})?(\d{1,2})[:.u](\d{2})")
    d2 = hm(r"(\d{1,2})[:.u](\d{2})\W{0,6}(?:zaal open|deuren open|deuren|doors)\b")
    doors = min(d1, d2) if d1 and d2 else (d1 or d2)   # "19:30 Zaal Open 20:00 Support": de vroegste is de deurtijd
    if not start and doors:
        # tijdschema zonder het woord 'aanvang' (Nobel: "19:00 - Deuren open 19:30 - Hakselaer 20:45 - …"):
        # de eerste tijd ná de deurtijd is de start van het programma
        dm = re.search(r"(?:deur|deuren|doors|zaal open|open)", low)
        if dm:
            after = low[dm.end():dm.end() + 160]
            later = [(int(h), int(m)) for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", after) if (int(h), int(m)) > doors and int(h) < 24 and int(m) < 60]
            if later:
                start = min(later)
    # servicekosten zijn geen ticketprijs: "Gratis · incl. € 1,75 servicekosten" (FLUOR) moet gratis blijven
    plow = re.sub(r"(?:incl\.?|inclusief|excl\.?|exclusief|\+)?\s*€?\s?\d{1,3}(?:[.,]\d{2})?\s*(?:aan\s+)?(?:servicekosten|service ?fee|service ?kosten|administratiekosten|transactiekosten|fee)\b", " ", low)
    plow = re.sub(r"(?:servicekosten|service ?fee|service ?kosten|administratiekosten)\W{0,12}€?\s?\d{1,3}(?:[.,]\d{2})?", " ", plow)
    low = plow
    pm = _pick_price(low)
    price = None
    if re.search(r"\bgratis\b|\bfree\b(?! ?(wifi|parking))", low) and not pm:
        price = "gratis"
    elif pm:
        price = fmt_price(pm)
    return dt, start, doors, price


_DISCOUNT_CTX = re.compile(r"leden|members?|lid\b|cjp|student|scholier|jeugd|jongeren|kinderen|kids|t/m \d+ jaar|tot en met \d+|65\+|vrienden|donateur|korting|reduced|early\b|deurprijs|dagkassa|avondkassa|deurverkoop|aan de deur|at the door|door ?sale|\bdoor\s*:|deur\s*:|late\b", re.I)
_PREFERRED_CTX = re.compile(r"regulier|regular|normaal|standaard|voorverkoop|vvk|presale|pre-?sale|tickets?|entree|kaarten", re.I)


def _pick_price(low: str) -> str | None:
    """De reguliere voorverkoopprijs uit tekst met meerdere bedragen. So What!: "Leden: € 5,00 Regulier: € 10,00
    Voorverkoop Regulier: € 8,00" -> € 8,00 (niet de ledenprijs); De Piek: "Presale € 23,00 | Tickets € 25,00" -> € 23,00.
    Bedragen met een kortings- of deurprijscontext (leden, CJP, jeugd, dagkassa) vallen af zolang er andere zijn;
    daarna wint het eerste bedrag met een 'gewone' context, anders het eerste bedrag."""
    raw = []
    for pat in (r"€\s?(\d{1,3}(?:[.,]\d{2})?)(?:,-)?", r"(\d{1,3}(?:[.,]\d{2})?)\s?(?:€|(?<![a-z])euro\b)",
                r"(?<![a-z])euro?\b\s?(\d{1,3}(?:[.,]\d{2})?)(?:,-)?", r"(?:tickets?|entree|kaarten|prijs|vvk|voorverkoop)\W{0,12}(\d{1,3}[.,]\d{2})\b"):
        for m in re.finditer(pat, low):
            raw.append((m.start(), m.end(), m.group(1)))
    if not raw:
        return None
    raw.sort()
    cands = []
    prev_end = None
    for start, end, val in raw:
        if cands and start < cands[-1][3]:
            continue  # zelfde bedrag, door twee patronen gevonden
        # context = het label direct vóór dit bedrag (tot het vorige bedrag): "Door: €16,00 Early: €12,00 Regular: €14,50"
        lo = max(0, start - 28, prev_end if prev_end is not None else 0)
        cands.append((start, val, low[lo:start], end))
        prev_end = end
    cands = [(a, b, c) for a, b, c, _ in cands]
    regular = [c for c in cands if not _DISCOUNT_CTX.search(c[2])]
    pool = regular or cands
    # de prijs van nú online een gewoon ticket bestellen: voorverkoop/online gaat vóór 'regulier' (kan dagkassa zijn), dat vóór de rest
    online = [c for c in pool if re.search(r"voorverkoop|vvk|presale|pre-?sale|online", c[2], re.I)]
    preferred = online or [c for c in pool if _PREFERRED_CTX.search(c[2])]
    return (preferred or pool)[0][1]


def event_from_flight_json(html: str, url: str, v: dict) -> Event | None:
    """Eventgegevens uit een React Server Components-payload (o.a. Paradiso: Craft CMS via Next.js)."""
    ev_id = url.rstrip("/").rsplit("/", 1)[-1]
    clean_html = html.replace('\\"', '"')
    # lange line-ups (Paradiso: "artists" per zaal) kunnen duizenden tekens tussen id en startDateTime zetten
    m = re.search(r'"__typename":\s*"event_\w+_Entry",\s*"id":\s*"%s".{0,40000}?"startDateTime":\s*"([^"]+)"' % re.escape(ev_id), clean_html, re.S)
    if not m:
        return None
    # blok = dit event t/m het volgende event-object; anders lekken velden (startMain, prijs) van "gerelateerde events"
    tail = clean_html[m.end(): m.end() + 4000]
    nxt = re.search(r'"__typename":\s*"event_\w+_Entry"|"(?:relatedEvents|related|highlightedItems|upcoming|events)":\s*\[', tail)
    block = clean_html[m.start(): m.end() + (nxt.start() if nxt else 2500)]
    head = clean_html[m.start(): m.end()]  # id t/m startDateTime: hier staan titel, ondertitel en line-up
    # tijdvelden staan direct na startDateTime (date, doorsOpen, doorsClose, startMain); "startMain":null betekent: geen aparte aanvang
    near = tail[:600]
    sm = re.search(r'"startMain":\s*(?:"([^"]*)"|null)', near)
    start_main = sm.group(1) if sm and sm.group(1) else None

    def _unesc(v: str) -> str:  # JSON-escapes in de RSC-payload: \u0026 -> &, \/ -> /
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda mm: chr(int(mm.group(1), 16)), v).replace("\\/", "/")

    def fld(name):
        mm = re.search(r'"%s":\s*"([^"]*)"' % name, block)
        return clean(_unesc(mm.group(1))) if mm else None

    # startDateTime draagt de juiste DATUM (UTC); startMain/doorsOpen hebben een bruikbare TIJD maar een onzinnige datum
    # (Paradiso vult daar de dag van vandaag in). Dus: datum uit startDateTime, aanvangstijd uit startMain als die er is.
    start = parse_dt(m.group(1))
    if start and start_main:
        sm = parse_dt(start_main)
        if sm:
            start = start.replace(hour=sm.hour, minute=sm.minute)
    if not start:
        return None
    title = fld("title") or "?"
    lineup = []
    for seg in re.findall(r'"artists":\s*\[(.*?)\]\s*\}', head, re.S):
        lineup += [clean(_unesc(t)) for t in re.findall(r'"title":\s*"([^"]+)"', seg)]
    lineup = [x for x in dict.fromkeys(lineup) if x and not re.search(r"\btba\b|more tba|special guest", x, re.I)][:20]
    genres = []
    if '"subBrand"' in block:
        # subBrand = programmareeks (Club Paradiso, Indiestad, Tones, Sugar Mountain…): alleen houden als het een genre is
        cands = [_unesc(g) for g in re.findall(r'"title":\s*"([^"]+)"', block.split('"subBrand"', 1)[1].split("]", 1)[0]) if g][:3]
        genres = [g for g in cands if normalize_tag(g) not in (None, "overig")]
    loc = re.search(r'"location":\s*\{[^{}]*?"title":\s*"([^"]+)"', block)
    status = None
    if re.search(r'"(cancelled|isCancelled)":true', block) or "afgelast" in block.lower():
        status = "afgelast"
    elif re.search(r'"(soldOut|isSoldOut)":true', block) or "uitverkocht" in block.lower():
        status = "uitverkocht"
    sub = fld("subtitle")
    if loc and loc.group(1).lower() not in (v["name"].lower(),):
        sub = (sub + " · " if sub else "") + loc.group(1)
    price = fld("ticketPriceFormatted") or fld("price") or fld("priceFrom")
    if price:
        price = price.replace("€", "").strip()
    if fld("soldOut") == "yes":
        status = status or "uitverkocht"
    return Event(venue=v["name"], city=v["city"], title=title, start=start.isoformat(timespec="minutes"), url=url,
                 subtitle=sub, genres=genres, price=(f"€ {price}" if price else None), status=status, source="flight_json", lineup=lineup,
                 location=_unesc(loc.group(1)) if loc else None)


def strat_sitemap_detail(v: dict, base: str, cache: dict) -> list[Event]:
    """Eventlinks uit de sitemap (laatste N bestanden = nieuwste events), daarna per eventpagina lezen (gecached)."""
    sitemap = v.get("sitemap") or urljoin(base, "/sitemap.xml")
    idx = get(sitemap, delay=0.5).text
    if "<sitemapindex" in idx or ("<sitemap>" in idx and "<url>" not in idx):
        files = [m.group(1) for m in re.finditer(r"<loc>(.*?)</loc>", idx)]
        pat = re.compile(v.get("sitemap_pattern", r"event"))
        files = [f for f in files if pat.search(f)]
    else:
        files = [sitemap]   # gewone urlset (Q-factory): de sitemap zelf bevat de eventlinks

    def num(f):
        mm = re.search(r"(\d+)\.xml$", f)
        return int(mm.group(1)) if mm else 0

    files.sort(key=num)
    files = files[-int(v.get("last_files", 6)):] or [sitemap]
    # per URL de lastmod meenemen: aankomende events worden bijgewerkt (ticketstatus), oude niet meer.
    recent_days = int(v.get("sitemap_recent_days", 0))
    cutoff = (TODAY - timedelta(days=recent_days)).isoformat() if recent_days else None
    urls: list[str] = []
    for f in files:
        try:
            xml = get(f, delay=0.5).text
        except requests.RequestException as ex:
            log(f"    sitemap mislukt {f}: {ex}")
            continue
        for m in re.finditer(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>(.*?)</loc>", m.group(1))
            mod = re.search(r"<lastmod>(.*?)</lastmod>", m.group(1))
            if not loc:
                continue
            if cutoff and mod and mod.group(1)[:10] < cutoff:
                continue
            urls.append(loc.group(1))
        if not re.search(r"<url>", xml):  # sitemap zonder <url>-blokken: alleen <loc>
            urls += [m.group(1) for m in re.finditer(r"<loc>(.*?)</loc>", xml)]
    lp = re.compile(v["link_pattern"]) if v.get("link_pattern") else None
    urls = [u for u in dict.fromkeys(urls) if not lp or lp.search(u)]
    urls = urls[-int(v.get("max_detail", 600)):]
    log(f"    {len(urls)} eventlinks uit sitemap, detailpagina's ophalen (gecached)…")
    out = []
    for u in urls:
        ev = fetch_detail(v, u, cache)
        if ev:
            out.append(ev)
    return out


def detail_extra(v: dict, url: str, cache: dict) -> dict | None:
    """Gestructureerde + tekstuele gegevens van een eventpagina (gecached onder "x|url"); None als niet opgehaald."""
    key = "x|" + url
    c = cache.get(key)
    if c and c.get("v", 1) >= CACHE_VERSION and date.fromisoformat(c["fetched"]) >= TODAY - timedelta(days=int(v.get("detail_ttl_days", 10))):
        return c.get("extra") or {}
    try:
        html = get(url, delay=float(v.get("crawl_delay", 0.6))).text
    except requests.RequestException:
        cache[key] = {"fetched": TODAY.isoformat(), "extra": {}}
        return {}
    extra: dict = {}
    for n in jsonld_events(html):
        ld = event_from_jsonld(n, v, url, "x")
        if ld:
            extra.update({"ld_start": ld.start, "ld_price": ld.price, "ld_genres": ld.genres, "lineup": ld.lineup})
            break
    txt = page_text(html)
    tdt, tstart, tdoors, tprice = extract_from_text(txt)
    if not tprice:
        tprice = price_from_embedded_json(html)
    if not tstart and not tdoors:
        tstart, tdoors = times_from_embedded_json(html)
    extra.update({"start": tstart, "doors": tdoors, "price": tprice})
    if v.get("lineup") and not extra.get("lineup"):
        extra["lineup"] = clean_lineup([x.get_text() for x in soup_of(html).select(v["lineup"])])
    if not extra.get("ld_genres"):
        hints = genre_hints(txt[:1500], limit=4)
        extra["hint_genres"] = hints if len(hints) <= 2 else []
    cache[key] = {"fetched": TODAY.isoformat(), "v": CACHE_VERSION, "extra": extra}
    return extra


def apply_extra(e: Event, x: dict, needs_time: bool) -> None:
    """Regels voor het samenvoegen van eventpaginagegevens met een event uit een overzichtslijst.
    Aanvang ('aanvang/start') op de pagina wint altijd van de lijst; deuren alleen als er niets beters is."""
    st = datetime.fromisoformat(e.start)
    if x.get("start"):
        hh, mm = x["start"]
        if needs_time or (hh, mm) != (st.hour, st.minute):
            e.start, e.time_est = st.replace(hour=hh, minute=mm).isoformat(timespec="minutes"), False
    elif needs_time:
        if x.get("ld_start") and x["ld_start"][11:16] != "00:00":
            e.start = st.replace(hour=int(x["ld_start"][11:13]), minute=int(x["ld_start"][14:16])).isoformat(timespec="minutes")
        elif x.get("doors"):
            e.start = st.replace(hour=x["doors"][0], minute=x["doors"][1]).isoformat(timespec="minutes")
    if not e.price:
        e.price = x.get("price") or x.get("ld_price")
    if not e.genres:
        e.genres = x.get("ld_genres") or x.get("hint_genres") or []
    if not e.lineup and x.get("lineup"):
        e.lineup = x["lineup"]


def enrich_from_detail(v: dict, evs: list[Event], cache: dict) -> None:
    """Vul ontbrekende aanvangstijd/prijs/genre/line-up aan vanaf de eventpagina (gecached, met budget per run)."""
    budget = int(v.get("enrich_max", 120))
    for e in evs:
        needs_time = e.start[11:16] in ("", "00:00")
        if not (needs_time or not e.price or not e.genres) or not e.url or e.url.rstrip("/") == v["url"].rstrip("/"):
            continue
        cached = ("x|" + e.url) in cache
        if not cached:
            if budget <= 0:
                continue
            budget -= 1
        x = detail_extra(v, e.url, cache)
        if x:
            apply_extra(e, x, needs_time)


def audit_venue(v: dict, evs: list[Event], cache: dict) -> dict:
    """Generieke kwaliteitscontrole per podium; de lessen van eerdere fouten, toegepast op álle podia.
    - tijdcontrole: steekproef van eventpagina's; staat daar een expliciete aanvang die structureel afwijkt van de
      lijsttijd (Doornroosje: +1u door foute tijdzone), dan worden de gecontroleerde events gecorrigeerd en wordt
      het podium gemarkeerd (time_shift) zodat `time_is_local` gezet kan worden.
    - datumcluster: >25% van de events op één dag zonder tijd = vrijwel zeker een parserfout (ECI: 74 op 'vandaag');
      die events worden verwijderd.
    - late tijden: >35% van de events om 23:00 of later is verdacht voor een concertpodium.
    - dekking: aandeel events met tijd, prijs, genre.
    """
    a: dict = {}
    n = len(evs)
    if not n:
        return a
    # datumcluster
    by_day = Counter(e.start[:10] for e in evs)
    day, cnt = by_day.most_common(1)[0]
    if n >= 10 and cnt / n > 0.25:
        notime = [e for e in evs if e.start[:10] == day and e.start[11:16] in ("", "00:00")]
        if len(notime) >= 0.8 * cnt:
            for e in notime:
                evs.remove(e)
            a["date_cluster_removed"] = {"day": day, "events": len(notime)}
    # tijdcontrole (steekproef, gecached)
    if v.get("time_check", True) and v.get("type", "auto") not in ("disabled",):
        sample = [e for e in evs if e.start[11:16] not in ("", "00:00") and e.url and e.url.rstrip("/") != v["url"].rstrip("/")]
        sample = sample[:: max(1, len(sample) // 4)][:4]
        offsets = []
        for e in sample:
            x = detail_extra(v, e.url, cache)
            if x and x.get("start"):
                hh, mm = x["start"]
                st = datetime.fromisoformat(e.start)
                diff = round(((hh * 60 + mm) - (st.hour * 60 + st.minute)) / 60, 1)
                offsets.append(diff)
                if diff:
                    e.start = st.replace(hour=hh, minute=mm).isoformat(timespec="minutes")
        if offsets:
            common = Counter(offsets).most_common(1)[0]
            a["time_check"] = {"sampled": len(offsets), "offsets": offsets}
            if common[0] and common[1] >= 2 and common[1] >= len(offsets) - 1 and abs(common[0]) in (1.0, 2.0):
                a["time_shift"] = common[0]
    late = sum(1 for e in evs if e.start[11:16] >= "23:00")
    if n >= 10 and late / n > 0.35:
        a["many_late"] = round(late / n, 2)
    a["coverage"] = {"time": round(sum(1 for e in evs if e.start[11:16] not in ("", "00:00")) / n, 2),
                     "price": round(sum(1 for e in evs if e.price) / n, 2),
                     "genre": round(sum(1 for e in evs if e.genres) / n, 2)}
    return a


def strat_jsonld_detail(v: dict, html: str, base: str, cache: dict) -> list[Event]:
    pages = [html]
    for extra in v.get("list_pages", [])[1:]:
        try:
            pages.append(get(extra, delay=1.0).text)
        except requests.RequestException as ex:
            log(f"    extra pagina mislukt {extra}: {ex}")
    if v.get("list_pages_template"):
        # paginering: doorgaan tot een pagina 404 geeft of geen nieuwe eventlinks meer bevat
        seen_links = set(event_links(v, html, base))
        for n in range(2, int(v.get("list_pages_max", 60)) + 1):
            try:
                r = SESSION.get(v["list_pages_template"].format(n=n + int(v.get("list_pages_offset", 0))), timeout=TIMEOUT)
            except requests.RequestException:
                break
            time.sleep(float(v.get("crawl_delay", 0.6)))
            if r.status_code != 200:
                break
            new_links = [l for l in event_links(v, r.text, base) if l not in seen_links]
            if not new_links:
                break
            seen_links.update(new_links)
            pages.append(r.text)
    links: list[str] = []
    for p in pages:
        for l in event_links(v, p, base):
            if l not in links:
                links.append(l)
    links = links[: int(v.get("max_detail", 400))]   # was 80: Nieuwe Nor en SPOT bleven daardoor op precies 80 events hangen
    log(f"    {len(links)} eventlinks, detailpagina's ophalen (gecached)…")
    out = []
    for l in links:
        ev = fetch_detail(v, l, cache)
        if ev:
            out.append(ev)
    return out


# --- HTML met selectors ----------------------------------------------------------

def strat_html(v: dict, html: str, base: str) -> list[Event]:
    s = soup_of(html)
    out = []
    for item in s.select(v["item"]):
        def pick(key):
            sel = v.get(key)
            return item.select_one(sel) if sel else None
        t = pick("title")
        title = clean(t.get(v["title_attr"]) if t is not None and v.get("title_attr") and t.has_attr(v["title_attr"]) else t.get_text()) if t else None
        # link: uit attribuut (bijv. data-target), uit selector, of de eerste <a>
        url = None
        if v.get("link_attr"):
            holder = item if item.has_attr(v["link_attr"]) else item.find(attrs={v["link_attr"]: True})
            url = urljoin(base, holder[v["link_attr"]]) if holder else None
        if not url:
            a = pick("link") or item.find("a", href=True) or (item if item.name == "a" else None)
            url = urljoin(base, a["href"]) if a and a.has_attr("href") else base
        d = pick("date")
        dt = None
        if d is not None:
            if d.has_attr("datetime") and not v.get("date_text_only"):
                dt = parse_dt(d.get("datetime"))
            dt = dt or parse_dt(clean(d.get_text()))
            if dt is None:
                dt = extract_from_text(d.get_text(" "))[0]
        if not dt and v.get("date_from_url"):
            dt = date_from_url(url)
        if not dt and v.get("group") and v.get("group_date"):
            # datum staat als kop boven een groep kaarten (Willemeen: .we__agenda-row > .we__agenda-item-date "vr 04 sep")
            grp = item.find_parent(class_=v["group"].lstrip(".")) if v["group"].startswith(".") else item.find_parent(v["group"])
            gd = grp.select_one(v["group_date"]) if grp else None
            if gd is not None:
                dt = parse_dt(clean(gd.get_text())) or extract_from_text(gd.get_text(" "))[0]
        if not dt:
            dt = extract_from_text(item.get_text(" "))[0]
        if not (title and dt):
            continue
        # tijd en prijs uit de tekst van het item (bijv. "Open 17:30 / Aanvang 18:00 / € 8,50")
        _, tstart, tdoors, tprice = extract_from_text(item.get_text(" "))
        if not tprice:
            # prijs als data-attribuut (Metropool: data-event-price="17.00" op de ticketknop)
            holder = item if any(k.endswith("price") for k in item.attrs) else item.find(lambda tag: any(k.endswith("price") for k in tag.attrs))
            if holder is not None:
                key = next(k for k in holder.attrs if k.endswith("price"))
                tprice = normalize_price(fmt_price(holder[key]))
        if not tstart and not tdoors:
            # één losse tijd in de kaart zonder label (Willemeen "12:00"): dat is de aanvang
            times = re.findall(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", item.get_text(" "))
            times = [(int(h), int(m)) for h, m in times if int(h) < 24 and int(m) < 60]
            if len(times) == 1:
                tstart = times[0]
        if (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            hh, mm = tstart or tdoors
            dt = dt.replace(hour=hh, minute=mm)
        genres = []
        for ga in as_list(v.get("genre_attr") or []):   # één of meer data-attributen (SPOT: data-genres + data-subgenres)
            holder = item if item.has_attr(ga) else item.find(attrs={ga: True})
            if holder:
                genres += [x.strip() for x in re.split(r"[,/|·•]", str(holder[ga])) if x.strip()]
        for g in item.select(v["genre"]) if v.get("genre") else []:
            # "VR 04 SEP | Rock, Symfo- & Progressive Rock" (De Pul): datumdelen zijn geen genre
            genres += [x.strip() for x in re.split(r"[,/|·•]", clean(g.get_text()) or "") if x.strip() and not re.search(r"\d", x)]
        sub = pick("subtitle")
        status = None
        if item.find(class_=re.compile(r"sold-?out|uitverkocht", re.I)) or re.search(r"\buitverkocht\b|\bsold[ -]?out\b", item.get_text(" "), re.I):
            status = "uitverkocht"
        if item.find(class_=re.compile(r"cancel|afgelast", re.I)) or re.search(r"\bafgelast\b|\bgeannuleerd\b", item.get_text(" "), re.I):
            status = "afgelast"
        out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                         subtitle=clean(sub.get_text()) if sub else None, genres=list(dict.fromkeys(g for g in genres if g)),
                         price=tprice, status=status, source="html"))
    return out


def strat_html_api(v: dict, base: str) -> list[Event]:
    """HTML-fragmenten uit een eigen 'laad meer'-eindpunt (De Pul: query.php?…&amount_of_events_already_shown={offset}
    geeft {"output": "<a class=agenda-event>…"}). Dezelfde CSS-selectors als type: html, maar de bron is de API:
      type: html
      api: "https://…/query.php?source=agenda&amount_of_events_already_shown={offset}"   # {offset} = al getoonde items, {page} = paginanummer
      api_html_key: output          # JSON-sleutel met het fragment; weglaten als het antwoord zelf HTML is
    Stopt als een pagina geen (nieuwe) items meer oplevert."""
    out: list[Event] = []
    seen: set[str] = set()
    offset, page = 0, 1
    while page <= int(v.get("max_pages", 30)):
        url = v["api"].replace("{offset}", str(offset)).replace("{page}", str(page))
        r = get(url, delay=float(v.get("crawl_delay", 0.6)))
        html = r.text
        if v.get("api_html_key"):
            try:
                html = str(_path(r.json(), v["api_html_key"]) or "")
            except ValueError:
                html = str(_path(json.loads(r.text.strip()), v["api_html_key"]) or "")
        n_items = len(soup_of(html).select(v["item"]))
        evs = [e for e in strat_html(v, html, base) if e.url not in seen]
        if not n_items or not evs:
            break
        seen.update(e.url for e in evs)
        out += evs
        offset += n_items
        page += 1
        if "{offset}" not in v["api"] and "{page}" not in v["api"]:
            break
    return out


def strat_facetwp(v: dict, base: str) -> list[Event]:
    """FacetWP (WordPress-plugin voor filters + 'laad meer'; Bibelot): de pagina toont 30 events, de rest komt via een
    JSON-POST naar dezelfde URL met {"action":"facetwp_refresh","data":{"paged":n,…}}; het antwoord bevat "template"
    (HTML-fragment) en settings.pager.total_pages. Zelfde CSS-selectors als type: html.
      type: html
      facetwp: true          # of automatisch: de pagina bevat 'facetwp' (class facetwp-template / facetwp-load-more)
    """
    path = urlparse(base).path.strip("/")
    out: list[Event] = []
    seen: set[str] = set()
    total_pages = int(v.get("max_pages", 20))
    page = 1
    while page <= total_pages:
        body = {"action": "facetwp_refresh", "data": {"facets": {}, "frozen_facets": {}, "http_params": {"get": [], "uri": path, "url_vars": []},
                                                      "template": "wp", "extras": {}, "soft_refresh": 1, "first_load": 0, "paged": page}}
        r = SESSION.post(base, json=body, timeout=TIMEOUT, headers={**(BROWSER_HEADERS if urlparse(base).netloc in _BROWSER_UA_HOSTS else {}), "Accept": "application/json"})
        time.sleep(float(v.get("crawl_delay", 0.6)))
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError:
            break
        html = j.get("template") or ""
        pager = (j.get("settings") or {}).get("pager") or {}
        if pager.get("total_pages"):
            total_pages = min(total_pages, int(pager["total_pages"]))
        evs = [e for e in strat_html(v, html, base) if e.url not in seen]
        if not evs:
            break
        seen.update(e.url for e in evs)
        out += evs
        page += 1
    return out


# bekende WordPress-thema's/plugins met vaste class-namen: automatisch herkend
HTML_PRESETS = [
    {"detect": ".wp_theatre_event", "item": ".wp_theatre_event", "title": ".wp_theatre_event_title", "date": ".wp_theatre_event_startdate, .wp_theatre_event_date",
     "subtitle": ".wp_theatre_event_subtitle, .wp_theatre_event_support_title", "genre": ".wp_theatre_event_categories", "name": "theater-for-wordpress"},
    {"detect": ".event-program", "item": ".event-program", "title": ".event-program__name", "date": ".event-program__date", "subtitle": ".event-program__subtitle",
     "genre": ".event-program__genres, .event-program__tags", "date_from_url": True, "name": "patronaat-thema"},
]


def strat_html_preset(v: dict, html: str, base: str) -> tuple[list[Event], str | None]:
    s = soup_of(html)
    for preset in HTML_PRESETS:
        if len(s.select(preset["detect"])) >= 3:
            cfg = {**preset, **v}
            return strat_html(cfg, html, base), preset["name"]
    return [], None


# ----------------------------------------------------------------------------
# per podium
# ----------------------------------------------------------------------------

def fetch_venue(v: dict, cache: dict) -> tuple[list[Event], str, str, dict]:
    """Geeft (events, gebruikte strategie, opmerking, audit)."""
    base = v["url"]
    t = v.get("type", "auto")
    if t == "disabled" or v.get("enabled") is False:
        return [], "disabled", "uitgeschakeld in venues.yaml", {}

    html = ""
    if t not in ("tribe", "sitemap_detail", "json_api", "stager") and not (t == "html" and v.get("api")):
        html = get(base, delay=float(v.get("crawl_delay", 0))).text

    order = {
        "auto": ["jsonld", "microdata", "embedded", "html_preset", "tribe", "wp_event", "jsonld_detail"],
        "microdata": ["microdata"],
        "jsonld": ["jsonld"], "embedded": ["embedded"], "nextdata": ["embedded"],
        "tribe": ["tribe"], "wp_event": ["wp_event"], "json_api": ["json_api"], "stager": ["stager"], "jsonld_detail": ["jsonld_detail"], "html": ["html"],
        "sitemap_detail": ["sitemap_detail"],
    }.get(t, [t])
    notes = []
    best: tuple[list[Event], str] = ([], "none")
    for strat in order:
        try:
            if strat == "jsonld":
                evs = strat_jsonld(v, html)
            elif strat == "microdata":
                evs = strat_microdata(v, html)
            elif strat == "embedded":
                evs = strat_embedded(v, html)
            elif strat == "tribe":
                evs = strat_tribe(v, base)
            elif strat == "wp_event":
                evs = strat_wp_event(v, base, cache)
            elif strat == "json_api":
                evs = strat_json_api(v, base, cache)
            elif strat == "stager":
                try:
                    evs = strat_stager(v, base, cache)
                except Exception as ex:  # noqa: BLE001 — API veranderd? dan de JSON-LD van de shoppagina
                    notes.append(f"stager-api: {type(ex).__name__} {str(ex)[:80]} -> JSON-LD")
                    evs = strat_jsonld(v, html or get(base).text)
            elif strat == "jsonld_detail":
                evs = strat_jsonld_detail(v, html, base, cache)
            elif strat == "html":
                if v.get("api"):
                    evs = strat_html_api(v, base)
                elif v.get("facetwp") or (v.get("facetwp") is None and "facetwp-template" in html):
                    evs = strat_facetwp(v, base)
                    if len(evs) < 3:
                        evs = strat_html(v, html, base)
                else:
                    evs = strat_html(v, html, base)
                    if v.get("list_pages_template"):
                        # gepagineerde HTML-agenda (Willem Twee ?page=2…): doorgaan tot een pagina niets nieuws oplevert
                        known = {e.url for e in evs}
                        for n in range(2, int(v.get("list_pages_max", 40)) + 1):
                            try:
                                r = SESSION.get(v["list_pages_template"].format(n=n + int(v.get("list_pages_offset", 0))), timeout=TIMEOUT)
                            except requests.RequestException:
                                break
                            time.sleep(float(v.get("crawl_delay", 0.6)))
                            if r.status_code != 200:
                                break
                            more = [e for e in strat_html(v, r.text, base) if e.url not in known]
                            if not more:
                                break
                            known.update(e.url for e in more)
                            evs += more
            elif strat == "html_preset":
                evs, preset_name = strat_html_preset(v, html, base)
                if preset_name:
                    strat = f"html:{preset_name}"
            elif strat == "sitemap_detail":
                evs = strat_sitemap_detail(v, base, cache)
            else:
                notes.append(f"onbekend type {strat}")
                continue
        except requests.RequestException as ex:
            notes.append(f"{strat}: {type(ex).__name__} {str(ex)[:120]}")
            continue
        except Exception as ex:  # noqa: BLE001 — één podium mag de run niet breken
            notes.append(f"{strat}: {type(ex).__name__} {str(ex)[:120]}")
            continue
        evs = [e for e in evs if in_window(datetime.fromisoformat(e.start))]
        if v.get("only_genres"):
            want = {g.lower() for g in v["only_genres"]}
            evs = [e for e in evs if {g.lower() for g in e.genres} & want]
        if len(evs) > len(best[0]):
            best = (evs, strat)
        if len(evs) >= int(v.get("good_enough", 20)):
            break
        notes.append(f"{strat}: {len(evs)} events")
    evs, strat = best
    # ticketshops met server-side data als automatische extra bron: Stager (<slug>.stager.co/shop/default/events, JSON-LD
    # ItemList met 50 komende events incl. tijd) wordt herkend aan ticketlinks op de agenda- of eventpagina's
    if v.get("ticketshops", True) and not v.get("_is_extra"):
        shops = set(re.findall(r"https?://([a-z0-9-]+)\.stager\.co/", (html or "").lower()))
        # alleen shops die bij dít podium horen: Luxor Live linkt ook naar willemeen.stager.co, De Spot naar deoostkerk, Neushoorn
        # naar explorethenorth (festival) en veel sites naar app.stager.co — die zouden andermans events opleveren
        vf = re.sub(r"[^a-z0-9]", "", _fold(v["name"]))
        host = urlparse(v["url"]).netloc.replace("www.", "").split(".")[0].replace("-", "")
        def mine(slug: str) -> bool:
            sf = slug.replace("-", "")
            return slug != "app" and len(sf) >= 3 and (sf in vf or vf in sf or sf in host or host in sf)
        shops = {s_ for s_ in shops if mine(s_)}
        for slug in sorted(shops)[:2]:
            src = {"url": f"https://{slug}.stager.co/shop/default/events", "type": "stager", "enrich": False}
            if src["url"] not in [x.get("url") for x in as_list(v.get("extra_sources") or [])]:
                v = {**v, "extra_sources": as_list(v.get("extra_sources") or []) + [src]}
                notes.append(f"ticketshop herkend: {slug}.stager.co")
    # extra bronnen voor hetzelfde podium (eigen site toont maar 3 weken, de Stager-ticketshop 50 events; of een tweede
    # agendapagina): elk met eigen type/instellingen; dubbele events (zelfde dag + titel) worden later samengevoegd
    for src in as_list(v.get("extra_sources") or []):
        sub = {**v, **src, "extra_sources": None, "category_pages": None, "name": v["name"], "city": v["city"], "_is_extra": True}
        try:
            more, sstrat, snote, _ = fetch_venue(sub, cache)
            known = {e.url.rstrip("/") for e in evs}
            new = [e for e in more if e.url.rstrip("/") not in known]
            evs = evs + new
            notes.append(f"extra bron {src.get('url')}: {len(more)} events via {sstrat}, {len(new)} nieuw")
        except Exception as ex:  # noqa: BLE001
            notes.append(f"extra bron {src.get('url')}: {type(ex).__name__} {str(ex)[:80]}")
    if len(evs) >= int(v.get("min_events", 3)):
        if v.get("enrich", True) and strat not in ("jsonld_detail", "sitemap_detail"):
            try:
                enrich_from_detail(v, evs, cache)
            except Exception as ex:  # noqa: BLE001
                notes.append(f"enrich: {type(ex).__name__}")
        if v.get("category_pages"):
            try:
                cats, excl = category_tags(v, cache)
                before = len(evs)
                evs, tagged = apply_category_tags(evs, cats, excl)
                notes.append(f"{tagged} events getagd via podiumfilters" + (f", {before - len(evs)} uitgesloten (andere locatie)" if before != len(evs) else ""))
            except Exception as ex:  # noqa: BLE001
                notes.append(f"podiumfilters: {type(ex).__name__} {str(ex)[:80]}")
        audit = {}
        try:
            audit = audit_venue(v, evs, cache)
        except Exception as ex:  # noqa: BLE001
            notes.append(f"audit: {type(ex).__name__}")
        if audit.get("time_shift"):
            notes.append(f"LET OP: aanvang op eventpagina's wijkt {audit['time_shift']:+.0f}u af van de lijst -> zet time_is_local of controleer tijdzone")
        if audit.get("date_cluster_removed"):
            notes.append(f"{audit['date_cluster_removed']['events']} events zonder tijd op {audit['date_cluster_removed']['day']} verwijderd (datumcluster)")
        if audit.get("many_late"):
            notes.append(f"{int(audit['many_late']*100)}% van de events begint na 23:00")
        return evs, strat, "; ".join(notes), audit
    return [], "none", "; ".join(notes), {}


def category_tags(v: dict, cache: dict) -> tuple[dict[str, set[str]], set[str]]:
    """Podium-eigen genre-/type-/locatiefilters als bron van tags (venues.yaml -> category_pages).

    Veel podia hebben op hun agenda een filter per genre, type of locatie (Tivoli: ?sf_genre=pop, Doornroosje: ?genre=metal
    en ?location=de-vereeniging, FLUOR: /programma/categorie/dance/). Die indeling is door de programmeurs zelf gemaakt en
    dus betrouwbaarder dan woorden uit de beschrijving. Per filter (slug) worden de overzichtspagina('s) opgehaald en de
    eventlinks verzameld. Resultaat: (eventurl -> {tags}, uit te sluiten eventurls). De tags gaan door dezelfde regels
    (genres.yaml) als andere podiumtags en sturen zo hoofdgenre, subgenre én type (Muziek/Overig); `exclude`-slugs
    (bv. een locatiefilter voor een ander gebouw dat al als eigen podium in venues.yaml staat) verwijderen events.
    Eén keer per dag per podium gecached (cache-sleutel catpages|<podium>). Eén filtergroep of een lijst:

      category_pages:
        - url:   "https://…/programma/?genre={slug}"            # eerste pagina
          paged: "https://…/programma/page/{n}/?genre={slug}"   # optioneel: vervolgpagina's (n >= 2); stop bij 404/geen nieuwe links
          max_pages: 30
          link_pattern: "…"                                     # optioneel: alleen voor deze pagina's (anders die van het podium)
          tags: {pop: Pop, "kennis-debat": "Kennis & debat", "soul-funk-jazz": [Soul, Funk, Jazz]}
        - url: "https://…/programma/?location={slug}"
          exclude: [de-vereeniging, waalhalla]
    """
    cfgs = as_list(v.get("category_pages") or [])
    cfgs = [c for c in cfgs if c.get("url") and (c.get("tags") or c.get("exclude"))]
    if not cfgs:
        return {}, set()
    key = f"catpages|{v['name']}"
    c = cache.get(key)
    if c and c.get("fetched") == TODAY.isoformat() and "map" in c:
        return {u: set(t) for u, t in c["map"].items()}, set(c.get("exclude", []))
    base = v["url"]
    out: dict[str, set[str]] = {}
    excl: set[str] = set()
    delay = float(v.get("crawl_delay", 0.6))
    pages_read = n_cat = 0

    def links_for(cfg: dict, slug: str) -> set[str]:
        nonlocal pages_read
        vv = dict(v, link_pattern=cfg["link_pattern"]) if cfg.get("link_pattern") else v
        seen: set[str] = set()
        for n in range(1, int(cfg.get("max_pages", 30)) + 1):
            if n == 1:
                page_url = cfg["url"].format(slug=slug)
            elif cfg.get("paged"):
                page_url = cfg["paged"].format(slug=slug, n=n + int(cfg.get("page_offset", 0)))
            else:
                break
            try:
                r = SESSION.get(page_url, timeout=TIMEOUT)
            except requests.RequestException:
                break
            time.sleep(delay)
            if r.status_code != 200:
                break
            pages_read += 1
            new = [l for l in event_links(vv, r.text, base) if l not in seen]
            if not new:
                break
            seen.update(new)
        return {l.rstrip("/") for l in seen}

    for cfg in cfgs:
        for slug, labels in (cfg.get("tags") or {}).items():
            n_cat += 1
            for l in links_for(cfg, slug):
                out.setdefault(l, set()).update(as_list(labels))
        for slug in as_list(cfg.get("exclude") or []):
            n_cat += 1
            excl.update(links_for(cfg, slug))
    log(f"    podiumfilters: {n_cat} categorieën, {pages_read} pagina's, {len(out)} events getagd, {len(excl)} uitgesloten")
    cache[key] = {"fetched": TODAY.isoformat(), "map": {u: sorted(t) for u, t in out.items()}, "exclude": sorted(excl)}
    return out, excl


def apply_category_tags(evs: list[Event], cats: dict[str, set[str]], exclude: set[str] | None = None) -> tuple[list[Event], int]:
    """Voegt de podiumfilter-tags toe aan e.genres (vooraan: ze zijn betrouwbaarder dan tekst-hints) en verwijdert
    uitgesloten events. Geeft (events, aantal getagde events)."""
    n = 0
    kept = []
    for e in evs:
        u = (e.url or "").rstrip("/")
        if exclude and u in exclude:
            continue
        kept.append(e)
        tags = cats.get(u)
        if not tags:
            continue
        n += 1
        e.genres = sorted(tags) + [g for g in e.genres if g not in tags]
    return kept, n


def relabel_by_location(events: list[Event], venues: list[dict]) -> int:
    """Een podium dat ook elders programmeert (Paradiso in Tolhuistuin, Doornroosje in De Vereeniging) levert events die
    bij dat andere gebouw horen. Staat de genoemde locatie zelf als podium in venues.yaml, dan verhuist het event
    daarheen (venue/stad); de dedupe hieronder haalt daarna de dubbele met het eigen programma van dat podium weg."""
    by_name = {}
    for v in venues:
        by_name[_fold(v["name"])] = v
        for a in as_list(v.get("aliases") or []):
            by_name[_fold(a)] = v
    n = 0
    for e in events:
        if not e.location:
            continue
        f = _fold(e.location)
        target = by_name.get(f) or next((v for k, v in by_name.items() if len(k) > 4 and (f.startswith(k + " ") or f.startswith(k + " -"))), None)
        if target and target["name"] != e.venue:
            e.venue, e.city = target["name"], target["city"]
            n += 1
    return n


def _richness(e: Event) -> int:
    return len(e.genres) * 2 + (3 if e.price else 0) + (3 if e.start[11:16] not in ("", "00:00") else 0) + len(e.lineup) + (1 if e.subtitle else 0)


def dedupe(events: list[Event]) -> list[Event]:
    """Dubbele events weg: dezelfde URL binnen een podium, en daarna hetzelfde podium + dag + titel (bv. na verhuizing op
    locatie, of als een site een event twee keer toont). De rijkste versie (tijd, prijs, genres, line-up) blijft."""
    seen, out = {}, []
    for e in events:
        key = (e.venue, e.url.rstrip("/").lower()) if e.url else (e.venue, e.title.lower(), e.start[:10])
        if key in seen:
            old = seen[key]
            if _richness(e) > _richness(old):
                out[out.index(old)] = e
                seen[key] = e
            continue
        seen[key] = e
        out.append(e)
    by_day: dict[tuple, list[Event]] = {}
    final = []
    for e in out:
        day_key = (e.venue, e.start[:10])
        old = next((o for o in by_day.get(day_key, []) if _same_event(o, e)), None)
        if old is not None:
            rich, poor = (e, old) if _richness(e) > _richness(old) else (old, e)
            # velden aanvullen uit de armere versie; de URL van de eigen site gaat vóór die van een ticketshop
            if not rich.price and poor.price:
                rich.price = poor.price
            if rich.start[11:16] in ("", "00:00") and poor.start[11:16] not in ("", "00:00"):
                rich.start = poor.start
            if not rich.genres and poor.genres:
                rich.genres = poor.genres
            if not rich.subtitle and poor.subtitle:
                rich.subtitle = poor.subtitle
            if re.search(r"stager\.co|tickets?\.|shop\.|eventix|paylogic|ticketmaster|weeztix|tixly", rich.url or "") and poor.url and not re.search(r"stager\.co|tickets?\.|shop\.|eventix|paylogic|ticketmaster|weeztix|tixly", poor.url):
                rich.url = poor.url
            if rich is e:
                final[final.index(old)] = e
                by_day[day_key][by_day[day_key].index(old)] = e
            continue
        by_day.setdefault(day_key, []).append(e)
        final.append(e)
    return final


def _title_key(t: str) -> str:
    t = NOISE_PAREN.sub("", t or "")
    t = re.sub(r"\b(uitverkocht|sold out|afgelast|verplaatst|nieuwe datum|support|presents?|live|concert|tour|show|20\d\d)\b", " ", t, flags=re.I)
    t = re.sub(r"['’`´\"“”.,:;!?+&]", "", t)   # 90's / 90’s / 90s
    return re.sub(r"\s+", " ", _fold(t)).strip()


def _same_event(a: Event, b: Event) -> bool:
    """Zelfde event op dezelfde dag bij hetzelfde podium? Titels gelijk, of de ene bevat de andere ("In The Flesh?" vs
    "In The Flesh - The Dutch Pink Floyd"), of ze delen het grootste deel van hun woorden, of — als de titels niets
    gemeen hebben (site: "Zeeuwse coverbands", ticketshop: "Band on the Run") — beide hebben dezelfde aanvangstijd."""
    ta, tb = _title_key(a.title), _title_key(b.title)
    if ta and tb:
        if ta == tb or ta in tb or tb in ta:
            return True
        wa, wb = set(ta.split()), set(tb.split())
        common = wa & wb
        if len(common) >= 2 and len(common) >= min(len(wa), len(wb)) * 0.6:
            return True
    sa, sb = a.start[11:16], b.start[11:16]
    if sa and sb and sa not in ("", "00:00") and sb not in ("", "00:00"):
        ha, ma = int(sa[:2]), int(sa[3:]); hb, mb = int(sb[:2]), int(sb[3:])
        if abs((ha * 60 + ma) - (hb * 60 + mb)) <= 30 and {a.source, b.source} & {"stager", "jsonld", "flight_json"} and a.source != b.source:
            return True   # zelfde tijd, verschillende bronnen (site vs ticketshop): één event met twee namen
    return False


def main(only: list[str] | None = None) -> int:
    venues = yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8"))
    seen_path, cache_path = STATE / "seen.json", STATE / "detail_cache.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    first_run = not seen

    all_events: list[Event] = []
    report = {"generated": datetime.now().isoformat(timespec="seconds"), "venues": []}
    def run_one(v):
        t0 = time.time()
        try:
            evs, strat, note, audit = fetch_venue(v, cache)
        except Exception as ex:  # noqa: BLE001
            evs, strat, note, audit = [], "error", f"{type(ex).__name__}: {str(ex)[:200]}", {}
            traceback.print_exc()
            log(traceback.format_exc()[-1500:])
        evs = dedupe(evs)
        log(f"== {v['name']} ({v['city']}): {len(evs)} events via {strat} ({time.time()-t0:.0f}s) {note}")
        return v, evs, strat, note, audit

    todo = [v for v in venues if not only or v["name"] in only]
    # podia parallel (elk podium zelf netjes sequentieel met zijn eigen crawl_delay)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "8"))) as pool:
        results = list(pool.map(run_one, todo))
    for v, evs, strat, note, audit in results:
        # volledigheidsindicatoren: weekenddekking (vr/za met minstens één event in de komende 8 weken, van 16) en horizon
        # (verste datum). Een podium met 400+ plaatsen heeft normaal (bijna) elk weekend iets: minder dan 11/16 is verdacht dun.
        wk_days = {e.start[:10] for e in evs if TODAY <= date.fromisoformat(e.start[:10]) < TODAY + timedelta(weeks=8)
                   and date.fromisoformat(e.start[:10]).weekday() in (4, 5)}
        horizon = max((e.start[:10] for e in evs), default=None)
        audit = dict(audit or {})
        audit["weekend"] = len(wk_days)
        audit["horizon"] = horizon
        cap = v.get("capacity") or 0
        if evs and cap >= 400 and len(wk_days) < 11:
            audit["thin"] = True
            note = (note + "; " if note else "") + f"dun programma: {len(wk_days)}/16 weekenddagen met een event (podium van {cap} plaatsen)"
        report["venues"].append({"name": v["name"], "city": v["city"], "url": v["url"], "strategy": strat,
                                 "events": len(evs), "note": note, "ok": len(evs) > 0, "audit": audit})
        all_events.extend(evs)
    # events die een podium elders programmeert (Paradiso in Tolhuistuin) horen bij dat andere podium; daarna dubbelen weg
    moved = relabel_by_location(all_events, venues)
    before = len(all_events)
    all_events = dedupe(all_events)
    report["relocated"] = moved
    report["deduped"] = before - len(all_events)
    log(f"Locaties: {moved} events verhuisd naar het podium waar ze plaatsvinden; {before - len(all_events)} dubbele events samengevoegd")

    # 'nieuw' bepalen; een podium dat voor het eerst meedoet levert geen 'nieuwe' events op
    known_venues = {k.split("|", 1)[0] for k in seen}
    backdate = (TODAY - timedelta(days=30)).isoformat()
    for e in all_events:
        key = f"{e.venue}|{e.url}"
        if key not in seen:
            seen[key] = TODAY.isoformat() if (e.venue in known_venues or not seen) else backdate
        e.first_seen = seen[key]
    # opschonen: sleutels van events die > 60 dagen weg zijn
    live = {f"{e.venue}|{e.url}" for e in all_events}
    for k in list(seen):
        if k not in live and date.fromisoformat(seen[k]) < TODAY - timedelta(days=60):
            del seen[k]
    if first_run:
        # eerste run: niets als 'nieuw' markeren, anders is alles nieuw
        for e in all_events:
            e.first_seen = (TODAY - timedelta(days=30)).isoformat()
            seen[f"{e.venue}|{e.url}"] = e.first_seen

    # --- verrijking: artiesten, genres, type, prijs ---
    vmeta = {v["name"]: v for v in venues}
    unknown_genres: dict[str, int] = {}
    adb = artistdb.load()
    seen_ev_path = STATE / "artists_seen.json"
    seen_ev = set(json.loads(seen_ev_path.read_text())) if seen_ev_path.exists() else set()
    kind_learn_path, sub_learn_path = STATE / "kind_learn.json", STATE / "subgenre_learn.json"
    kind_learn = json.loads(kind_learn_path.read_text()) if kind_learn_path.exists() else {}
    sub_learn = json.loads(sub_learn_path.read_text()) if sub_learn_path.exists() else {}
    for f, v in list(sub_learn.get("votes", {}).items()):      # hernoemde hoofdgenres in het leergeheugen omzetten
        sub_learn["votes"][f] = artistdb.migrate_groups(v)
    for f, v in sub_learn.get("accepted", {}).items():
        v["group"] = artistdb.GROUP_RENAMES.get(v.get("group"), v.get("group"))
    for e in all_events:
        e.section = vmeta.get(e.venue, {}).get("section", "poppodium")
        e.price = normalize_price(e.price)
        e.artists = extract_artists(e.title, e.subtitle)
        if e.lineup:
            known = {artist_key(a) for a in e.artists}
            e.artists += [a for a in e.lineup if artist_key(a) not in known and len(artist_key(a)) > 1][: 15 - len(e.artists)]
            if not e.subtitle and len(e.lineup) > 1:
                e.subtitle = "met " + ", ".join(e.lineup[:8]) + (" e.a." if len(e.lineup) > 8 else "")
        e.genre_norm, unk = normalize_genres(e.genres, e.title, e.subtitle or "")
        for u in unk:
            unknown_genres[u] = unknown_genres.get(u, 0) + 1
        e.kind, sure = classify_kind_ex(e.title, e.subtitle, e.genres, e.genre_norm, e.start, kind_learn)
        if sure:
            learn_kinds(kind_learn, e.genres, e.kind)
        e.subgenres, unk_sub = normalize_subgenres(e.genres, e.genre_norm, sub_learn)
        learn_subgenres(sub_learn, unk_sub, e.genre_norm)
        e.price_num = price_number(e.price)
        e.free = e.price_num == 0.0
        if e.kind in ("concert", "festival", "club"):
            artistdb.record_event(adb, e.artists, e.venue, e.genres, e.genre_norm, f"{e.venue}|{e.url}", seen_ev)
    try:
        artistdb.musicbrainz_lookup(adb, budget=int(os.environ.get("MB_BUDGET", "150")), log=log)
        artistdb.spotify_lookup(adb, budget=int(os.environ.get("SPOTIFY_BUDGET", "300")), log=log)
    except Exception as ex:  # noqa: BLE001
        log(f"externe lookup mislukt: {type(ex).__name__}: {str(ex)[:120]}")
    artistdb.derive_genres(adb)
    filled = 0
    for e in all_events:
        if not e.genre_norm and e.artists:
            g, subs = artistdb.genres_for(adb, e.artists)
            if g:
                e.genre_norm = g
                if not e.genres:
                    e.genres = subs
                filled += 1
    artistdb.save(adb)
    seen_ev_path.write_text(json.dumps(sorted(seen_ev)[-30000:]))
    nk, ns = promote_kinds(kind_learn), promote_subgenres(sub_learn)
    for e in all_events:
        if not e.subgenres:
            e.subgenres, _ = normalize_subgenres(e.genres, e.genre_norm, sub_learn)
    kind_learn_path.write_text(json.dumps(kind_learn, ensure_ascii=False, indent=0), encoding="utf-8")
    sub_learn_path.write_text(json.dumps(sub_learn, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"Zelflerend: {len(kind_learn.get('accepted', {}))} tag->type-koppelingen (+{nk}), {len(sub_learn.get('accepted', {}))} geleerde subgenres (+{ns})")
    report["learned"] = {"kind_tags": kind_learn.get("accepted", {}), "subgenres": sub_learn.get("accepted", {})}
    report["subgenre_labels"] = {k: subgenre_label(k) for e in all_events for k in e.subgenres}
    report["subgenre_labels"].update({k: v["label"] for k, v in sub_learn.get("accepted", {}).items()})
    report["subgenre_groups"] = {k: subgenre_group(k) for e in all_events for k in e.subgenres}
    report["subgenre_groups"].update({k: v["group"] for k, v in sub_learn.get("accepted", {}).items()})
    log(f"Artiestenbank: {len(adb)} artiesten; {filled} events kregen genre via de kennisbank")

    # --- reeksengeheugen: prijs/tijd van terugkerende events onthouden en aanvullen ---
    sdb, sseen = seriesdb.load()
    for e in all_events:
        seriesdb.record(sdb, sseen, e.venue, e.title, f"{e.venue}|{e.url}", e.price, e.start, e.kind)
    est_p = est_t = est_k = 0
    for e in all_events:
        gp, gt, gk = seriesdb.guess(sdb, e.venue, e.title)
        # type: een reeks die eerder duidelijk als feest/talk/… herkend werd, corrigeert het standaardtype "concert"
        if gk and gk != e.kind and e.kind == "concert":
            e.kind = gk
            est_k += 1
        # prijs wordt NOOIT geschat: alleen wat op de site staat telt (de reeks onthoudt prijzen wel, voor onderzoek)
        if gt and not (len(e.start) > 10 and e.start[11:16] != "00:00"):
            e.start, e.time_est = f"{e.start[:10]}T{gt}", True
            est_t += 1
    seriesdb.save(sdb, sseen)
    log(f"Reeksengeheugen: {len(sdb)} reeksen; {est_t} tijden en {est_k} typen overgenomen uit eerdere edities (prijzen worden nooit geschat)")
    report["series"] = len(sdb)
    report["estimated"] = {"price": est_p, "time": est_t, "kind": est_k}
    report["unknown_genres"] = dict(sorted(unknown_genres.items(), key=lambda x: -x[1])[:150])
    report["artists"] = len(adb)
    report["genre_groups"] = {k: v["label"] for k, v in _taxonomy()[0].items()}
    for rv in report["venues"]:
        meta = vmeta.get(rv["name"], {})
        rv["section"] = meta.get("section", "poppodium")
        rv["capacity"] = meta.get("capacity")
    report["kinds"] = {k: sum(1 for e in all_events if e.kind == k) for k in ("concert", "club", "festival", "talk", "other")}

    # --- archief: elk event dat ooit is gezien blijft bewaard (onderzoeksdata: programmering, prijzen, genres per podium) ---
    arch_path = DATA / "archive.json"
    archive = json.loads(arch_path.read_text(encoding="utf-8")) if arch_path.exists() else {}
    for e in all_events:
        k = f"{e.venue}|{e.url}"
        rec = archive.get(k, {"first_seen": e.first_seen or TODAY.isoformat()})
        rec.update({"venue": e.venue, "city": e.city, "title": e.title, "start": e.start, "url": e.url, "subtitle": e.subtitle,
                    "genres": e.genres, "genre_norm": e.genre_norm, "subgenres": e.subgenres, "kind": e.kind, "price": e.price,
                    "price_num": e.price_num, "status": e.status, "artists": e.artists, "lineup": e.lineup, "section": e.section,
                    "last_seen": TODAY.isoformat()})
        archive[k] = rec
    arch_path.write_text(json.dumps(archive, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    report["archive"] = len(archive)
    log(f"Archief: {len(archive)} events bewaard (data/archive.json)")

    all_events.sort(key=lambda e: (e.start, e.venue, e.title))
    (DATA / "events.json").write_text(json.dumps([asdict(e) for e in all_events], ensure_ascii=False, indent=1), encoding="utf-8")
    report["total_events"] = len(all_events)
    (DATA / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    # het logboek van deze run meeschrijven (data/run.log), zodat een mislukt podium ook zonder de GitHub-console te onderzoeken is
    (DATA / "run.log").write_text("\n".join(_LOG)[-400000:], encoding="utf-8")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for r in report["venues"] if r["ok"])
    log(f"\nKlaar: {len(all_events)} events uit {ok}/{len(report['venues'])} podia")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
