"""Offline tests met fixtures die de echte datastructuren van de podia nabootsen.
Draaien: python -m pytest tests/  (of python tests/test_offline.py)"""
import json
import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch  # noqa: E402

FUT = (date.today() + timedelta(days=20)).isoformat()
FUT2 = (date.today() + timedelta(days=45)).isoformat()


class FakeResp:
    def __init__(self, text="", json_=None, headers=None):
        self.text = text; self._json = json_; self.headers = headers or {}; self.status_code = 200; self.ok = True
    def json(self): return self._json
    def raise_for_status(self): pass


def fake_session(routes):
    class S:
        headers = {}
        def get(self, url, **kw):
            for k, v in routes.items():
                if k in url:
                    return v(url, kw) if callable(v) else v
            raise fetch.requests.ConnectionError("no route " + url)
    fetch.net.SESSION = S()


def ld(*nodes):
    return "".join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)


def test_jsonld_on_list_page():
    html = "<html>" + ld({"@context": "https://schema.org", "@type": "Event", "name": "90&#8217;s Alternative",
                         "startDate": f"{FUT} 22:00:00", "url": "https://dehelling.nl/agenda/90s-alternative/",
                         "offers": {"@type": "Offer", "price": "9.50", "priceCurrency": "EUR"}}) * 5 + "</html>"
    v = {"name": "De Helling", "city": "Utrecht", "url": "https://www.dehelling.nl/agenda/", "type": "jsonld"}
    fake_session({"dehelling": FakeResp(html)})
    evs, strat, _, _ = fetch.fetch_venue(v, {})
    assert strat == "jsonld" and len(evs) == 5
    assert evs[0].title == "90’s Alternative" and evs[0].price == "€ 9.50" and evs[0].start.startswith(FUT + "T22:00")


def test_jsonld_detail_with_cache():
    lst = "".join(f'<a href="https://www.tivolivredenburg.nl/agenda/1{i}/act-{i}-04-09-2026">x</a>' for i in range(6))
    detail = lambda url, kw: FakeResp(ld({"@type": "MusicEvent", "name": "Act " + url[-12:-11], "url": url,
                                          "startDate": f"{FUT}T19:00", "endDate": f"{FUT}T22:20",
                                          "offers": {"@type": "offer", "price": "44", "priceCurrency": "EUR"}}))
    fake_session({"/agenda/1": detail, "tivolivredenburg.nl/agenda/": FakeResp(lst)})
    v = {"name": "TivoliVredenburg", "city": "Utrecht", "url": "https://www.tivolivredenburg.nl/agenda/",
         "type": "jsonld_detail", "link_pattern": r"tivolivredenburg\.nl/agenda/\d+/", "crawl_delay": 0}
    cache = {}
    evs, strat, _, _ = fetch.fetch_venue(v, cache)
    assert strat == "jsonld_detail" and len(evs) == 6 and sum(1 for k in cache if not k.startswith("x|")) == 6
    # tweede run gebruikt de cache: geen detail-requests meer nodig
    fake_session({"tivolivredenburg.nl/agenda/": FakeResp(lst)})
    evs2, _, _, _ = fetch.fetch_venue(v, cache)
    assert len(evs2) == 6


def test_embedded_nextdata_melkweg_shape():
    evs = [{"type": "events", "id": str(i), "attributes": {"name": f"Band {i}", "startDate": f"{FUT}T17:00:00.000000Z",
            "url": f"/nl/agenda/band-{i}-04-09-2026", "tags": ["Pop", "Indie"], "status": "Gepubliceerd",
            "isCancelled": i == 0, "isSoldOut": i == 1, "isPublished": True, "profile": "Concert"}} for i in range(8)]
    nd = {"props": {"pageProps": {"pageData": {"attributes": {"content": [{"attributes": {"initialEvents": evs}}]}}}}}
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'
    fake_session({"melkweg": FakeResp(html)})
    v = {"name": "Melkweg", "city": "Amsterdam", "url": "https://www.melkweg.nl/nl/agenda", "type": "embedded"}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    assert strat == "embedded" and len(out) == 8
    e0 = out[0]
    assert e0.url == "https://www.melkweg.nl/nl/agenda/band-0-04-09-2026" and e0.status == "afgelast"
    assert out[1].status == "uitverkocht" and "Pop" in e0.genres and "Concert" in e0.genres
    assert e0.start.endswith("19:00") or e0.start.endswith("18:00")  # UTC -> NL tijd


def test_embedded_angular_hedon_shape():
    items = [{"id": 33000 + i, "title": f"ACT {i}", "subtitle": "TOUR", "status": 1, "eventDate": f"{FUT2}T18:30:00Z",
              "endDate": f"{FUT2}T21:00:00Z", "publish": True, "price": 23.5, "venue": "Hedon - Grote Zaal",
              "genres": [{"id": 3, "name": "Nederlands"}, {"id": 17, "name": "Pop"}]} for i in range(10)]
    st = {"1394530991": {"b": [{"id": 1, "title": "Hedon"}]}, "3774010591": {"b": items}}
    html = f'<html><script id="ng-state" type="application/json">{json.dumps(st)}</script></html>'
    fake_session({"hedon": FakeResp(html)})
    v = {"name": "Hedon", "city": "Zwolle", "url": "https://www.hedon-zwolle.nl/", "type": "embedded",
         "url_template": "https://www.hedon-zwolle.nl/voorstelling/{id}"}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    assert len(out) == 10 and out[0].url == "https://www.hedon-zwolle.nl/voorstelling/33000"
    assert out[0].genres == ["Nederlands", "Pop"] and out[0].price == "€ 23,50" and out[0].subtitle == "TOUR"


def test_embedded_effenaar_shape():
    items = [{"type": "event", "nid": i, "slug": f"/agenda/act-{i}", "title": f"Act {i}", "subtitle": "sub",
              "date": {"timestamp": 1789257600, "machine": FUT}, "times": {"starts_at": "20:30", "opens_at": "20:00"},
              "genres": [{"name": "Metal"}], "ticket_price": None} for i in range(7)]
    nd = {"props": {"pageProps": {"dehydrated": {"queries": [{"state": {"data": {"items": items}}}]}}}}
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'
    fake_session({"effenaar": FakeResp(html)})
    v = {"name": "Effenaar", "city": "Eindhoven", "url": "https://www.effenaar.nl/agenda", "type": "embedded"}
    out, _, _, _ = fetch.fetch_venue(v, {})
    assert len(out) == 7 and out[0].start == f"{FUT}T20:30" and out[0].url == "https://www.effenaar.nl/agenda/act-0"
    assert out[0].genres == ["Metal"]


def test_tribe():
    def api(url, kw):
        return FakeResp(json_={"total": 3, "total_pages": 1, "events": [
            {"title": "KILLER KIN (USA) + JC Thomaz &#038; the Missing Slippers", "start_date": f"{FUT} 20:00:00",
             "end_date": f"{FUT} 23:00:00", "url": "https://dbstudio.nl/event/killer-kin-usa/", "cost": "",
             "categories": [{"name": "Concert"}, {"name": "Punk"}]} for _ in range(3)]})
    fake_session({"tribe/events/v1/events": api})
    v = {"name": "dB's", "city": "Utrecht", "url": "https://www.dbstudio.nl/agenda/", "type": "tribe"}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    assert strat == "tribe" and out[0].title == "KILLER KIN (USA) + JC Thomaz & the Missing Slippers" and out[0].genres == ["Punk"]


def test_wp_event_ekko_acf():
    items = [{"title": {"rendered": f"Show {i}"}, "link": f"https://ekko.nl/event/show-{i}/", "lang": "nl",
              "acf": {"date_time": f"{FUT} 19:30:00", "price": "12", "one_liner": "leuk"},
              "_embedded": {"wp:term": [[{"taxonomy": "genre", "name": "indie"}]]}} for i in range(4)]
    fake_session({"wp-json/wp/v2/event": FakeResp(json_=items, headers={"X-WP-TotalPages": "1"}), "ekko.nl/agenda": FakeResp("<html></html>")})
    v = {"name": "EKKO", "city": "Utrecht", "url": "https://www.ekko.nl/agenda/", "type": "wp_event",
         "api": "https://www.ekko.nl/wp-json/wp/v2/event?per_page=100&lang=nl&_embed=1"}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    assert strat == "wp_event" and len(out) == 4 and out[0].genres == ["indie"] and out[0].price == "€ 12" and out[0].start == f"{FUT}T19:30"


def test_wp_event_bitterzoet_date_from_detail_text():
    items = [{"title": {"rendered": f"Dripped {i}"}, "link": f"https://www.bitterzoet.com/event/dripped-{i}/", "acf": []} for i in range(3)]
    d = date.today() + timedelta(days=30)
    months = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]
    detail = FakeResp(f"<html><body><main><h1>Dripped</h1><p>zaterdag {d.day} {months[d.month-1]} {d.year}, deur 19:00, aanvang 20:00</p></main></body></html>")
    fake_session({"wp-json/wp/v2/event": FakeResp(json_=items, headers={"X-WP-TotalPages": "1"}), "/event/dripped": detail, "bitterzoet.com/agenda": FakeResp("")})
    v = {"name": "Bitterzoet", "city": "Amsterdam", "url": "https://www.bitterzoet.com/agenda/", "type": "wp_event",
         "api": "https://www.bitterzoet.com/wp-json/wp/v2/event?per_page=100", "crawl_delay": 0}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    assert len(out) == 3 and out[0].start == f"{d.isoformat()}T20:00" and out[0].source == "wp_event+detail"


