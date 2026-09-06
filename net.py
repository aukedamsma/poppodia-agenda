"""net.py — HTTP-laag: sessie, drie-traps escalatie (eigen UA -> browser-headers -> Chrome-TLS), blokkadedetectie."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import requests

from common import TIMEOUT, UA, log


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})


BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
BROWSER_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8", "Upgrade-Insecure-Requests": "1"}
_BROWSER_UA_HOSTS: set[str] = set()   # hosts die onze eigen user-agent weigeren (Cloudflare 403: Het Podium, So What!)


_BLOCK_RE = re.compile(r"just a moment|cf-chl|challenge-platform|attention required|access denied|are you a robot|captcha|bot verification|ddos-guard|incapsula|_Incapsula_Resource", re.I)


def _looks_blocked(r) -> bool:
    """Een 200 met een botmuur erin (Cloudflare 'Just a moment…', Incapsula): geen echte pagina. Ook een HTML-antwoord op
    een JSON-eindpunt (wp-json/…) wijst daarop (Bolwerk, Estrado: 'tribe: JSONDecodeError')."""
    try:
        ct = (r.headers.get("content-type") or "").lower()
        head = (r.text or "")[:3000]
    except Exception:  # noqa: BLE001
        return False
    if _BLOCK_RE.search(head) and len(r.text or "") < 20000:
        return True
    if "/wp-json/" in str(getattr(r, "url", "")) and "json" not in ct and head.lstrip()[:1] in ("<", ""):
        return True
    # een sitemap zonder <urlset>/<sitemapindex> is geen sitemap maar een botmuur- of foutpagina (Q-factory: 202-challenge)
    if str(getattr(r, "url", "")).lower().endswith(".xml") and "<urlset" not in head and "<sitemapindex" not in head:
        return True
    if getattr(r, "status_code", 200) == 202:
        return True
    return False


_IMPERSONATE_HOSTS: set[str] = set()   # hosts waar alleen een browser-TLS-handdruk (curl_cffi) door de botmuur komt


def _impersonated_get(url: str, **kw):
    """Zelfde GET, maar met de TLS-vingerafdruk van Chrome (curl_cffi). Cloudflare/Vercel-botmuren (GIGANT, Het Podium,
    Q-factory) keuren python-requests af op de TLS-handdruk, niet op de headers. Optionele dependency."""
    try:
        from curl_cffi import requests as creq  # type: ignore
    except ImportError:
        return None
    try:
        return creq.get(url, impersonate="chrome", timeout=TIMEOUT, headers=kw.get("headers") or BROWSER_HEADERS,
                        params=kw.get("params"), allow_redirects=True)
    except Exception as ex:  # noqa: BLE001
        log(f"    {urlparse(url).netloc}: browser-TLS mislukt: {type(ex).__name__}")
        return None


def http_get(url: str, **kw):
    """GET met botmuur-escalatie: eigen user-agent -> browser-headers -> browser-TLS (curl_cffi). Onthoudt per host wat nodig was."""
    host = urlparse(url).netloc
    if host in _IMPERSONATE_HOSTS:
        r = _impersonated_get(url, **kw)
        if r is not None:
            return r
    if host in _BROWSER_UA_HOSTS and "headers" not in kw:
        kw["headers"] = BROWSER_HEADERS
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    blocked = getattr(r, "status_code", 200) in (403, 406, 429, 503) or _looks_blocked(r)
    if blocked and host not in _BROWSER_UA_HOSTS:
        # de site weigert de eigen user-agent van de agenda; probeer één keer als gewone browser (robots.txt staat crawlen toe)
        kw["headers"] = BROWSER_HEADERS
        r2 = SESSION.get(url, timeout=TIMEOUT, **kw)
        if r2.ok and not _looks_blocked(r2):
            _BROWSER_UA_HOSTS.add(host)
            log(f"    {host}: eigen user-agent geweigerd ({r.status_code}), verder als browser")
            return r2
        blocked = True
    if blocked:
        r3 = _impersonated_get(url, **kw)
        if r3 is not None and r3.ok and not _looks_blocked(r3):
            _IMPERSONATE_HOSTS.add(host)
            log(f"    {host}: botmuur ({r.status_code}) -> verder met browser-TLS-handdruk")
            return r3
    return r


def http_post(url: str, **kw):
    """POST met dezelfde botmuur-escalatie als http_get: hosts die alleen met Chrome-TLS door de muur komen (of dat na een
    403 blijken te doen) gaan via curl_cffi; anders de gewone sessie. Gebruikt voor Stager-sessies, FacetWP en GraphQL."""
    host = urlparse(url).netloc
    def _cffi():
        try:
            from curl_cffi import requests as creq  # type: ignore
        except ImportError:
            return None
        try:
            return creq.post(url, impersonate="chrome", timeout=TIMEOUT, params=kw.get("params"), json=kw.get("json"),
                             data=kw.get("data"), headers={**BROWSER_HEADERS, **(kw.get("headers") or {})})
        except Exception as ex:  # noqa: BLE001
            log(f"    {host}: browser-TLS (POST) mislukt: {type(ex).__name__}")
            return None
    if host in _IMPERSONATE_HOSTS:
        r = _cffi()
        if r is not None:
            return r
    r = SESSION.post(url, timeout=TIMEOUT, **{k: v for k, v in kw.items() if k in ("params", "json", "data", "headers")})
    if getattr(r, "status_code", 200) in (403, 406, 429, 503) or _looks_blocked(r):
        r2 = _cffi()
        if r2 is not None and r2.ok and not _looks_blocked(r2):
            _IMPERSONATE_HOSTS.add(host)
            log(f"    {host}: botmuur op POST ({r.status_code}) -> verder met browser-TLS-handdruk")
            return r2
    return r


def get(url: str, delay: float = 0.0, **kw):
    if delay:
        time.sleep(delay)
    r = http_get(url, **kw)
    r.raise_for_status()
    return r
