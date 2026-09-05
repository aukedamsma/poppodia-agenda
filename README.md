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
| Het Podium, So What!, GIGANT | alles, als Cloudflare ook de browser-headers weigert | botblokkade | dan alleen via de Stager-shop (So What!, GIGANT hebben er geen) |
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