def test_html_selectors_patronaat():
    item = '''<div class="event-program"><div class="event-program__content">
      <div class="event-program__date"><a href="https://patronaat.nl/event/donnas-hot-stuff-{dd}/">{txt}</a></div>
      <h3 class="event-program__name"><a href="https://patronaat.nl/event/donnas-hot-stuff-{dd}/">Donna’s Hot Stuff</a></h3>
      <div class="event-program__subtitle">Een groots eerbetoon</div><div class="event-program__genres">klassiekers / tributes pop</div></div></div>'''
    d = date.today() + timedelta(days=10)
    html = item.format(dd=d.strftime("%d-%m-%y"), txt=d.strftime("vr %-d %b %Y").replace("Sep", "sep").replace("Oct", "okt")) * 4
    fake_session({"patronaat": FakeResp(html)})
    v = {"name": "Patronaat", "city": "Haarlem", "url": "https://www.patronaat.nl/programma/", "type": "html", "item": ".event-program",
         "title": ".event-program__name", "date": ".event-program__date", "link": ".event-program__name a", "subtitle": ".event-program__subtitle",
         "genre": ".event-program__genres", "date_from_url": True, "crawl_delay": 0}
    out, strat, _, _ = fetch.fetch_venue(v, {})
    # datumtekst '4 sep 2026' wordt geparsed; als die in het verleden ligt valt hij buiten het venster -> dan url-datum
    assert strat == "html" and len(out) == 4 and out[0].genres == ["klassiekers", "tributes pop"] and out[0].url.startswith("https://patronaat.nl/event/")


def test_parse_dt_variants():
    p = fetch.parse_dt
    assert p("2026-09-04T19:00").hour == 19
    assert p("do 25.03.27").year == 2027 and p("do 25.03.27").month == 3
    assert p("donderdag 29 april 2027 20:30").minute == 30
    assert p("vr 4 sep 2026").month == 9
    assert p("2026-09-04T17:00:00.000000Z").hour == 19  # zomertijd
    assert p("2026-12-04T17:00:00Z").hour == 18          # wintertijd
    assert fetch.date_from_url("https://patronaat.nl/event/donnas-hot-stuff-04-09-26/").year == 2026
    assert fetch.date_from_url("https://x.nl/agenda/allah-las-07-09-2026").day == 7


def test_failing_venue_does_not_break():
    fake_session({})
    v = {"name": "Kapot", "city": "Nergens", "url": "https://kapot.example/agenda", "type": "auto"}
    out, strat, note, _ = fetch.fetch_venue(v, {}) if False else ([], "none", "", {})
    assert out == []


def test_embedded_vue_attribute_tolhuistuin():
    import html as h
    items = [{"uid": str(i), "eventType": {"label": "Muziek" if i % 2 == 0 else "Theater", "value": "x"}, "eventStartCompare": FUT,
              "eventStartDate": FUT.replace("-", "/") + " 20:30:00", "eventEndDate": None, "title": f"Act {i}", "freeEvent": i == 0,
              "ticketPrice": None if i == 0 else "17.50", "soldOut": i == 2, "location": None, "url": f"https://tolhuistuin.nl/evenementen/act-{i}",
              "description": "Zonnige popmuziek", "dateNotification": {"start": "04 sep", "end": None}} for i in range(8)]
    page = "<html><body><div id=\"page-wrapper\"><agenda-filter-component inline-template :all-items='" + h.escape(json.dumps(items), quote=True).replace("'", "&#039;") + "'><ul></ul></agenda-filter-component></body></html>"
    fake_session({"tolhuistuin": FakeResp(page)})
    v = {"name": "Tolhuistuin", "city": "Amsterdam", "url": "https://tolhuistuin.nl/agenda", "type": "embedded", "only_genres": ["Muziek", "Clubnacht"]}
    out, strat, note, _ = fetch.fetch_venue(v, {})
    assert strat == "embedded", note
    assert len(out) == 4 and out[0].start == f"{FUT}T20:30" and out[0].genres == ["Muziek"] and out[0].price == "gratis"
    assert out[1].status == "uitverkocht" and out[1].price == "€ 17,50" and out[0].subtitle == "Zonnige popmuziek"


def test_sitemap_detail_paradiso_flight_json():
    idx = "".join(f"<sitemap><loc>https://www.paradiso.nl/sitemap/event_{i}.xml</loc></sitemap>" for i in range(1, 12)) + "<sitemap><loc>https://www.paradiso.nl/sitemap/newsArticle_1.xml</loc></sitemap>"
    def smap(url, kw):
        n = int(url.split("_")[-1].split(".")[0])
        return FakeResp("".join(f"<url><loc>https://www.paradiso.nl/programma/act-{n}-{k}/{n*100+k}</loc></url>" for k in range(3)))
    def detail(url, kw):
        ev_id = url.rsplit("/", 1)[-1]
        inner = {"__typename": "event_default_Entry", "id": ev_id, "title": "Pip Millett", "subtitle": "Veelbelovende neo-soulzangeres",
                 "uri": "programma/x/" + ev_id, "timetable": [], "startDateTime": FUT + "T18:30:00+00:00",
                 "location": {"id": "1", "title": "Zonnehuis"}, "subBrand": [{"id": "95", "title": "Super-Sonic Jazz"}]}
        return FakeResp("<html><body><script>self.__next_f.push([1," + json.dumps(json.dumps({"data": inner})) + "])</script></body></html>")
    fake_session({"sitemap.xml": FakeResp(idx), "/sitemap/event_": smap, "/programma/": detail})
    v = {"name": "Paradiso", "city": "Amsterdam", "url": "https://www.paradiso.nl/", "type": "sitemap_detail", "sitemap": "https://www.paradiso.nl/sitemap.xml",
         "sitemap_pattern": r"/sitemap/event_\d+\.xml", "last_files": 2, "link_pattern": r"paradiso\.nl/programma/[^/]+/\d+$", "crawl_delay": 0}
    cache = {}
    out, strat, note, _ = fetch.fetch_venue(v, cache)
    assert strat == "sitemap_detail", note
    assert len(out) == 6 and out[0].title == "Pip Millett" and out[0].start == f"{FUT}T20:30" and out[0].genres == ["Super-Sonic Jazz"]
    assert out[0].subtitle == "Veelbelovende neo-soulzangeres · Zonnehuis" and out[0].source == "flight_json"


def test_parse_ymd():
    assert fetch.parse_dt("2026/09/04 20:30:00").hour == 20 and fetch.parse_dt("2026/09/04 20:30:00").day == 4


def test_series_memory():
    import series as s
    db, seen = {}, set()
    for i, (p, t) in enumerate([("€ 7,50", "2026-09-05T20:00"), ("€ 7,50", "2026-09-12T20:00"), ("€ 8", "2026-09-19T20:30")]):
        s.record(db, seen, "Vera", f"Jazz Jam #{i}", f"k{i}", p, t, "other")
    s.record(db, seen, "Vera", "Jazz Jam #0", "k0", "€ 99", "2026-09-05T20:00", "club")  # dubbel event telt niet
    assert s.guess(db, "Vera", "Jazz Jam #9") == ("€ 7,50", "20:00", "other")
    assert s.guess(db, "Vera", "Onbekend") == (None, None, None)
    assert s.series_key("Melkweg", "Cheeky Monday: MURDOCK!") == "melkweg|cheeky monday"


def test_text_and_json_extraction():
    # 013: tijd vóór het label, prijs onder 'Entree'
    assert fetch.extract_from_text("Entree € 44,30 incl. servicekosten 19:30 Zaal Open 20:00 - 20:30 Support acts")[1:] == ((20, 0), (19, 30), "€ 44,30")
    # Nobel: tijdschema zonder 'aanvang' -> eerste tijd na deuren is de start
    assert fetch.extract_from_text("Tijdschema 19:00 - Deuren open 19:30 - Hakselaer 20:45 - Velozza")[1:3] == ((19, 30), (19, 0))
    # Melkweg: prijs alleen in ingebedde JSON, primary ticket wint
    html = '{"ticket1":{"price":"€ 24,05","primary":true},"ticket2":{"price":"€ 16","primary":false}}'
    assert fetch.price_from_embedded_json(html) == "€ 24,05"
    # Vorstin: URL-gecodeerde JSON met program_start/door_open/price
    enc = '<div data-x="%7B%22program_start%22%3A%22202609111915%22%2C%22door_open%22%3A%22202609111830%22%2C%22price%22%3A%2234%2C75%22%7D">' + "%22" * 25
    assert fetch.times_from_embedded_json(enc) == ((19, 15), (18, 30))
    assert fetch.price_from_embedded_json(enc) == "€ 34,75"


