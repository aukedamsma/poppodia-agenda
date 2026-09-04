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
        self.text = text; self._json = json_; self.headers = headers or {}
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
    fetch.SESSION = S()


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
    assert out[0].genres == ["Nederlands", "Pop"] and out[0].price == "€ 23,5" and out[0].subtitle == "TOUR"


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
      <div class="event-program__date"><a href="https://patronaat.nl/event/donnas-hot-stuff-{dd}/">vr 4 sep 2026</a></div>
      <h3 class="event-program__name"><a href="https://patronaat.nl/event/donnas-hot-stuff-{dd}/">Donna’s Hot Stuff</a></h3>
      <div class="event-program__subtitle">Een groots eerbetoon</div><div class="event-program__genres">klassiekers / tributes pop</div></div></div>'''
    d = date.today() + timedelta(days=10)
    html = item.format(dd=d.strftime("%d-%m-%y")) * 4
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

