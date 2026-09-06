# Podiumagenda

Eén dagelijks bijgewerkte agenda met alle concerten van de Nederlandse poppodia die ik volg,
zodat ik niet elke site apart hoef af te zoeken. Draait gratis op GitHub Actions, de pagina staat op GitHub Pages.

- **Agenda-pagina**: `docs/index.html` (via Pages op `https://aukedamsma.github.io/poppodia-agenda/`)
- **Agenda-app**: abonneer op `docs/agenda.ics`
- **Ruwe data**: `data/events.json`, status per podium in `data/report.json`

## Hoe het werkt

1. `fetch.py` leest `venues.yaml` en haalt per podium de agenda op. Per podium wordt van schoon naar ruw geprobeerd:
   schema.org JSON-LD op de agendapagina → ingebedde JSON (Next.js, Angular) → The Events Calendar API →
   WordPress REST API → JSON-LD op de losse eventpagina's (gecached, 10 dagen) → CSS-selectors uit `venues.yaml`.
2. Alles wordt genormaliseerd naar één lijst (`data/events.json`). Event-URL's die de vorige run nog niet bestonden
   krijgen een `first_seen`-datum; de pagina toont die 7 dagen als **nieuw**.
3. `build.py` bouwt `docs/index.html` en `docs/agenda.ics`.
4. `.github/workflows/daily.yml` draait dit elke ochtend om 07:00 en commit het resultaat.

Een falend podium blokkeert nooit de rest: het staat dan met een ✕ in de status-voettekst van de pagina,
met de reden in `data/report.json`.

## Genres en artiesten

- `genres.yaml` bevat de taxonomie: ~27 hoofdgenres met één-woordslabels (naar Bandcamp Discover, toegesneden op de Nederlandse
  poppodia; Afrobeats staat los van Wereldmuziek; comedy/cabaret is geen muziekgenre en valt onder Spoken word met type 'talk') plus regels die ruwe podiumtags op een hoofdgenre afbeelden. Ruwe tags blijven bewaard als subgenre.
  Onbekende tags staan na elke run in `data/report.json` onder `unknown_genres`; voeg ze toe aan de regels.
- `taxonomy.py` herkent artiesten in titels/ondertitels, bepaalt het eventtype (concert / club / festival / talk / other;
  regels in `genres.yaml` onder `kinds`, aanvangstijd telt mee: vanaf 23:00 is het een feest) en de prijs als getal.
- `series.py` beheert `state/series.json`: terugkerende events per podium ("Cheeky Monday", "Jazz Jam #14", "VroegZat")
  met hun gebruikelijke prijs, aanvangstijd en type. Ontbrekende waarden worden daaruit ingevuld en gemarkeerd (`~ € 7,50`).
- `artists.py` beheert `state/artists.json`: een kennisbank die elke run groeit (genre-stemmen van podia, medespelers,
  podia, MusicBrainz-tags en — met `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` als GitHub-secrets — Spotify-genres en
  populariteit). Events zonder genre krijgen er een via de kennisbank. Basis voor de smaakscore.

## Methodiek: regels die voor álle podia gelden

Elke fout die bij één podium is gevonden, is omgezet in een generieke regel in `fetch.py`, zodat hij bij alle
(ook toekomstige) podia wordt afgevangen:

| Les (waar gevonden) | Regel |
|---|---|
| Aanvang vs. deuren (PAARD, Tivoli) | Een expliciete *aanvang/start* op de eventpagina wint altijd van de tijd in de lijst; *deuren* alleen als er niets beters is (`apply_extra`). |
| Lokale tijd met foute tijdzone (Doornroosje, +1u) | Steekproef per podium: wijkt de aanvang op de eventpagina structureel 1–2 uur af, dan worden die events gecorrigeerd en krijgt het podium een `!` met "zet `time_is_local`" in het rapport (`audit_venue`). |
| Datum van vandaag uit de sitekop (ECI, 74 films op één dag) | Een datum uit losse tekst die precies vandaag is zonder tijd wordt genegeerd; blijft er toch een cluster (>25% van de events op één dag zonder tijd), dan worden die events verwijderd en gemeld. |
| Categoriepagina's als event (Cpunt "Film", "Exposities") | `/agenda/<categorie>` en soortgelijke paden zijn nooit een event (`NON_EVENT_PATH`). |
| Sitemap op id, niet op datum (Paradiso) | Sitemap-URL's worden op `lastmod` gefilterd (`sitemap_recent_days`): aankomende events worden bijgewerkt, oude niet meer. |
| Gemengd programma (ECI, Cacaofabriek, Tivoli) | Eventtype uit titel + podiumtags + aanvangstijd; film/theater/talk/kids verdwijnen uit "Concert". Waar het podium een aparte muziekpagina heeft, gebruiken we die. |
| Line-up en genre alleen in de beschrijving (Tivoli, pop-agenda) | JSON-LD `performer`, een `lineup`-selector of de beschrijving leveren artiesten; specifieke genrewoorden in de beschrijving ("postpunk", "garagerock") worden tags als het podium er geen geeft. |
| Het podium heeft zelf genre-/typefilters op de agenda (Tivoli `?sf_genre=pop`, `kennis-debat`, `familie`…) | `category_pages` in `venues.yaml`: per filter worden de overzichtspagina's opgehaald (gepagineerd tot 404) en de events getagd met de eigen indeling van de programmeurs. Die tags gaan vóór tekst-hints en sturen hoofdgenre, subgenre én type Muziek/Overig. Ook locatiefilters: events in een ander gebouw dat al als eigen podium in de lijst staat (Doornroosje in De Vereeniging/Goffert) worden via `exclude` weggelaten. Eén keer per dag gecached. Nu actief voor Tivoli, Doornroosje, Vera, FLUOR, 't Beest, Volt en Cpunt; bij nieuwe podia eerst kijken of zo'n filter bestaat (van de 74 VNPF-podia hebben er ~15 een server-side filter; de rest filtert alleen in JavaScript). |
| Agenda alleen via JavaScript/API (Boerderij events.php, Mezz WP-REST met eigen velden, De Pul 'laad meer', Bibelot FacetWP, Q-factory/BIRD alleen sitemap, Het Podium ?resultaten=) | Nieuwe generieke bronsoorten: `json_api` (eigen JSON-eindpunt met veldafbeelding), `html` + `api` (HTML-fragmenten uit een laad-meer-eindpunt), FacetWP (JSON-POST met paged, automatisch herkend), gepagineerde HTML (`list_pages_template`), sitemap als gewone urlset met `lastmod`-filter, `detail_date`/`group_date` voor datums buiten de kaart. Bij een nieuw podium eerst in de browser kijken welke verzoeken de agenda doet. |
| Ticketshop als vollediger bron (Vera toont 3 weken, Stager 50+ events; Hall of Fame alleen via de shop; Simplon-prijzen alleen in de shop) | `stager`-bron: de Stager-shop-API (anonieme sessie, events per 20, ticketprijzen per event) wordt automatisch toegevoegd zodra een podium naar `<slug>.stager.co` linkt; WordPress-sites met de Stager-koppeling (`acf.stager_*`) leveren programma-start, deuren, prijzen en uitverkocht direct. Dubbele events (site + shop) worden samengevoegd; de URL van de eigen site wint. |
| Site weigert onbekende user-agents (Het Podium, So What!, GIGANT: Cloudflare 403) | Eén herhaalde poging met gewone browser-headers; daarna onthoudt de fetcher dat voor die host. robots.txt staat crawlen toe. |
| Publicatiedatum als eventdatum (Gelderlandfabriek `<time class="entry-date">` van augustus bij een event in september) | `<time>`-elementen met publicatieklassen (entry-date, published, updated, byline) tellen niet; de eventdatum komt uit de tekst of `detail_date`. |
| Podium programmeert in een ander gebouw (Paradiso in Tolhuistuin/Melkweg, Doornroosje in De Vereeniging) | Locatie wordt meegelezen; staat die locatie zelf als podium in `venues.yaml`, dan verhuist het event daarheen en worden dubbelen met het eigen programma van dat podium samengevoegd (`relabel_by_location`, `dedupe` op dag + titel). |
| Zelflerend type leert onzin uit genretags (postpunk → festival via Le Guess Who, latin → club) | Een tag die een muziekgenre is leert alleen een ander type dan concert als er een typewoord in staat (clubnacht, dance nights, rave, film, comedy…); `kind_tag_overrides` in genres.yaml corrigeert de rest. |
| Bandnamen die op feesten lijken ("Jungle By Night", "Two Door Cinema Club") | Zwakke feestwoorden (night, club, disco) tellen alleen samen met een late aanvang. |
| Terugkerende events zonder tijd | Reeksengeheugen vult de tijd aan uit eerdere edities, gemarkeerd als schatting (~). Prijzen worden nooit geschat: alleen een geparsete prijs (of 'uitverkocht') komt op de kaart. |
| Prijs/tijd alleen in ingebedde JSON (Melkweg `"price":"€ 24,05"`, Vorstin URL-gecodeerde `program_start`/`door_open`) | `price_from_embedded_json` en `times_from_embedded_json` lezen JSON-blobs in de HTML (ook URL-gecodeerd) als de zichtbare tekst niets oplevert; het 'primary' ticket wint. |
| Tijd vóór het label ("19:30 Zaal Open", 013) en tijdschema's zonder 'aanvang' (Nobel "19:00 - Deuren open 19:30 - Band") | Beide woordvolgordes worden herkend; de vroegste tijd is de deurtijd, de eerste tijd erna de start. |
| Meerdere bedragen op een pagina (Willemeen "Door €16 / Early €12 / Regular €14,50", So What! "Leden €5 / Regulier €10 / Voorverkoop €8") | De prijs is die van nú online een gewoon ticket bestellen: het label direct vóór elk bedrag bepaalt de context; leden/CJP/jeugd/early bird/deur/dagkassa vallen af, voorverkoop/online wint van 'regulier', dat van de rest (`_pick_price`). |
| Servicekosten (De Pul "€ 24,00 inclusief € 2 servicekosten", FLUOR "Gratis · incl. € 1,75 servicekosten", Stager-shops met aparte fee) | De prijs is wat je online betaalt, dus inclusief servicekosten: een 'inclusief'-bedrag blijft staan, een expliciet 'excl./+ € Y servicekosten' wordt opgeteld, een fee zonder bedrag telt niets op (niet schatten). In Stager-shops wordt de fee bij de ticketprijs geteld. Gratis blijft gratis, hoe hoog de servicekosten ook zijn (`_strip_service_fee`, `_fee_cents`). |
| Landaanduiding achter een naam (Vera "mary in the junkyardUK", Tivoli "KATE CLOVER (USA)", "SIGH (Japan)") | Land als toevoeging wordt uit titel en artiestnaam gehaald zodat dezelfde band overal onder één naam staat; alleen als suffix (tussen haakjes, vastgeplakt in hoofdletters, of los aan het eind), nooit als deel van de naam ("UK Subs", "US Girls", "Made in USA") (`strip_country`). |
| Eindtijd gelezen als aanvang (Tivoli "21:00 Deuren open 21:00 Aanvang 02:00 Verwachte eindtijd": 40UP om 02:00, Discozwemmen om 03:00) | Tijden met een eindlabel (eindtijd, einde, tot) tellen niet als aanvang; de layout waarde-vóór-label en label-vóór-waarde worden beide herkend. Een 'aanvang' vóór 06:00 terwijl de deuren 's avonds opengaan is per definitie een eindtijd. |
| Podium programmeert elders zonder dat in JSON-LD te zeggen (Tivoli in De Helling: Krallice stond bij beide, met verschillende tijden) | Locatie uit de paginatekst ("vindt plaats in De Helling", ondertitel "… \| in De Helling", "Locatie: …") en uit de ondertitel; is dat een podium uit venues.yaml, dan verhuist het event daarheen en haalt de dedupe de dubbele weg (`location_from_text`, `relabel_by_location`). |
| Eén festival/reeks onder veel namen (Popronde, Popronde 2026, Popronde Alkmaar, Popronde Arnhem 2026) | Jaartal en de eigen stad achter een naam zijn geen deel van de naam: in de artiestweergave valt alles onder één kop (`strip_city`, jaartal weg in `_clean_name`). |
| Samenwerking van twee podia op een derde plek (BIRD + Rotown: "Zwangere Guy \| Maassilo Rotterdam" en "Zwangere Guy", zelfde avond) | Zelfde titel, stad, dag en tijd (±30 min) bij verschillende podia = één event: één kaart, medeorganisator in `co_venues`, de plek uit de titel in `location`; de kaart toont "Maassilo (BIRD & Rotown)" (`merge_coproductions`). |
| Ruwe prijstekst uit een bron (De Peppel Tribe "Voorverkoop €21,00 Deurticket €26,00", FLUOR "$18,50", Ahoy "€ € 40,00") | Elke prijs gaat door dezelfde parser als eventpagina's (`canonical_price`): online/voorverkoopprijs, inclusief servicekosten, één bedrag. |
| Tijdcontrole corrigeerde gestructureerde tijden met tekstheuristiek (LantarenVenster: "Dezelfde avond om 20:30 uur treedt Teus Nobel op" gaf -1u op een event van 19:00) | De steekproef corrigeert alleen nog events uit lijst-HTML; JSON-LD/Stager/API-tijden worden gemeld maar niet overschreven. |
| Kaart linkt naar de ticketshop i.p.v. de eventpagina (De Pul: kaart is `<a href="/agenda/…">` met daarin een ticketlink naar shop.tickets.cm.com; tijd/prijs bleven 6%) | De kaart zelf als die een link is, anders de eerste link op de eigen site, pas daarna een externe link. Daardoor werkt de verrijking vanaf de eventpagina. |
| Datum met weekdag erachter (Grenswerk "05.09 zaterdag": 138 → 1 events na de strengere datumparser) | dag.maand met een weekdag ervoor óf erna is een datum; regressietest toegevoegd. |
| Datum staat twee keer op de pagina, alleen de tweede keer met tijd (Estrado "vr 02 okt … vrijdag 2 oktober 2026 20:30 uur") | Elke vermelding van dezelfde datum wordt geprobeerd voor een tijd direct erachter. |
| Sitemap als bron mist events (Paradiso: event_*.xml op id gesorteerd, 100 URL's per bestand, dagelijks verse lastmods; cap van 900 liet Cat Power, Fat Freddy's Drop en Panchiko weg) | De GraphQL-API die de site zelf gebruikt is de lijstbron (`graphql_detail`): endpoint en publieke client-token worden elke run uit de JS-bundel gelezen (nooit opgeslagen), cursor-paginering, ~470 events met locatie/uitverkocht/status; de eventpagina blijft de bron voor prijs en aanvang. |
| Uitverkochte/afgelaste events verdwijnen uit de bron-JSON (Melkweg zet `isPublished:false`; 49 van 282 events, waaronder Khamari, MUNA, Melanie C) | `isPublished:false` telt alleen als er geen status (uitverkocht/afgelast) bij staat; uitverkocht hoort in de agenda. Gevonden via de vergelijking met pop-agenda.nl. |
| Coproductie-samenvoeging te gretig (run #22: 500 dubbelen, De Helling 79 → 34, Rotown 106 → 68: alles op dezelfde tijd in dezelfde stad werd één event) | Over podia heen telt alleen de titel (gelijk, bevat, of ≥ 75% gedeelde woorden); "zelfde tijd, andere bron" geldt alleen binnen één podium. Generieke titels (festival, clubnacht, quiz …) worden nooit samengevoegd. |
| Kleine zaal met eigen adres zit verstopt in de agenda van het grote podium (Merleyn in Doornroosje; pop-agenda.nl telt er 42 events) | Locatiefilter van het podium met tag `@Merleyn` (@ = locatie, geen genre) en een passief podium in venues.yaml (`passive: true`, geen eigen bron): de events verhuizen daarheen. Zelfde mechanisme voor Zonnehuis (Paradiso). |
| Grote zalen zonder server-side agenda (Ziggo Dome: Next.js, 0 events; AFAS Live: 15 van ~118) | Ziggo Dome via de eigen JSON-API (`/api/agenda/aankomend`, `time_utc: true` want showDate is UTC, genres als JSON-string); AFAS Live via de HTML-agenda (alle maanden in één pagina). Gevonden via pop-agenda.nl (79 resp. 44 events). |
| Botmuur keurt de TLS-handdruk af, niet de headers (GIGANT, Het Podium: 403 ook met browser-headers; Q-factory: 202-challenge) | Derde trap in `http_get`: na eigen user-agent en browser-headers volgt een GET met de TLS-vingerafdruk van Chrome (`curl_cffi`, `impersonate="chrome"`); per host onthouden. Alleen voor sites waarvan robots.txt crawlen toestaat. |
| Tijd uit ingebedde JSON in UTC (Melkweg `"startTime":"…T17:30:00Z"` = 19:30; alle Melkweg-tijden liepen 2 uur voor omdat deze waarde de correcte lijsttijd overschreef) | Tijdzone in JSON-tijdvelden wordt omgerekend naar Nederlandse tijd; bij een tijdschema ("19:30 Doors 19:45 support 20:30 headliner") is de eerste tijd ná de deuren de aanvang. Per podium `cache_version` om alleen díe eventpagina's opnieuw te lezen. |
| Botmuur blokkeert op IP-niveau, ook met Chrome-TLS (Het Podium; zusterorganisatie De Tamboer onderscheidt de zalen niet) | `fallback_sources`: bronnen die alleen meedoen als de eigen site niets oplevert (mechanisme beschikbaar, bv. voor een eigen ticketshop). Concurrerende aggregators (pop-agenda.nl, podiuminfo.nl) zijn géén databron, alleen interne controle; Het Podium blijft daarom leeg tot de blokkade verdwijnt. |
| Sitemap-antwoord is een botmuur (Q-factory: 202-challenge, geen `<urlset>`) | `_looks_blocked` herkent een .xml zonder `<urlset>`/`<sitemapindex>` en een 202-status als blokkade, zodat de escalatie (browser-headers, Chrome-TLS) ook voor sitemaps loopt. |
| Stille regressies (Grenswerk 138 → 1 na een parserfix, De Helling 79 → 34 door een dedupe-fout: het podium bleef 'ok') | `state/history.json` bewaart per podium het aantal events van de laatste runs; een daling tot onder 60% van het beste van de laatste 7 runs (of van ≥ 10 naar 0) staat als `regression` in het rapport, in de log ("LET OP regressie") en als ! in de podiumlijst. |
| Agenda verhuisd naar een ander adres (redirect van de agendapagina) | Een redirect naar een andere host of een ander pad wordt gemeld (`redirect` in het rapport) zodat venues.yaml bijgewerkt kan worden voordat de bron stilletjes leegloopt. |
| 'Eerste treffer' als bron (JSON-LD van alleen de eerste pagina won van een volledige HTML-lijst) | Strategieën worden gescoord op aantal × (tijd- en prijsdekking) en de beste wint (`_source_score`); doorzoeken zolang een groot podium (≥ 400) < 40 events heeft of de horizon < 6 weken is (`_good_enough`). |
| Geen vaste toets of geverifieerde events blijven kloppen | `tests/groundtruth.json`: steekproef van events die met de browser op de podiumsite zijn gecontroleerd (podium, dag, titel, evt. tijd en prijs). Elke run vergelijkt (`check_groundtruth`); missers staan in report.json onder `groundtruth` en als LET OP in de log. Aanvullen na elke validatieronde. |
| Botmuur ook op POST-verkeer (Stager-sessie, FacetWP, GraphQL) | `http_post` met dezelfde escalatie naar Chrome-TLS als `http_get`. |
| Twee voorstellingen op één dag werden één (Ziggo Dome: Roxy Dekker 15:00 en 20:30) | Zelfde titel en dag maar tijden ≥ 2 uur uiteen én verschillende URL's = twee shows; gevonden door de groundtruth-toets. |
| Prijsweergave wisselde (€ 9.50, € 19,5, 12,50 euro) | Eén weergave in de laatste stap (`display_price`): altijd komma, twee decimalen (€ 24,50), hele bedragen zonder decimalen (€ 24); tot 4 cent onder/boven een heel bedrag (€ 19,98) wordt afgerond op het hele getal. |
| Prijs staat alleen in een ticketshop van de organisator (WORM: per event een andere Stager-shop, 78% zonder prijs; dB's: tribe-API zonder prijzen) | De Stager-link op de eventpagina wordt gevolgd: anonieme shopsessie, event op id of op dag + titel, `tickets-overview` → reguliere prijs incl. fee (`stager_price_from_link`). Shopherkenning kijkt nu ook op de agendapagina en drie eventpagina's als de API-lijst geen HTML heeft (dB's → dbs.stager.co). |
| Engelstalige tijden ("Doors 7:30 PM") | AM/PM wordt vóór alle tijdpatronen omgezet naar 24-uurs (`_ampm_to_24h`). |
| "€ 0,-" gold als ontbrekende prijs (Tivoli: 75 gratis events zonder prijs) | Een geparset bedrag van nul is 'gratis'. |
| Stager-API-tijden 2 uur te laat (startsOn eindigt op Z maar is Nederlandse tijd; bleef onzichtbaar omdat de dedupe de shopversie wegwerkte, tot de twee-shows-regel ze liet staan: 377 dubbelen in run #26) | `startsOn` als lokale tijd lezen (`_stager_local`). Les: een tijdzone-suffix in een API is geen bewijs; controleer tegen de site. |
| Sitemap noemt alleen anderstalige pagina's (Q-factory: /en/events/) | `url_replace` in venues.yaml herschrijft sitemap-URL's naar de Nederlandse pagina vóór het patroon. Vier redirects uit de nieuwe detectie (Nieuwe Nor, De Spot, Willem Twee, Aa-Theater) in venues.yaml bijgewerkt. |
| Stager-shop met een andere naam dan `default` (Luxor Live: /shop/luxor-live/…; de default-shop is leeg → "0 events via stager", ook bij Muziekgieterij, Neushoorn, Bibelot, Willem Twee) | De shopnaam wordt uit de ticketlink gelezen en meegenomen in de shop-URL en de API-calls. |
| Site om de andere run onbereikbaar (Astrant, Loburg: ConnectionError → podium leeg) | Levert een bron niets op door een fout, dan blijven de events van de vorige run staan (max. 3 runs achter elkaar), gemarkeerd `stale` in het rapport en als ! in de podiumlijst. |
| Tekst-tijd overschreef een juiste API-tijd met een leesfout (dB's "Tijd: 8:00 pm" → 08:00 i.p.v. 20:00, 12 uur ernaast, 53 events) | Een paginatekst mag de lijsttijd alleen corrigeren binnen 3 uur (deuren → aanvang); grotere sprongen zijn leesfouten. Plus AM/PM-omzetting en cache_version voor dB's. |
| Run bleef hangen (run #28: 6 uur, door GitHub afgebroken, niets geschreven) | Waakhond in `main`: per podium max 25 min, per run max 150 min (env `VENUE_MINUTES`/`RUN_MINUTES`); wat dan nog loopt wordt 'timeout' en houdt zijn events van de vorige run; `socket.setdefaulttimeout(60)` als vangnet; hangende threads houden het proces niet open. Zet in `daily.yml` ook `timeout-minutes: 180`. |
| Kaart linkte naar een ticketshop (Stager, CM.com, Ticketmaster) in plaats van de agenda van het podium | `url` is altijd de agendapagina (eventpagina van het podium, anders de agenda-overzichtspagina); de shoplink staat apart in `ticket_url` en verschijnt als klein 'tickets'-knopje op de kaart. Identiteit van een event (`id` = podium + bron-url) blijft stabiel, zodat 'nieuw', archief en het lijstje niet breken. |
| Elke tijd op een eventpagina woog even zwaar, ook een losse tijd achter een datum of uit een tijdschema | Tijdherkomst: `extract_from_text_ex` geeft bij de aanvang aan waar die vandaan komt (`label` = expliciet "aanvang/start", `after_date`, `paren`, `schedule`, `embedded`). Alleen een gelabelde aanvang mag een bestaande lijsttijd corrigeren (binnen 3 uur); ongelabelde tijden vullen alleen aan als de lijst geen tijd had of de lijsttijd de deurtijd bleek. De herkomst staat in `time_src` in events.json. |
| Elke prijsbron woog even zwaar (los bedrag in de tekst kon een JSON-LD-prijs of shopprijs verdringen) | Prijsherkomst met rangorde (`PRICE_RANK`): shop (Stager-API, incl. servicekosten) > lijst/JSON-LD > gelabelde tekstprijs (voorverkoop/tickets/entree/gratis, of met servicekosten) > embedded JSON > los bedrag. Bij samenvoegen van twee versies wint de sterkste herkomst, niet de rijkste versie; 'uitverkocht' blijft altijd staan. `price_src` in events.json; tellingen per run in report.json (`price_src`, `time_src`). |
| Het rapport toonde aantallen per podium, niet wélke events verdwenen of verschoven (een parserfout die het aantal niet raakt bleef onzichtbaar) | Run-op-run verschil (`run_diff`, `report.json` → `diff`): toekomstige events die zonder 'afgelast' verdwenen, van dag verschoven, ≥ 1 uur van tijd verschoven of > € 5 van prijs veranderd, met titel en url. Een podium dat ≥ 30% van ≥ 10 toekomstige events kwijt is krijgt `vanished` (LET OP in log, rapport en podiumlijst). Podia die stale/error/timeout zijn tellen niet mee. |
| Datum zonder herkenbare maand (Cinetol "Sun18.10" → 18 september i.p.v. oktober) | De fuzzy datumparser vult nooit meer een ontbrekende maand aan met de huidige maand: zonder maandnaam of volledige datum is er geen datum. Gevonden door onze data naast pop-agenda.nl te leggen — die vergelijking is een vaste controlestap. |
| Verouderde cache na een parserverbetering (013: prijs bleef leeg omdat de pagina al gecached was) | `CACHE_VERSION` in `fetch.py`: verhogen dwingt het opnieuw ophalen van alle aankomende events af; verleden events blijven gecached. |

Twee zelflerende lagen (bewijs verzamelen → bij voldoende consistentie promoveren tot regel; handmatig vast te zetten in `genres.yaml`):

- **Subgenres** (`state/subgenre_learn.json`): ruwe tags worden afgebeeld op een vaste lijst van ~105 subgenres naar de indeling van
  Bandcamp Discover (spellingvarianten via `aliases`; niet-genres via `subgenre_noise`). Een onbekende tag die ≥ 5 keer en in ≥ 80% van de
  gevallen bij één hoofdgenre voorkomt, wordt automatisch subgenre van dat hoofdgenre.
- **Type** (`state/kind_learn.json`): elke keer dat een regel het type zeker vaststelt, stemmen de podiumtags van dat event mee. Een tag die
  ≥ 5 keer en in ≥ 85% van de gevallen bij één niet-muziektype hoort (Tivoli "Kennis & Debat" → talk) bepaalt daarna zelf het type van
  events zonder andere aanwijzing. Zonder aanwijzing blijft het type muziek (concert).

Elk podium krijgt in `data/report.json` een `audit` met dekking (aandeel events met tijd / prijs / genre), de weekenddekking
(aantal vrijdagen/zaterdagen met een event in de komende 8 weken, van 16 — een podium van 400+ plaatsen met minder dan 11 is
'dun' en krijgt een !), de horizon (verste datum) en waarschuwingen; de voettekst van de site toont dezelfde cijfers. Zo is in één blik
te zien waar de data dun is of waar we minder ver vooruit kijken dan het podium zelf.

## Wat (nog) niet lukt, en waarom

| Podium | Ontbreekt | Reden | Alternatief |
|---|---|---|---|
| Manifesto (Hoorn) | prijs | staat alleen in de Paylogic-shop (JavaScript) | geen; tijd en programma wel compleet |
| De Cacaofabriek | prijs | alleen in de Ticketlab-shop (JavaScript + cookiemuur) | geen |
| Metropool | prijs deels | Ticketmaster; de lijst heeft `data-event-price` voor de meeste events | — |
| Baroeg | tijd | "Het tijdschema wordt in de week van het evenement bekendgemaakt" | de Stager-shop levert de tijd zodra bekend |
| Q-factory | alles, als de Vercel-botmuur ook de Chrome-TLS-handdruk weigert (GIGANT kwam er in run #24 wél mee door) | botblokkade op GitHub-IP's | Ticketmaster (geen server-side data); pop-agenda.nl heeft Q-factory niet |
| Het Podium | alles | eigen site blokkeert GitHub op IP-niveau (403, ook met Chrome-TLS); pop-agenda.nl heeft de events maar is alleen controlebron | geen; opnieuw proberen als de blokkade verdwijnt |
| Hall of Fame | prijs | eigen site is een lege Nuxt-app; de Stager-shop-API levert wel prijzen | — |
| Skatecafe | alles | site onbereikbaar (DNS) | — |

## Archief

`data/archive.json` bewaart elk event dat ooit is opgehaald (sleutel podium|url) met titel, datum, prijs, genres, type,
artiesten, status en first/last_seen. De site toont alleen wat nog komt; het archief groeit elke run en is bedoeld als
onderzoeksdata (programmering en prijzen per podium door de tijd).

## Een podium toevoegen

Voeg een blok toe aan `venues.yaml`. Meestal is dit genoeg:

```yaml
- name: Vera
  city: Groningen
  url: https://www.vera-groningen.nl/programma/
```

Zonder `type` probeert de fetcher alle strategieën automatisch. Levert de volgende run niets op,
kijk dan in `data/report.json` wat er per strategie gebeurde. Als de site alleen platte HTML heeft,
geef dan CSS-selectors op:

```yaml
- name: Patronaat
  city: Haarlem
  url: https://www.patronaat.nl/programma/
  type: html
  item: ".event-program"          # één blok per event
  title: ".event-program__name"
  date: ".event-program__date"    # tekst of <time datetime>
  link: ".event-program__name a"
  genre: ".event-program__genres"
  subtitle: ".event-program__subtitle"
  date_from_url: true             # datum uit de URL (DD-MM-YYYY) als hij ontbreekt
  crawl_delay: 10                 # seconden tussen requests, als robots.txt daarom vraagt
```

Twee bijzondere types voor lastige sites:

- `sitemap_detail` (Paradiso): eventlinks komen uit de sitemap (`sitemap`, `sitemap_pattern`, `last_files`), de data uit de
  eventpagina's (JSON-LD of de ingebedde React Server Components-payload). Zwaar bij de eerste run, daarna incrementeel via de cache.
- `embedded` leest ook JSON die in HTML-attributen zit (Tolhuistuin: Vue-prop `:all-items`). Met `only_genres: [Muziek, Clubnacht]`
  filter je een gemengd programma (theater, workshops) weg.

Alle opties staan bovenin `venues.yaml`. Tijdelijk uitzetten: `type: disabled`.

## Zelf draaien

```bash
pip install -r requirements.txt
python fetch.py            # alle podia
python fetch.py Hedon EKKO # alleen deze
python build.py
open docs/index.html
python tests/test_offline.py
```

## Netheid

Eén run per dag, één request per pagina, een herkenbare User-Agent en respect voor `crawl_delay`
waar een podium daarom vraagt. Podia die geautomatiseerd ophalen expliciet weren, staan op `disabled`.