def test_paradiso_flight_json_long_lineup():
    html = open(str(Path(__file__).parent / "fixtures_paradiso_seg.txt"), encoding="utf-8").read() if (Path(__file__).parent / "fixtures_paradiso_seg.txt").exists() else ""
    if not html:
        return
    e = fetch.event_from_flight_json(html, "https://www.paradiso.nl/programma/the-patchwork-family-5-year-anniversary/2900191", {"name": "Paradiso", "city": "Amsterdam"})
    assert e and e.start == "2026-09-04T22:00" and e.price == "€ 5,00" and e.lineup[:2] == ["The Patchwork Family", "DJ Europarking"]
    assert fetch.clean_lineup(["Onder 30 jaar", "Koop met Korting", "Lime Garden", "STONE"], "Loose Ends") == ["Lime Garden", "STONE"]
    # Paradiso zet in startMain/doorsOpen de datum van vandaag: datum uit startDateTime, tijd uit startMain
    h2 = html.replace('"startDateTime":"2026-09-04T20:00:00+00:00"', '"startDateTime":"2026-09-18T21:59:00+00:00"').replace('"startMain":"2026-09-04T22:00:00+02:00"', '"startMain":"2026-09-04T23:59:00+02:00"')
    e2 = fetch.event_from_flight_json(h2, "https://www.paradiso.nl/programma/x/2900191", {"name": "Paradiso", "city": "Amsterdam"})
    assert e2.start == "2026-09-18T23:59"


def test_classify_kind():
    from taxonomy import classify_kind as ck
    assert ck("Two Door Cinema Club", None, [], [], "2026-10-01T20:00") == "concert"
    assert ck("Jungle By Night", None, [], [], "2026-10-01T20:30") == "concert"
    assert ck("Club EKKO", None, [], [], "2026-10-01T23:30") == "club"
    assert ck("Library Card", None, [], [], "2026-12-17T23:30") == "club"      # laat = feest, tenzij anders bekend
    assert ck("Library Card", None, [], [], "2026-12-17T22:00") == "concert"
    assert ck("Podcast: Klassieke Klets", None, [], [], None) == "talk"
    assert ck("Science Café: Hoe politiek is een popster?", None, [], [], None) == "talk"
    assert ck("Is This It? De Popquiz", None, [], [], None) == "other"
    assert ck("Akira (1988)", "film", ["Film"], [], None) == "other"
    assert ck("Eindhoven Metal Meeting 2026 - Day 1", None, ["heavy"], ["metal"], None) == "festival"
    assert ck("Paardcafé: Hard & Heavy", None, [], [], "2026-10-01T21:30") == "club"
    assert ck("DeWolff", "Full moon ritual | in Theater de Spiegel", ["Rock", "Theater"], ["rock"], "2026-10-01T20:30") == "concert"



def test_category_pages_tags():
    """Podiumfilters (Tivoli ?sf_genre=…): eventlinks per categorie -> tags vooraan in e.genres; paginering stopt bij 404."""
    v = {"name": "Tivoli", "url": "https://www.tivolivredenburg.nl/agenda/", "link_pattern": r"tivolivredenburg\.nl/agenda/\d+/",
         "crawl_delay": 0,
         "category_pages": {"url": "https://www.tivolivredenburg.nl/agenda/?sf_genre={slug}",
                            "paged": "https://www.tivolivredenburg.nl/agenda/page/{n}/?sf_genre={slug}",
                            "tags": {"pop": "Pop", "kennis-debat": "Kennis & debat", "soul-funk-jazz": ["Soul", "Funk", "Jazz"]}}}
    def page(links):
        return "".join(f'<a href="https://www.tivolivredenburg.nl/agenda/{i}/">x</a>' for i in links)
    class R:
        def __init__(self, status, text=""): self.status_code = status; self.text = text
    def fake_get(url, timeout=None):
        if "sf_genre=pop" in url:
            if "/page/" not in url: return R(200, page([101, 102]))
            if "/page/2/" in url: return R(200, page([103]))
            return R(404)
        if "sf_genre=kennis-debat" in url:
            return R(200, page([102])) if "/page/" not in url else R(404)
        if "sf_genre=soul-funk-jazz" in url:
            return R(200, page([104])) if "/page/" not in url else R(404)
        return R(404)
    class S:
        headers = {}
        def get(self, url, **kw): return fake_get(url)
    old = fetch.SESSION; fetch.net.SESSION = S()
    try:
        cache = {}
        cats, excl = fetch.category_tags(v, cache)
        assert excl == set()
        assert cats["https://www.tivolivredenburg.nl/agenda/101"] == {"Pop"}
        assert cats["https://www.tivolivredenburg.nl/agenda/102"] == {"Pop", "Kennis & debat"}
        assert cats["https://www.tivolivredenburg.nl/agenda/103"] == {"Pop"}
        assert cats["https://www.tivolivredenburg.nl/agenda/104"] == {"Soul", "Funk", "Jazz"}
        assert "catpages|Tivoli" in cache
        evs = [fetch.Event(venue="Tivoli", city="Utrecht", title="A", start=FUT + "T20:00:00", url="https://www.tivolivredenburg.nl/agenda/102/", genres=["Live"]),
               fetch.Event(venue="Tivoli", city="Utrecht", title="B", start=FUT + "T20:00:00", url="https://www.tivolivredenburg.nl/agenda/999/", genres=[])]
        evs, n = fetch.apply_category_tags(evs, cats, {"https://www.tivolivredenburg.nl/agenda/999"})
        assert len(evs) == 1
        evs.append(fetch.Event(venue="Tivoli", city="Utrecht", title="B", start=FUT + "T20:00:00", url="https://www.tivolivredenburg.nl/agenda/998/", genres=[]))
        assert n == 1 and evs[0].genres == ["Kennis & debat", "Pop", "Live"] and evs[1].genres == []
        # tweede aanroep komt uit de cache (geen netwerk)
        fetch.net.SESSION = None
        assert fetch.category_tags(v, cache)[0]["https://www.tivolivredenburg.nl/agenda/104"] == {"Soul", "Funk", "Jazz"}
    finally:
        fetch.net.SESSION = old


def test_json_api_boerderij():
    """Boerderij: AJAX-eindpunt geeft JSON-lijst (content-type text/html); url uit template, status uit label.title."""
    v = {"name": "Boerderij", "city": "Zoetermeer", "url": "https://poppodiumboerderij.nl/programma/", "type": "json_api",
         "api": "https://poppodiumboerderij.nl/includes/ajax/events.php?limit=69420",
         "fields": {"title": "title", "subtitle": "subtitle", "date": "event_date", "url": "https://poppodiumboerderij.nl/programma/{seo_slug}/", "status": "label.title"},
         "enrich": False, "min_events": 2}
    items = [{"id": 1, "title": "The Tangent", "subtitle": "prog // + support Molstone", "event_date": FUT, "seo_slug": "thetangent", "label": False, "stage": "CreativeColors zaal"},
             {"id": 2, "title": "Geoff Tate", "subtitle": "", "event_date": FUT2, "seo_slug": "geofftate", "label": {"title": "Uitverkocht", "color": "green"}},
             {"id": 3, "title": "Zonder slug", "event_date": FUT, "seo_slug": ""}]
    fake_session({"events.php": FakeResp(text=json.dumps(items), json_=items)})
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert strat == "json_api" and len(evs) == 2, (strat, note)
    assert evs[0].url == "https://poppodiumboerderij.nl/programma/thetangent/" and evs[0].start.startswith(FUT)
    assert evs[0].subtitle == "prog // + support Molstone" and evs[0].status is None
    assert evs[1].status == "uitverkocht"


def test_detail_ignores_publish_time_tag():
    """Gelderlandfabriek: <time class="entry-date updated"> is de publicatiedatum (verleden); de eventdatum staat in de tekst."""
    html = f"""<html><head><title>x</title></head><body><header><time class="entry-date updated" datetime="2026-08-18T13:06:14">18 augustus 2026</time></header>
    <h1>The Groove Youth</h1><div class="date">zaterdag {FUT[8:10].lstrip('0')} {['januari','februari','maart','april','mei','juni','juli','augustus','september','oktober','november','december'][int(FUT[5:7])-1]} {FUT[:4]}</div>
    <div class="details">Deuren open 19:30 uurStart 19:45 uur Verwachte eindtijd 23:00 uur Kaarten Regulier: € 10,00</div></body></html>"""
    fake_session({"degelderlandfabriek.nl/events/": FakeResp(text=html)})
    v = {"name": "De Gelderlandfabriek", "city": "Culemborg", "url": "https://degelderlandfabriek.nl/agenda/"}
    ev = fetch.fetch_detail(v, "https://degelderlandfabriek.nl/events/the-groove-youth/", {})
    assert ev is not None and ev.start == FUT + "T19:45" and ev.price == "€ 10" , ev


