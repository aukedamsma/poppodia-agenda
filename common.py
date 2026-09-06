"""common.py — gedeelde basis: paden, TODAY/HORIZON, het Event-datamodel, log(), tekst- en soup-hulpjes.
Geen netwerk, geen parsing-regels: alles wat elke andere module nodig heeft zonder iets terug te hoeven importeren."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from html import unescape
from pathlib import Path

from bs4 import BeautifulSoup



ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = ROOT / "state"

UA = "poppodia-agenda/1.0 (persoonlijke concertagenda; 1 run per dag; contact via github.com/aukedamsma/poppodia-agenda)"
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
    co_venues: list[str] = field(default_factory=list)    # medeorganiserende podia (BIRD + Rotown: Zwangere Guy in Maassilo)
    ticket_url: str | None = None  # ticketshop-link als die bekend is; `url` is altijd de agendapagina van het podium, nooit de shop
    id: str = ""                   # stabiele identiteit "podium|bron-url" (voor 'nieuw', archief en het lijstje in de UI)
    subgenres: list[str] = field(default_factory=list)    # canonieke subgenres (genres.yaml -> subgenres, + zelfgeleerd)
    price_est: bool = False        # prijs geschat uit eerdere edities van dezelfde reeks (state/series.json)
    time_est: bool = False         # aanvangstijd idem
    time_src: str | None = None    # herkomst als de eventpagina de lijsttijd verving: label | after_date | paren | schedule | embedded
    price_src: str | None = None   # herkomst van de prijs: shop | list | jsonld | labeled | embedded | text (zie PRICE_RANK)
    section: str = "poppodium"     # poppodium | overig (uit venues.yaml)


# ----------------------------------------------------------------------------
# hulpfuncties
# ----------------------------------------------------------------------------

_LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    if len(_LOG) < 5000:
        _LOG.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = unescape(unescape(str(s)))  # WP REST levert soms dubbel gecodeerde titels ("&amp;#038;")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def in_window(dt: datetime | None) -> bool:
    return dt is not None and TODAY - timedelta(days=1) <= dt.date() <= HORIZON


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def page_text(html: str) -> str:
    """Zichtbare tekst van het hoofddeel van een pagina (zonder scripts/styles/nav zover herkenbaar)."""
    s = soup_of(html)
    for t in s(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    # site-kop weg (bevat menu), maar een <header> binnen het event (datum/tijd/prijs erin) blijft staan
    for t in s("header"):
        if t.find("nav") is not None or t.find("ul") is not None or (t.parent is not None and t.parent.name == "body"):
            t.decompose()
    whole = s.body or s
    body = s.find("main") or s.find("article") or whole
    txt = re.sub(r"\s+", " ", body.get_text(" "))
    if body is not whole:
        # <main> is soms maar een fragment (EKKO: alleen de beschrijving; datum, aanvang en prijs staan in een <section>
        # ernaast): is het minder dan de helft van de pagina, neem dan de hele pagina
        full = re.sub(r"\s+", " ", whole.get_text(" "))
        if len(txt) < 0.5 * len(full):
            txt = full
    return txt