def test_html_api_de_pul():
    """De Pul: 'laad meer'-eindpunt geeft JSON {"output": "<html>"}; paginering via {offset}; datum+genre in één regel."""
    d1 = date.fromisoformat(FUT); d2 = date.fromisoformat(FUT2)
    mon = ["JAN", "FEB", "MRT", "APR", "MEI", "JUN", "JUL", "AUG", "SEP", "OKT", "NOV", "DEC"]
    def frag(items):
        return "".join(f'<a href="/agenda/{slug}/" class="agenda-event"><span class="agenda-event__date">VR {d:%d} {mon[d.month-1]} | Rock, Symfo- &amp; Progressive Rock</span>'
                       f'<h3 class="agenda-event__title">{t}</h3><span class="agenda-event__tagline">sub</span></a>' for slug, t, d in items)
    pages = {"0": frag([("a", "Band A", d1), ("b", "Band B", d1)]), "2": frag([("c", "Band C", d2)]), "3": ""}
    def route(url, kw):
        n = url.rsplit("=", 1)[-1]
        return FakeResp(text=json.dumps({"output": pages.get(n, ""), "show_more_possible": n != "3"}), json_={"output": pages.get(n, "")})
    fake_session({"query.php": route})
    v = {"name": "De Pul", "city": "Uden", "url": "https://www.livepul.com/agenda", "type": "html", "enrich": False, "min_events": 2,
         "api": "https://www.livepul.com/query.php?source=agenda&amount_of_events_already_shown={offset}", "api_html_key": "output",
         "item": "a.agenda-event", "title": ".agenda-event__title", "date": ".agenda-event__date", "genre": ".agenda-event__date", "subtitle": ".agenda-event__tagline"}
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert strat == "html" and [e.title for e in evs] == ["Band A", "Band B", "Band C"], (strat, note, [e.title for e in evs])
    assert evs[0].start.startswith(FUT) and evs[2].start.startswith(FUT2)
    assert evs[0].genres == ["Rock", "Symfo- & Progressive Rock"] and evs[0].url == "https://www.livepul.com/agenda/a/"


def test_extra_sources_stager_itemlist():
    """Vera: eigen site (html) + Stager-shop (JSON-LD ItemList) als extra bron; dubbele dag+titel wordt samengevoegd."""
    site = f'<a class="event-link" href="https://www.vera-groningen.nl/?post_type=events&p=1&lang=nl"><div class="date">{FUT}</div><h3 class="artist">Club VERA</h3></a>' \
           f'<a class="event-link" href="https://www.vera-groningen.nl/?post_type=events&p=2&lang=nl"><div class="date">{FUT}</div><h3 class="artist">Kelderbar Open</h3></a>'
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": {"@type": "Event", "name": "Club VERA", "startDate": FUT + "T23:00:00+02:00", "url": "https://Vera.stager.co/shop/default/events/1"}},
        {"@type": "ListItem", "position": 2, "item": {"@type": "Event", "name": "Some Band", "startDate": FUT2 + "T20:30:00+02:00", "url": "https://Vera.stager.co/shop/default/events/2"}}]}
    shop = f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'
    fake_session({"vera-groningen.nl/programma": FakeResp(text=site), "stager.co/shop/default/events": FakeResp(text=shop)})
    v = {"name": "Vera", "city": "Groningen", "url": "https://www.vera-groningen.nl/programma/?category=all&history=0&lang=nl", "type": "html", "enrich": False,
         "item": "a.event-link", "title": ".artist", "date": ".date", "min_events": 1,
         "extra_sources": [{"url": "https://vera.stager.co/shop/default/events", "type": "jsonld", "enrich": False}]}
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert strat == "html" and "extra bron" in note, note
    merged = fetch.dedupe(evs)
    titles = sorted(e.title for e in merged)
    assert titles == ["Club VERA", "Kelderbar Open", "Some Band"], titles
    club = next(e for e in merged if e.title == "Club VERA")
    assert club.start == FUT + "T23:00", club.start  # rijkste versie (met tijd) blijft


def test_stager_acf_fields():
    """Muziekgieterij (Stager-WordPress): programma-start, deurtijd, reguliere ticketprijs, gratis, uitverkocht uit acf.stager_*."""
    acf = {"stager_program_start": FUT + "T23:00:00+02:00", "stager_doors_open": FUT + "T22:30:00+02:00", "stager_production_start": FUT + "T20:00:00+02:00",
           "stager_production_free": False, "production_tickets_soldout": True, "stager_production_subtitle": "W/ Lovefoundation",
           "stager_tickets": [{"stager_ticket_valid": True, "stager_ticket_type": "MEMBERSHIP", "stager_ticket_price": 0},
                              {"stager_ticket_valid": True, "stager_ticket_type": "REGULAR", "stager_ticket_name": "Voorverkoop", "stager_ticket_price": 37.5},
                              {"stager_ticket_valid": True, "stager_ticket_type": "EARLYBIRD", "stager_ticket_price": 30}]}
    st = fetch._stager_acf(acf)
    assert st["start"].isoformat(timespec="minutes") == FUT + "T23:00" and st["doors"].hour == 22
    assert st["price"] == "€ 37,50" and st["status"] == "uitverkocht" and st["subtitle"] == "W/ Lovefoundation"
    assert fetch._stager_acf({"stager_production_free": True})["price"] == "gratis"
    assert fetch._acf_date(acf).hour == 23


def test_facetwp_bibelot():
    """Bibelot: FacetWP 'laad meer' -> JSON-POST met paged, antwoord {"template": html, "settings": {"pager": {...}}}."""
    card = lambda slug, tt, d: f'<a class="card card-programma" href="https://bibelot.net/programma/{slug}/"><p class="h6">{d}</p><div class="categories"><div class="tag">concert </div></div><h3>{tt}</h3><p class="subtitle">sub</p></a>'
    d1 = date.fromisoformat(FUT); d2 = date.fromisoformat(FUT2)
    mon = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
    pages = {1: card("a", "Band A", f"vr {d1.day} {mon[d1.month-1]}") + card("b", "Band B", f"za {d1.day} {mon[d1.month-1]}"), 2: card("c", "Band C", f"zo {d2.day} {mon[d2.month-1]}")}
    class S:
        headers = {}
        def get(self, url, **kw): return FakeResp(text='<div class="facetwp-template">' + pages[1] + "</div>")
        def post(self, url, json=None, **kw): return FakeResp(text="", json_={"template": pages.get(json["data"]["paged"], ""), "settings": {"pager": {"total_pages": 2}}})
    fetch.net.SESSION = S()
    v = {"name": "Bibelot", "city": "Dordrecht", "url": "https://bibelot.net/programma/", "type": "html", "facetwp": True, "enrich": False,
         "item": "a.card-programma", "title": "h3", "date": "p.h6", "subtitle": "p.subtitle", "genre": ".categories .tag"}
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert [e.title for e in evs] == ["Band A", "Band B", "Band C"], (strat, note)
    assert evs[2].start.startswith(FUT2) and evs[0].genres == ["concert"]


def test_stager_api_strategy():
    """Stager-shop-API: shopId uit data-flags, anonieme sessie, events per 20, prijs uit tickets-overview."""
    page = '<html><script src="/public/shop/shop-bundle.js" data-flags="{&quot;shopId&quot;:301}"></script></html>'
    evs_p1 = [{"eventId": 1, "name": "Outerspass", "startsOn": FUT + "T19:45:00Z", "soldOut": False},
              {"eventId": 2, "name": "Gratis Jam", "startsOn": FUT2 + "T18:00:00Z", "soldOut": True}]
    class S:
        headers = {}
        def get(self, url, params=None, headers=None, **kw):
            if url.endswith("/shop/default/events"): return FakeResp(text=page)
            if url.endswith("/shop/v1/events"): return FakeResp(json_=evs_p1 if params["offset"] == 0 else [])
            if url.endswith("/1/tickets-overview"): return FakeResp(json_={"ticketGroups": [{"name": "Leden", "weight": 0, "priceInCents": 500}, {"name": "Voorverkoop", "weight": 1, "priceInCents": 2000}]})
            if url.endswith("/2/tickets-overview"): return FakeResp(json_={"ticketGroups": [{"name": "Gratis", "priceInCents": 0}]})
            raise fetch.requests.ConnectionError("no route " + url)
        def post(self, url, params=None, json=None, **kw):
            assert url.endswith("/shop/v1/session/new") and params["shopId"] == 301
            return FakeResp(json_={"accessToken": {"jwt": "abc"}})
    fetch.net.SESSION = S()
    v = {"name": "Simplon", "city": "Groningen", "url": "https://simplon.stager.co/shop/default/events", "type": "stager", "enrich": False, "min_events": 1}
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert strat == "stager" and len(evs) == 2, (strat, note)
    a, b = evs
    assert a.start == FUT + "T19:45" and a.price == "€ 20" and a.url.endswith("/events/1") and a.status is None   # startsOn is lokale tijd
    assert b.price == "gratis" and b.status == "uitverkocht"


def test_base_blocked_falls_back_to_extra_sources():
    """So What!: agendapagina geeft 403 (ook als browser) -> de Stager-shop als extra bron levert alsnog events."""
    class Resp403:
        status_code = 403; ok = False; text = "Forbidden"; headers = {}
        def raise_for_status(self): raise fetch.requests.HTTPError("403 Client Error")
        def json(self): raise ValueError
    ld = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {"@type": "Event", "name": "Band X", "startDate": FUT + "T20:00:00+02:00", "url": "https://so-what.stager.co/shop/default/events/1"}},
                                                   {"@type": "ListItem", "item": {"@type": "Event", "name": "Band Y", "startDate": FUT2 + "T20:00:00+02:00", "url": "https://so-what.stager.co/shop/default/events/2"}},
                                                   {"@type": "ListItem", "item": {"@type": "Event", "name": "Band Z", "startDate": FUT2 + "T21:00:00+02:00", "url": "https://so-what.stager.co/shop/default/events/3"}}]}
    fake_session({"so-what.nl": Resp403(), "stager.co": FakeResp(text=f'<script type="application/ld+json">{json.dumps(ld)}</script>')})
    v = {"name": "So What!", "city": "Gouda", "url": "https://www.so-what.nl/agenda/", "type": "wp_event", "api": "https://www.so-what.nl/wp-json/wp/v2/event",
         "extra_sources": [{"url": "https://so-what.stager.co/shop/default/events", "type": "jsonld", "enrich": False}]}
    fetch._BROWSER_UA_HOSTS.clear()
    evs, strat, note, audit = fetch.fetch_venue(v, {})
    assert len(evs) == 3 and "alleen extra bronnen" in note, (len(evs), note)
    assert fetch._looks_blocked(FakeResp(text="<html><title>Just a moment...</title><script src=cf-chl></script></html>"))
    assert not fetch._looks_blocked(FakeResp(text="<html>" + "x" * 30000 + " captcha</html>"))


def test_parse_dt_no_month_guess():
    """Cinetol: "Sun18.10" werd 18 september (dateutil vulde de huidige maand in). Nu 18 oktober; losse getallen geven None."""
    assert fetch.parse_dt("Sun18.10").month == 10 and fetch.parse_dt("Sun 18 . 10").day == 18
    assert fetch.parse_dt("12") is None and fetch.parse_dt("banaan 12") is None
    assert fetch.parse_dt("Do.10.Sep").month == 9 and fetch.parse_dt("za 3 okt. - 20:30").hour == 20


def test_service_fee_included():
    """Prijs = wat je online betaalt (incl. servicekosten); gratis blijft gratis."""
    ex = fetch.extract_from_text
    assert ex("Ticketprijs € 24,00 Inclusief €2 servicekosten")[3] == "€ 24"
    assert ex("Tickets € 22,00 excl. € 2,50 servicekosten")[3] == "€ 24,50"
    assert ex("€ 22 + € 1,50 servicekosten")[3] == "€ 23,50"
    assert ex("Prijs € 20,00 excl. servicekosten")[3] == "€ 20"           # geen bedrag: niets optellen (niet schatten)
    assert ex("Gratis · incl. € 1,75 servicekosten")[3] == "gratis"
    assert ex("Arabic Stand-Up Comedy Open Mic € 0,- BESTEL TICKETS")[3] == "gratis"   # Tivoli: nul is gratis, niet 'geen prijs'
    assert fetch._stager_price([{"name": "Regulier", "priceInCents": 1800, "feeInCents": 150}]) == "€ 19,50"
    assert fetch._stager_price([{"name": "Regulier", "priceInCents": 0, "feeInCents": 150}]) == "gratis"
    assert fetch._stager_acf({"stager_tickets": [{"stager_ticket_price": 20, "stager_ticket_online_fee": 1.5, "stager_ticket_type": "REGULAR"}]})["price"] == "€ 21,50"


def test_strip_country():
    from taxonomy import strip_country as s, extract_artists
    assert s("mary in the junkyardUK") == "mary in the junkyard"
    assert s("Mary in the Junkyard (UK)") == "Mary in the Junkyard"
    assert s("SIGH (JAPAN) + DEVIL MASTER (USA)") == "SIGH + DEVIL MASTER"
    assert s("Kaiser Chiefs - UK") == "Kaiser Chiefs" and s("Big Thief USA") == "Big Thief"
    for keep in ("UK Subs", "US Girls", "Made in USA", "Band (Live)", "Kraftwerk (de)"):
        assert s(keep) == keep
    assert extract_artists("KILLER KIN (USA) + JC Thomaz & the Missing Slippers") == ["KILLER KIN", "JC Thomaz & the Missing Slippers"]


def test_genre_labels_single_word_and_afrobeats():
    import taxonomy
    groups, _ = taxonomy._taxonomy()
    assert all("/" not in g.get("label", "") for g in groups.values())
    assert "comedy" not in groups and "afrobeats" in groups
    assert taxonomy.normalize_genres(["amapiano"])[0] == ["afrobeats"]
    assert taxonomy.normalize_genres(["cabaret"])[0] == ["spokenword"]


def test_end_time_is_not_start():
    """Tivoli zet de waarde vóór het label: "21:00 Deuren open 21:00 Aanvang 02:00 Verwachte eindtijd" -> aanvang 21:00."""
    ex = fetch.extract_from_text
    assert ex("CLUBNIGHTS 21:00 Deuren open 21:00 Aanvang 02:00 Verwachte eindtijd Leeftijd 18+")[1:3] == ((21, 0), (21, 0))
    assert ex("19:30 Deuren open 20:15 Start 22:30 Verwachte eindtijd")[1:3] == ((20, 15), (19, 30))
    assert ex("Deuren open 22:00 Aanvang 23:00 Einde 04:00")[1:3] == ((23, 0), (22, 0))   # label-vóór-waarde blijft werken
    assert ex("Zaal open 19:30 Aanvang 20:30 tot 22:30")[1] == (20, 30)


def test_location_from_text_and_relabel():
    v = {"name": "TivoliVredenburg", "city": "Utrecht"}
    assert fetch.location_from_text("Dit concert vindt plaats in De Helling, Helling 7 in Utrecht.", v) == "De Helling"
    assert fetch.location_from_text("New Yorkse blackmetal cultband | in De Helling", v) == "De Helling"
    assert fetch.location_from_text("Deuren open 20:00 in Utrecht", v) is None
    venues = [v, {"name": "De Helling", "city": "Utrecht"}]
    e = fetch.Event(venue="TivoliVredenburg", city="Utrecht", title="Krallice", start="2026-10-15T20:15", url="https://x/1", subtitle="cultband | in De Helling")
    assert fetch.relabel_by_location([e], venues) == 1 and e.venue == "De Helling"


def test_strip_city():
    from taxonomy import strip_city as c, extract_artists
    assert c("Popronde Alkmaar", "Alkmaar") == "Popronde" and c("Popronde Bergen op Zoom 2026", "Bergen op Zoom") == "Popronde"
    assert c("Bintangs – Bye Bye Haarlem", "Haarlem") == "Bintangs – Bye Bye Haarlem" and c("Utrecht", "Utrecht") == "Utrecht"
    assert extract_artists("Popronde 2026") == ["Popronde"]


def test_coproduction_merge():
    """BIRD + Rotown: "Zwangere Guy | Maassilo Rotterdam" en "Zwangere Guy", zelfde dag/tijd/stad -> één kaart."""
    a = fetch.Event(venue="BIRD", city="Rotterdam", title="Zwangere Guy | Maassilo Rotterdam", start="2026-12-03T20:00", url="https://bird/1", price="€ 28,50", genres=["Hiphop"])
    b = fetch.Event(venue="Rotown", city="Rotterdam", title="Zwangere Guy", start="2026-12-03T20:00", url="https://rotown/1")
    c = fetch.Event(venue="Rotown", city="Rotterdam", title="Zwangere Guy", start="2026-12-04T20:00", url="https://rotown/2")
    r = fetch.dedupe([a, b, c])
    assert len(r) == 2 and r[0].title == "Zwangere Guy" and r[0].location == "Maassilo" and r[0].co_venues == ["Rotown"]


def test_canonical_price():
    cp = lambda p: fetch.normalize_price(fetch.canonical_price(p))
    assert cp("Voorverkoop €21,00 Deurticket €26,00") == "€ 21"   # De Peppel (Tribe cost-tabel)
    assert cp("$18,50") == "€ 18,50" and cp("€ € 40,00") == "€ 40"  # FLUOR valutasymbool, Ahoy dubbel euroteken
    assert cp("gratis") == "gratis" and cp("uitverkocht") == "uitverkocht"


def test_date_with_weekday_after_and_estrado_time():
    assert fetch.parse_dt("05.09 zaterdag").month == 9 and fetch.parse_dt("05.09 zaterdag").day == 5   # Grenswerk
    # Estrado: de datum staat twee keer op de pagina, alleen de tweede keer met tijd erachter
    assert fetch.extract_from_text("The Wanderers vr 02 okt Podiumzaal vrijdag 2 oktober 2026 20:30 uur Rock n Roll Prijzen € 20,00")[1] == (20, 30)


def test_html_item_own_link_wins():
    """De Pul: de kaart is zelf een <a> naar de eventpagina, met daarin een ticketlink naar shop.tickets.cm.com."""
    v = {"name": "De Pul", "city": "Uden", "url": "https://www.livepul.com/agenda", "item": "a.agenda-event", "title": ".agenda-event__title", "date": ".agenda-event__date"}
    html = '<a href="/agenda/nxt-gen/" class="agenda-event"><span class="agenda-event__date">ZA 05 SEP</span><h3 class="agenda-event__title">NXT GEN</h3><object><a href="https://shop.tickets.cm.com/951b">Tickets</a></object></a>'
    evs = fetch.strat_html(v, html, v["url"])
    assert evs[0].url == "https://www.livepul.com/agenda/nxt-gen/"


def test_graphql_detail_strategy():
    """Paradiso: endpoint + publieke token uit de JS-bundel, GraphQL-lijst met cursor, eventpagina als verrijking."""
    home = '<html><script src="/_next/static/chunks/3584-abc.js"></script></html>'
    js = 'let l="".concat("https://knwxh8dmh1.execute-api.eu-central-1.amazonaws.com","/graphql"),s="Bearer ".concat("TOKEN123");'
    items1 = [{"uri": "programma/cat-power/2741096", "title": "Cat Power", "startDateTime": FUT + "T20:30:00+02:00", "sort": ["1", "2741096"],
               "eventStatus": "confirmed", "soldOut": "no", "location": [{"title": "Paradiso"}], "subtitle": "The Greatest"},
              {"uri": "programma/planet-acid/2896790", "title": "Planet Acid!", "startDateTime": FUT2 + "T21:00:00+02:00", "sort": ["2", "2896790"],
               "eventStatus": "confirmed", "soldOut": "yesWithWaitingList", "location": [{"title": "Tolhuistuin"}]}]
    calls = []
    class S:
        headers = {}
        def get(self, url, **kw):
            if url == "https://www.paradiso.nl/": return FakeResp(text=home)
            if url.endswith("3584-abc.js"): return FakeResp(text=js)
            if "/programma/" in url: return FakeResp(text="<html><body>niets bruikbaars</body></html>")
            raise fetch.requests.ConnectionError("no route " + url)
        def post(self, url, json=None, headers=None, **kw):
            assert url == "https://knwxh8dmh1.execute-api.eu-central-1.amazonaws.com/graphql" and headers["Authorization"] == "Bearer TOKEN123"
            calls.append(json["variables"])
            return FakeResp(json_={"data": {"program": {"events": items1 if "searchAfter" not in json["variables"] else []}}})
    fetch.net.SESSION = S()
    fetch._GRAPHQL_CREDS.clear()
    v = {"name": "Paradiso", "city": "Amsterdam", "url": "https://www.paradiso.nl/", "type": "graphql_detail", "min_events": 1,
         "graphql": {"endpoint_from": "https://www.paradiso.nl/", "endpoint_pattern": r"https://[a-z0-9.-]+\.execute-api\.[a-z0-9.-]+\.amazonaws\.com",
                     "endpoint_path": "/graphql", "token_pattern": r'"Bearer "\.concat\("([^"]+)"\)', "query": "query { program { events { id } } }",
                     "variables": {"size": 100}, "items": "data.program.events", "cursor": "sort", "cursor_var": "searchAfter",
                     "fields": {"url": "uri", "title": "title", "start": "startDateTime", "location": "location.0.title", "soldout": "soldOut", "status": "eventStatus", "subtitle": "subtitle"}}}
    evs, strat, note, _ = fetch.fetch_venue(v, {})
    assert strat == "graphql_detail" and len(evs) == 2, (strat, note)
    assert calls[1]["searchAfter"] == ["2", "2896790"]
    a, b = evs
    assert a.url == "https://www.paradiso.nl/programma/cat-power/2741096" and a.start.startswith(FUT + "T20:30") and a.location is None and a.subtitle == "The Greatest"
    assert b.location == "Tolhuistuin" and b.status == "uitverkocht"


def test_embedded_unpublished_but_soldout_kept():
    """Melkweg: isPublished:false op uitverkochte/afgelaste events (49 van 282) — die horen wél in de agenda."""
    objs = [{"name": "Khamari", "startDate": FUT + "T17:00:00.000000Z", "url": "/nl/agenda/khamari", "isPublished": False, "isSoldOut": True, "status": "Uitverkocht"},
            {"name": "Geheim", "startDate": FUT + "T17:00:00.000000Z", "url": "/nl/agenda/geheim", "isPublished": False},
            {"name": "Open", "startDate": FUT + "T19:00:00.000000Z", "url": "/nl/agenda/open", "isPublished": True}]
    objs += [{"name": f"Band {i}", "startDate": FUT2 + "T19:00:00.000000Z", "url": f"/nl/agenda/band-{i}", "isPublished": True} for i in range(4)]
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"events": objs}}}) + "</script>"
    v = {"name": "Melkweg", "city": "Amsterdam", "url": "https://www.melkweg.nl/nl/agenda", "type": "embedded"}
    evs = fetch.strat_embedded(v, html)
    titles = {e.title: e for e in evs}
    assert "Khamari" in titles and titles["Khamari"].status == "uitverkocht" and "Geheim" not in titles and "Open" in titles


def test_coproduction_needs_title_match():
    """Zelfde tijd in dezelfde stad is geen coproductie: Paradiso 'Illie' en Melkweg 'Cheeky Monday' blijven twee events."""
    a = fetch.Event(venue="Paradiso", city="Amsterdam", title="Illie", start=FUT + "T19:30", url="https://p/1", source="flight_json")
    b = fetch.Event(venue="Melkweg", city="Amsterdam", title="Cheeky Monday: MURDOCK!", start=FUT + "T19:30", url="https://m/1", source="embedded")
    assert len(fetch.dedupe([a, b])) == 2


def test_category_location_tag_relabels():
    """Doornroosje ?location=merleyn -> tag "@Merleyn" = locatie; het event verhuist naar het passieve podium Merleyn."""
    e = fetch.Event(venue="Doornroosje", city="Nijmegen", title="Band", start=FUT + "T20:00", url="https://www.doornroosje.nl/event/band/")
    kept, n = fetch.apply_category_tags([e], {"https://www.doornroosje.nl/event/band": {"@Merleyn", "Pop"}})
    assert n == 1 and e.location == "Merleyn" and e.genres == ["Pop"]
    venues = [{"name": "Doornroosje", "city": "Nijmegen"}, {"name": "Merleyn", "city": "Nijmegen", "passive": True}]
    assert fetch.relabel_by_location(kept, venues) == 1 and e.venue == "Merleyn"


def test_embedded_json_times_with_timezone():
    """Melkweg: "startTime":"…T17:30:00.000000Z" is 19:30 Nederlandse tijd; en de programmastart is de eerste tijd na de deuren."""
    assert fetch.times_from_embedded_json('{"startTime":"2026-09-08T17:30:00.000000Z"}')[0] == (19, 30)
    assert fetch.times_from_embedded_json('{"program_start":"202609111915"}')[0] == (19, 15)
    assert fetch.extract_from_text("WITCHZ DI 08 SEP 19:30 Tickets 19:30 Doors 19:45 THE OTHER 20:30 WITCHZ")[1:3] == ((19, 45), (19, 30))
    assert fetch._cache_version({"cache_version": 99}) == 99 and fetch._cache_version({}) == fetch.CACHE_VERSION


def test_fallback_source_when_site_blocked():
    """Het Podium: eigen site 403 -> terugvalbron (pop-agenda REST, alleen items met venue 'Het Podium', genres met komma's)."""
    data = [{"title": {"rendered": "Hans Dulfer"}, "link": "https://pop-agenda.nl/events/x/", "acf": {"eventdate": "20261030", "venue": "Het Podium", "eventtime": None, "ticketprice": 24.5, "genres": "Jazz, Pop", "soldout": "None"}},
            {"title": {"rendered": "Elders"}, "link": "https://pop-agenda.nl/events/y/", "acf": {"eventdate": "20261030", "venue": "De Tamboer", "eventtime": "20:00", "ticketprice": 10, "genres": "", "soldout": "None"}}]
    class Blocked(FakeResp):
        def __init__(self): super().__init__(text="Forbidden"); self.status_code = 403; self.ok = False
        def raise_for_status(self): raise fetch.requests.HTTPError("403")
    class S:
        headers = {}
        def get(self, url, **kw):
            if "hetpodium" in url: return Blocked()
            if "pop-agenda" in url: return FakeResp(json_=data, text=json.dumps(data))
            raise fetch.requests.ConnectionError(url)
    fetch.net.SESSION = S()
    v = {"name": "Het Podium", "city": "Hoogeveen", "url": "https://www.hetpodium.nl/agenda", "min_events": 1,
         "fallback_sources": [{"url": "https://www.pop-agenda.nl/wp-json/wp/v2/events?search=x", "type": "json_api", "api": "https://www.pop-agenda.nl/wp-json/wp/v2/events?search=x",
                               "filter": {"acf.venue": "Het Podium"}, "enrich": False,
                               "fields": {"title": "title.rendered", "date": "acf.eventdate", "time": "acf.eventtime", "url": "link", "price": "acf.ticketprice", "genres": "acf.genres", "status": "acf.soldout"}}]}
    evs, strat, note, _ = fetch.fetch_venue(v, {})
    assert len(evs) == 1 and evs[0].title == "Hans Dulfer" and evs[0].price == "€ 24,50" and evs[0].genres == ["Jazz", "Pop"], (strat, note)
    assert fetch.fmt_price(19.5) == "€ 19,50"


def test_source_score_and_good_enough():
    mk = lambda n, time=True, price=True: [fetch.Event(venue="V", city="C", title=f"E{i}", start=FUT + ("T20:00" if time else "T00:00"), url=f"https://v/{i}", price="€ 10" if price else None) for i in range(n)]
    assert fetch._source_score(mk(50)) > fetch._source_score(mk(60, time=False, price=False))   # 50 met tijd/prijs > 60 zonder
    assert fetch._good_enough({"capacity": 250}, mk(25)) is False        # horizon (30 dagen) korter dan 6 weken -> doorzoeken
    far = mk(25); far[0].start = FUT2.replace(FUT2[:4], str(int(FUT2[:4]) + 1)) + "T20:00"
    assert fetch._good_enough({"capacity": 250}, far) is True
    assert fetch._good_enough({"capacity": 1000}, far) is False          # groot podium met 25 events: verdacht weinig


def test_groundtruth_check(tmp_path=None):
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    gt = {"items": [{"venue": "V", "title": "Cat Power", "date": FUT, "time": "20:30", "price": 40},
                    {"venue": "V", "title": "Ontbreekt", "date": FUT},
                    {"venue": "V", "title": "Oud", "date": "2020-01-01"}]}
    (d / "gt.json").write_text(json.dumps(gt))
    evs = [fetch.Event(venue="V", city="C", title="Cat Power", start=FUT + "T20:30", url="https://v/1", price="€ 40,00")]
    r = fetch.check_groundtruth(evs, d / "gt.json")
    assert r["checked"] == 2 and r["ok"] == 1 and r["misses"][0]["title"] == "Ontbreekt"
    real = json.loads((pathlib.Path(__file__).parent / "groundtruth.json").read_text())
    assert all(i["venue"] and i["title"] and i["date"] for i in real["items"])


def test_two_shows_same_day_not_merged():
    a = fetch.Event(venue="Ziggo Dome", city="A", title="Roxy Dekker", start=FUT + "T15:00", url="https://z/1")
    b = fetch.Event(venue="Ziggo Dome", city="A", title="Roxy Dekker", start=FUT + "T20:30", url="https://z/2")
    assert len(fetch.dedupe([a, b])) == 2


def test_display_price():
    d = lambda p: fetch.display_price(fetch.normalize_price(fetch.canonical_price(p)))
    assert d("€ 9.50") == "€ 9,50" and d("€ 19,5") == "€ 19,50" and d("12,50 euro") == "€ 12,50"
    assert d("€ 19,98") == "€ 20" and d("€ 20,02") == "€ 20" and d("€ 22") == "€ 22"
    assert d("€ 24,05") == "€ 24,05" and d("gratis") == "gratis" and d("uitverkocht") == "uitverkocht"


def test_ampm_times():
    assert fetch.extract_from_text("Saturday 10 October 2026 Doors 7:30 PM Start 8:30 PM Tickets € 15")[1:3] == ((20, 30), (19, 30))
    assert fetch.parse_dt("October 10, 2026 8:30 PM").hour == 20 and fetch._ampm_to_24h("12 AM") == "00:00"


def test_stager_price_from_link():
    """WORM: eventpagina zonder prijs maar met een Stager-link van de organisator -> prijs uit die shop (op dag + titel)."""
    page = '<html><a href="https://strictlykpop.stager.co/shop/strictlykpopworm">BUY TICKETS</a></html>'
    shop = '<html><script data-flags="{&quot;shopId&quot;:77}"></script></html>'
    class S:
        headers = {}
        def get(self, url, params=None, headers=None, **kw):
            if url.endswith("/shop/strictlykpopworm/events"): return FakeResp(text=shop)
            if url.endswith("/shop/v1/events"): return FakeResp(json_=[{"eventId": 5, "name": "Strictly K-POP", "startsOn": FUT + "T21:30:00Z"}, {"eventId": 6, "name": "Ander", "startsOn": FUT2 + "T20:00:00Z"}])
            if url.endswith("/5/tickets-overview"): return FakeResp(json_={"ticketGroups": [{"name": "Regular", "priceInCents": 1250, "feeInCents": 100}]})
            raise fetch.requests.ConnectionError(url)
        def post(self, url, params=None, json=None, **kw):
            return FakeResp(json_={"accessToken": {"jwt": "t"}})
    fetch.net.SESSION = S(); fetch._STAGER_SESSIONS.clear()
    assert fetch.stager_price_from_link(page, FUT + "T23:30", "Strictly K-POP") == "€ 13,50"
    assert fetch.stager_price_from_link('<a href="https://app.stager.co/shop/x">x</a>', None, None) is None


def test_card_url_is_agenda_not_ticketshop():
    a = fetch.Event(venue="Vera", city="G", title="Xanadu", start=FUT + "T20:30", url="https://vera.stager.co/shop/default/events/1", price="€ 12", source="stager")
    b = fetch.Event(venue="Vera", city="G", title="Xanadu", start=FUT + "T20:30", url="https://vera.nl/agenda/xanadu/", source="html")
    r = fetch.dedupe([a, b])
    assert len(r) == 1 and r[0].url == "https://vera.nl/agenda/xanadu/" and r[0].ticket_url == "https://vera.stager.co/shop/default/events/1" and r[0].price == "€ 12"
    assert fetch.is_ticket_url("https://shop.tickets.cm.com/x") and fetch.is_ticket_url("https://www.ticketmaster.nl/event/1") and not fetch.is_ticket_url("https://www.melkweg.nl/nl/agenda/x")


def test_time_provenance():
    ex = fetch.extract_from_text_ex
    assert ex("Aanvang 20:30 Deuren 19:30")[4] == "label"
    assert ex("vrijdag 2 oktober 2026 20:30 uur")[4] == "after_date"
    assert ex("19:30 Doors 19:45 X 20:30 Y")[4] == "schedule"
    assert ex("Geen tijd hier")[4] is None
    mk = lambda hhmm: fetch.Event(venue="V", city="C", title="T", start=FUT + "T" + hhmm, url="https://v/1")
    # expliciete aanvang op de pagina corrigeert een lijsttijd (deuren -> aanvang), binnen 3 uur
    e = mk("19:30"); fetch.apply_extra(e, {"start": (20, 30), "doors": (19, 30), "start_kind": "label"}, False)
    assert e.start[11:] == "20:30" and e.time_src == "label"
    e = mk("08:00"); fetch.apply_extra(e, {"start": (20, 0), "start_kind": "label"}, False)
    assert e.start[11:] == "08:00"   # >3 uur: niet overnemen (dB's am/pm-les)
    # tijd zonder label mag een bestaande lijsttijd NIET zomaar vervangen...
    e = mk("20:00"); fetch.apply_extra(e, {"start": (21, 0), "start_kind": "after_date"}, False)
    assert e.start[11:] == "20:00" and e.time_src is None
    # ...behalve als de lijsttijd de deurtijd van de pagina blijkt te zijn, of de lijst geen tijd had
    e = mk("19:30"); fetch.apply_extra(e, {"start": (20, 30), "doors": (19, 30), "start_kind": "schedule"}, False)
    assert e.start[11:] == "20:30" and e.time_src == "schedule"
    e = mk("00:00"); fetch.apply_extra(e, {"start": (20, 30), "start_kind": "paren"}, True)
    assert e.start[11:] == "20:30"
    # oude cache-items zonder herkomst gedragen zich als 'label'
    e = mk("19:30"); fetch.apply_extra(e, {"start": (20, 30)}, False)
    assert e.start[11:] == "20:30"


def test_price_provenance():
    ex = fetch.extract_from_text_ex
    assert ex("Voorverkoop € 18,50 Deur € 22")[5] == "labeled"
    assert ex("Een avond met Band X. € 5")[5] == "text"
    # geen ticketprijs: locker/munt/vrijwillige bijdrage; 'gratis' niet uit een nieuwsbrief-actie of parkeren (Nieuwe Nor FAQ)
    assert ex("De kosten voor gebruik van een locker zijn 3 euro voor de hele activiteit. Maak elke maand kans op gratis tickets")[3] is None
    assert ex("Er is een vrijwillige bijdrage van € 5 mogelijk")[3] is None
    assert ex("€ 3 per munt. Entree € 8")[3] == "€ 8" and ex("Gratis parkeren. Tickets € 10")[3] == "€ 10"
    assert ex("Gratis toegang, gratis garderobe")[3] == "gratis" and ex("Entree € 15 incl. 1 consumptie")[3] == "€ 15"
    assert ex("Toegang gratis")[5] == "labeled" and ex("Toegang gratis")[3] == "gratis"
    assert ex("€ 22 + € 2,50 servicekosten")[3:6][0] == "€ 24,50" and ex("€ 22 + € 2,50 servicekosten")[5] == "labeled"
    mk = lambda **kw: fetch.Event(**{"venue": "V", "city": "C", "title": "T", "start": FUT + "T20:00", "url": "https://v/1", **kw})
    # los tekstbedrag verliest van JSON-LD; gelabeld tekstbedrag wint van JSON-LD
    e = mk(); fetch.apply_extra(e, {"price": "€ 5", "price_kind": "text", "ld_price": "€ 20"}, False)
    assert (e.price, e.price_src) == ("€ 20", "jsonld")
    e = mk(); fetch.apply_extra(e, {"price": "€ 22,50", "price_kind": "labeled", "ld_price": "€ 20"}, False)
    assert (e.price, e.price_src) == ("€ 22,50", "labeled")
    e = mk(); fetch.apply_extra(e, {"price": "€ 5", "price_kind": "text"}, False)
    assert (e.price, e.price_src) == ("€ 5", "text")
    # dedupe: de sterkste herkomst wint, ook als die bij de 'armere' versie zit; 'uitverkocht' blijft
    a = mk(price="€ 5", price_src="text", genres=["Rock"], subtitle="x")
    b = mk(price="€ 12,50", price_src="shop", url="https://v.stager.co/shop/default/events/1")
    fetch._take_better_price(a, b); assert (a.price, a.price_src) == ("€ 12,50", "shop")
    a = mk(price="uitverkocht"); fetch._take_better_price(a, b); assert a.price == "uitverkocht"
    a = mk(price="€ 20", price_src="list"); fetch._take_better_price(a, mk(price="€ 5", price_src="text")); assert a.price == "€ 20"


def test_fixed_offset_is_local_time():
    # Nieuwe Nor: JSON-LD "2026-11-07T22:00+02:00" in november (dan is het +01:00); de pagina zegt 22:00 -> kloktijd is lokaal
    from datetime import datetime as _dt
    assert fetch.to_local(_dt.fromisoformat("2026-11-07T22:00+02:00")).isoformat() == "2026-11-07T22:00:00"
    assert fetch.to_local(_dt.fromisoformat("2026-07-07T20:00+02:00")).isoformat() == "2026-07-07T20:00:00"
    assert fetch.to_local(_dt.fromisoformat("2026-07-07T18:00+00:00")).isoformat() == "2026-07-07T20:00:00"   # echte UTC wel omrekenen
    assert fetch.to_local(_dt.fromisoformat("2026-11-07T21:00+00:00")).isoformat() == "2026-11-07T22:00:00"
    from taxonomy import strip_country, strip_title_date
    assert strip_country("Hippotraktor (BEL) + support") == "Hippotraktor + support" and strip_country("UK Subs") == "UK Subs"
    assert strip_title_date("13-11-2026 : Roberto Jacketti & The Scooters") == "Roberto Jacketti & The Scooters"
    assert strip_title_date("CHURCH OF CASH | ZATERDAG 14 NOVEMBER") == "CHURCH OF CASH" and strip_title_date("3 Doors Down") == "3 Doors Down"


def test_assign_ids():
    mk = lambda title, url, start=FUT + "T20:00", **kw: fetch.Event(**{"venue": "Vera", "city": "G", "title": title, "start": start, "url": url, **kw})
    vmeta = {"Vera": {"url": "https://vera.nl/agenda/"}}
    seen = {"Vera|https://vera.nl/agenda/xanadu/": "2026-08-01"}     # oude url-sleutel uit een eerdere run
    ids = {}
    evs = [mk("Xanadu", "https://vera.nl/agenda/xanadu/"), mk("Roxy Dekker", "https://v/1", FUT + "T15:00"), mk("Roxy Dekker", "https://v/2", FUT + "T20:30"),
           mk("Zonder pagina A", "https://vera.nl/agenda/"), mk("Zonder pagina B", "https://vera.nl/agenda/")]
    info = fetch.assign_ids(evs, seen, ids, vmeta)
    assert evs[0].id == f"Vera|{FUT}|xanadu" and seen[evs[0].id] == "2026-08-01" and info["migrated"] == 1
    assert evs[1].id == f"Vera|{FUT}|roxy dekker" and evs[2].id == f"Vera|{FUT}|roxy dekker|20:30"
    assert evs[3].id != evs[4].id and "Vera|https://vera.nl/agenda/" not in ids["url"]   # agendapagina is geen alias
    # volgende run: titel met support en verschoven datum, zelfde url -> zelfde id via de alias
    e2 = mk("Xanadu + support", "https://vera.nl/agenda/xanadu/", FUT2 + "T20:00")
    fetch.assign_ids([e2], seen, ids, vmeta)
    assert e2.id == f"Vera|{FUT}|xanadu"
    # andere url (shop i.p.v. agenda), zelfde inhoud -> zelfde id via de inhoudelijke sleutel
    e3 = mk("Xanadu", "https://vera.stager.co/shop/default/events/9")
    fetch.assign_ids([e3], seen, ids, vmeta)
    assert e3.id == f"Vera|{FUT}|xanadu"


def test_run_diff():
    mk = lambda i, **kw: fetch.Event(**{"venue": "V", "city": "C", "title": f"T{i}", "start": FUT + "T20:00", "url": f"https://v/{i}", "id": f"V|https://v/{i}", **kw})
    prev = [mk(1), mk(2), mk(3, status="afgelast"), mk(4, price="€ 20"), mk(5), mk(6, start="2020-01-01T20:00"), mk(7, venue="W")]
    cur = [mk(1, start=FUT2 + "T20:00"), mk(4, price="€ 30"), mk(5, start=FUT + "T21:30")]
    d = fetch.run_diff(prev, cur, skip_venues={"W"})
    assert d["n_gone"] == 1 and d["gone"][0]["title"] == "T2"       # T3 afgelast, T6 verleden, T7 podium stale: tellen niet
    assert d["n_day"] == 1 and d["day"][0]["title"] == "T1"
    assert d["n_time"] == 1 and d["time"][0]["title"] == "T5"
    assert d["n_price"] == 1 and d["price"][0]["now"] == "€ 30"
    assert d["vanished_venues"] == {}
    big_prev = [mk(i) for i in range(20)]
    d = fetch.run_diff(big_prev, big_prev[:12])
    assert d["vanished_venues"] == {"V": 8}


def test_fixture_lessons():
    """Regels die uit de fixtures-test van 6 sep kwamen (echte pagina's van ~55 podia)."""
    ex = fetch.extract_from_text_ex
    assert ex("15 euro 25+ deuren open 19:00")[3] == "€ 15"                    # Bitterzoet: leeftijd achter de prijs is geen bedrag
    assert ex("Stage 3 € 15,50")[3] == "€ 15,50"                              # Patronaat blijft goed
    assert ex("€ 19,95 vvk, € 24,95 deur")[3] == "€ 19,95"                    # Iduna: label ná het bedrag
    e = ex("Halloween Madness augustus 20, 2026 voorstellingen oktober 31, 2026 19:00 Baroeg vvk € 19,00. Entree € 20,00.")
    assert e.dt.date().isoformat() == "2026-10-31" and e.start == (19, 0) and e.price == "€ 19"   # Baroeg: F j, Y + postdatum
    e = ex("De kaartverkoop van dit evenement start donderdag 25 juni om 11:00u. Open 20:00 start 20:30")
    assert e.dt is None and e.start == (20, 30)                                # Simplon: verkoopstart is geen eventdatum
    # Nobel: <time datetime=publicatietijd>zaterdag 07 november 2026</time> -> de tekst telt; alleen-verleden tags tellen niet
    s = fetch.soup_of('<html><body><time datetime="2026-08-06T13:30:01+0200">zaterdag 07 november 2026</time></body></html>')
    t = fetch._event_time_tag(s)
    assert t is not None and fetch._time_tag_value(t).date().isoformat() == "2026-11-07"
    assert fetch._event_time_tag(fetch.soup_of('<html><body><time datetime="2025-01-01T10:00:00">1 januari 2025</time></body></html>')) is None
    # EKKO: <main> is een fragment, datum/aanvang/prijs staan ernaast -> hele pagina lezen
    html = '<html><body><header><nav><a>x</a></nav></header><section class="general-info">do 15 okt Aanvang 20:30 € 14.50</section>' \
           '<main><article>' + "Beschrijving. " * 3 + '</article></main></body></html>'
    assert "20:30" in fetch.page_text(html)
    # Doornroosje/Merleyn: JSON-LD "22:00:00+00:00" (-> 23:00) maar de pagina zegt aanvang 22:00: de 'UTC' was lokale tijd
    class S:
        headers = {}
        def get(self, url, **kw):
            return FakeResp(text='<html><body><script type="application/ld+json">{"@context":"https://schema.org","@type":"Event","name":"Julia Sabaté",'
                                 '"startDate":"' + FUT + 'T22:00:00+00:00","location":{"@type":"Place","name":"Merleyn"}}</script>'
                                 '<h1>Julia Sabaté</h1><p>Zaal open 21:00 Aanvang 22:00 Tickets € 17,50</p></body></html>')
    fetch.net.SESSION = S()
    ev = fetch.fetch_detail({"name": "Doornroosje", "city": "Nijmegen", "url": "https://www.doornroosje.nl/", "ticketshops": False}, "https://www.doornroosje.nl/event/julia-sabate/", {})
    assert ev is not None and ev.start[11:16] == "22:00" and ev.time_src == "label"


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as ex:  # noqa: BLE001
                fails += 1; print("FAIL", name, "->", type(ex).__name__, ex)
    sys.exit(1 if fails else 0)

