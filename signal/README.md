# Signal — paikallinen tuotetutka MVP

## Käynnistä

Vaatii Python 3.9+ ja selaimen. Ei asennettavia paketteja.

```sh
cd outputs/product-radar
python3 server.py
```

Avaa http://127.0.0.1:8765. `PORT` vaihtaa portin. Tiedot säilyvät `radar.sqlite3`-tiedostossa; `RADAR_DB` vaihtaa tietokannan sijainnin. Palvelin kuuntelee vain omaa konetta. Käyttöliittymä toimii ilman ulkoista fonttia, jos Google Fonts ei ole saatavilla.

## Käyttö

1. Lisää nimi / kuvan kuvaus, tuotesivun URL, kuvan URL, kilpailija, niche ja hakusanat.
2. Avaa Validation: TikTok-hakulinkki auttaa etsimään videoita. Tarkista tuotteen väri, rakenne ja ominaisuudet kuvasta ja videosta. Nimi/hakusanat tunnistavat hakuehdokkaat; visuaalinen vastaavuus on MVP:ssä ihmisen varmentama. Tyhjä niche luokitellaan nimi-/hakusanasäännöillä; tarkista ja korjaa luokka tarvittaessa.
3. Tuo video JSON-muodossa. Tallenna sama URL myöhemmin uudella `observed_at`-ajalla kasvun laskemiseksi. Syötä puuttuvat mittarit `null`-arvoina.
4. `match: "confirmed"` merkitsee ihmisen tai luotetun adapterin tarkistamaa saman tuotteen osumaa. Se ei todista alkuperäistä julkaisijaa. Varhaisin löydetty julkaisu ei yksin ole alkuperäinen.
5. Product Feed ja Viral Videos järjestävät pisteiden perusteella. Haku ja niche-rajaus suodattavat tuotteita. Trends kokoaa nichet ja ajaa siementuotteiden hakua.

Esimerkkipainike luo synteettisen kaksivärisen turkoosin topin ja housujen casen (91k tykkäystä, 6 päivän ikä). Aiempi 62k mittaus, muut luvut ja kommentit ovat keksittyjä demonstraatioarvoja. Linkki on haku, ei oikea video. Esimerkkimerkinnät pysyvät näkyvissä.

## Automaattinen validointi ja winner-signaali

Uusi käsin lisätty tai `/api/feed`-rajapintaan saapuva tuote tallentuu atomisesti yhdessä deduplikointitunnuksen ja pysyvän validointityön kanssa. Taustatyö tarkistaa Shopify-tuotesivun julkisen `.js`-vastauksen ja käynnistää TikTok-adapterin haun. Näkyvät tilat: jonossa, validoidaan, tarkistettu, osittainen tulos, videodata odottaa kytkentää, haku epäonnistui.

Työjono palautuu käynnistyksessä. Tavallinen jatkopäivitys on 6 tunnin välein (`RADAR_REFRESH_SECONDS`, vähintään 60 sekuntia); provider-virheitä yritetään uudelleen kasvavalla viiveellä. Socket-aikakatkaisut eivät takaa työn enimmäiskestoa. Paikallinen worker käsittelee yhden tuotteen kerrallaan. Kun tuotteita tulee paljon, siirrä worker erilliseen jonopalveluun.

Saatavuus: `in_stock`, `partial`, `sold_out`, `unknown`. `sold_out` tarkoittaa ettei tuotetta tarjota ostettavaksi tarkistushetkellä; se ei todista myyntimäärää tai syytä. HTTP-virhe tai puuttuva saatavuus ei muutu sold out -havainnoksi. Lähde ja tarkistusaika näkyvät käyttöliittymässä. Haku sallii vain julkiset HTTPS-osoitteet, tarkistaa uudelleenohjaukset ja lukitsee yhteyden tarkistettuun IP-osoitteeseen. Manuaalinen vahvistus vaatii lähde-URL:n ja havaintoajan.

Product Feedin winner-score 0–100 käyttää vain samaan tuotteeseen varmennettuja (`match: confirmed`), oikeita, viimeisen 24 tunnin aikana mitattuja videoita. Esimerkit ja hylätyt/vahvistamattomat osumat eivät tue oikean tuotteen arviota:

- Tuoreus 0–25: `25 * max(0, 1 - ikä_päivinä/14)`.
- Mitattu tykkäyskasvu 0–30: kahden viimeisen havainnon erotus/tunnit, logaritminen asteikko, täysi paino 3000 tykkäystä/tunti.
- Ostoaikomus 0–25: ostoaikomuksen osuus kommenttiotoksesta; alle 30 kommentin otoksen painoa pienennetään suhteessa otoskokoon.
- Engagement 0–15: `(tykkäykset + 2*jaot)/näyttökerrat`, täysi paino 15 prosentissa.
- Saatavuus 0–5: tuore sold out -havainto antaa painoa vain jos ostoaikomus on vähintään 15 %, kommentteja vähintään 30 ja kasvu positiivinen.
- Kielteinen palaute vähentää enintään 15 pistettä kommenttiotoksen kielteisten kommenttien osuuden perusteella.

Kasvu, tuoreus ja engagement perustuvat parhaaseen kelvolliseen videoon; ostoaikomus koostuu kelvollisten videoiden otoksista. Otoksia ei ole mahdollista deduplikoida kommentoijittain, koska tunnisteita ei tallenneta. "Vahva signaali" vaatii lisäksi vähintään kaksi varmennettua videota, mitatun kasvun, vähintään 30 kommenttia ja näyttökerrat. Muut tulokset näytetään lupaavina, heikkoina tai puuttuvana näyttönä. Myynnin prosenttitodennäköisyys on `null`: kalibroitu ennuste vaatii tuotteiden toteutuneet myynti-/kate-/mainostulokset ja jälkikäteisen vertailun.

Videotaulukon score on erillinen diagnostinen engagement-score (tuoreus /30, kasvu /30, ostoaikomus /25, engagement /15), myös vahvistamattomille ehdokkaille. Varsinainen winner-arvio on erillisessä tuotepaneelissa. Trendivaihe: enintään 3 päivää varhainen havainto, 4–7 päivää aktiivinen ikkuna, 8–14 päivää myöhäinen, yli 14 päivää vanha. Tämä ei ennusta tarkkaa kuolinpäivää.

Puuttuva data ei muutu nollakasvuksi; se näkyy tuntemattomana ja rajoittaa pisteitä. FI/EN-avainsanasäännöt poimivat esimerkiksi "haluun tän", "mistä mä voin ostaa", "I want this" ja "where can I buy". Kielteisiä avainsanoja ja muutamia kieltorakenteita käsitellään ensin. Säännöt eivät ymmärrä kaikkia konteksteja tai ironiaa. Tarkista lähde, otos ja osuma. Sourcaus, saatavuus omalta toimittajalta, kate, kilpailu ja mainostulokset jäävät käyttäjän arvioitaviksi.

## Nykyinen Shopify-monitori

Integraatio on toteutettu repositoryyn `Aatskux/shopify-monitor`: `signal_bridge.py` liitetään `check_all`-funktion uuden tuotteen kohtaan. JSON-outbox säilyttää toimittamattomat launchit ja Actions tallentaa sen nykyisen tilan mukana. Ilman `SIGNAL_URL`- ja `SIGNAL_FEED_TOKEN`-asetuksia uusi lähetys on pois käytöstä. Ensimmäinen baseline ja dry-run eivät lähetä launchia. Katso repositoryn `SIGNAL.md`.

Paikalliseen listaan on tuotu 20 uusinta tuotetta repositoryn image_hashes.json-katalogista. Nämä ovat oikeita tallennettuja tuotteita, eivät todennettuja TikTok-voittajia. Julkaisuaika on katalogin julkaisutieto, ei väite siitä, että tuote havaittiin juuri nyt. Voit tehdä rajatun historian tuonnin uudelleen:

```sh
python3 import_monitor.py /path/to/shopify-monitor/image_hashes.json --limit 20
```

GitHub Actionsin 5 minuutin schedule voi viivästyä. Actions ei pääse paikallisen koneesi `127.0.0.1`-osoitteeseen: aja monitori samalla koneella tai julkaise suojattu HTTPS-feed-vastaanotin. GitHub-muutos on tarkistettava ja yhdistettävä, ja secrets määritettävä, ennen kuin nykyinen tuotantobotti lähettää Signaliin.

## Discord / data feed

Discordin tavallinen incoming webhook julkaisee viestejä Discordiin, eikä lue kanavan viestejä. Käytä nykyisen launch-monitorin rinnakkaista HTTP-lähetystä, tai kanavaa lukevaa Discord Gateway -bottia. Botin tarvitsemat oikeudet ja Message Content Intent riippuvat siitä, mitä viesteistä luetaan. Älä ota käyttöön koko palvelimen lukuoikeuksia tarpeettomasti.

Käynnistä vastaanotin tokenilla:

```sh
RADAR_FEED_TOKEN='replace-with-long-random-secret' python3 server.py
```

Lähetä `POST /api/feed`, `Content-Type: application/json`, `Authorization: Bearer <token>`:

```json
{
  "event_id": "discord-message-unique-id",
  "product": {
    "name": "Turquoise two tone co-ord",
    "url": "https://example.com/products/co-ord",
    "image": "https://example.com/product.jpg",
    "competitor": "Competitor boutique",
    "niche": "Fashion",
    "keywords": "turquoise two tone top matching pants"
  }
}
```

Saman `event_id`:n uudelleenlähetys palauttaa `duplicate` eikä luo uutta tuotetta. Sovita Discord-embedin title/url/image tuotteeksi lähettäjän puolella. Muista säilyttää message ID ja kilpailijan nimi. Tallenna token ympäristöön; älä lähetä sitä selaimeen. Vastaanotin hylkää feed-pyynnöt, jos tokenia ei ole määritetty. Sama tuote-URL yhdistyy myös eri tapahtumatunnuksilla; URL:n query-parametrit ohitetaan. Vanhoja pre-upgrade-recordeja ei migroida automaattisesti canonical-URL-deduplikointiin.

## TikTok-data tuotantoon

MVP ei ohita TikTokin kirjautumista eikä sisällä koko TikTokin scraperia. Live-haku vaatii määritetyn datatoimittajan. TikTokin Display API on käyttäjän luvittaman tilin dataa varten, ei yleinen kilpailijatuotteiden hakukone. Research API vaatii hyväksytyn pääsyn ja ehtojen mukaisen käyttötarkoituksen: älä oleta kaupallisen dropshipping-käytön olevan sallittua. Tarkista nykyiset käyttöehdot ja datatoimittajan oikeudet.

Adapteri käyttää ympäristömuuttujia `TIKTOK_PROVIDER_URL` ja `TIKTOK_PROVIDER_TOKEN`. `Hae data-adapterilla` lähettää määritettyyn palveluun POST-JSON:n:

```json
{"query":"turquoise two tone top matching pants","image_url":"https://example.com/product.jpg","product_name":"Turquoise set","product_url":"https://example.com/products/set","recent_days":14,"include_comments":true,"known_video_urls":[],"limit":20}
```

Provider suorittaa nimi-/hashtag-haun, halutessaan kuvaembedding-haun, palauttaa ehdokkaat ja niiden todennetut mittarit. Vastaus:

```json
{"videos":[{
  "url":"https://www.tiktok.com/@creator/video/REAL_ID",
  "published_at":"2026-09-25T12:00:00Z",
  "observed_at":"2026-10-01T12:00:00Z",
  "likes":91000,"comments_count":null,"shares":null,"views":null,
  "comments":["Where can I buy this?"],
  "source":"provider-name","match":"unverified"
}]}
```

Oikean casen hakua ei ole suoritettu eikä alkuperäistä turkoosia asusettiä varmennettu. Kuva on tällä hetkellä viite ja adapterin syöte, ei paikallinen kuvantunnistus. Niche päätellään avainsanoista tai valitaan käsin; tarkempi luokittelu voidaan lisätä adapteriin.

Signal päivittää adapterihaun automaattisesti oletuksena 6 tunnin välein. Adapterin on päivitettävä myös `known_video_urls`-listan videot. Erillisestä datatyöstä voi lähettää mittaukset `POST /api/videos` lisäämällä `product_id`. Säilytä todelliset havaintoajat, julkaisuajat, null-arvot ja lähde. Provider-haun aikakatkaisu on 25 s ja kerralla enintään 20 videota; yksittäisten havaintojen virheet palautetaan käyttöliittymään.

## Discovery-pohja

`POST /api/discover` hakee nykyisten kymmenen korkeimman pisteen tuotteen hakusanoilla. Ilman adapteria se palauttaa hakulinkit. Tämä on siemenhaku, ei itsenäinen markkinan seuranta. Laajenna workeriksi: niche-/hashtag-siementen jono → provider → ehdokkaiden yhdistäminen → ihmisen osumatarkistus → toistuvat mittaukset → tuoreus/kasvupriorisointi. Lisää tulevaisuudessa samankaltaisuusmalli, automaattinen niche, paginointi, työstöjono ja hakuhistoria.

## Tuotantovalmiuden rajat

Paikallinen MVP sisältää SQLite-tallennuksen, ristikkäisten selain-originien POST-eston, feed-tokenin, URL-skeeman tarkistuksen ja 2 MB syöterajan. Se ei sisällä käyttäjäkirjautumista. Manuaaliset API:t ovat paikallisesti avoimia. Älä julkaise palvelinta sellaisenaan: lisää kirjautuminen ja kaikkiin kirjoitusrajapintoihin tunnistus, HTTPS/reverse proxy, sallittu origin/host, rate limit, worker-jono, provider-URL:n sallittujen hostien lista, tietokantavarmistus sekä kommenttien säilytys-/poistopolitiikka. Älä tallenna tarpeettomia kommentoijien tunnisteita. Linkitetyt kuvat latautuvat kolmannen osapuolen palvelimelta.

## Tarkistus

```sh
PYTHONPYCACHEPREFIX=/tmp/radar-pycache python3 -m unittest discover -s tests -v
node --check static/app.js
```

18 testiä kattavat myös HTTP-feed→työjono→validointi-polun testiproviderilla, tokenin, saatavuussäännöt, retry-viiveen ja winner-näytön rajauksen. Live-provider ja Discord-botti tarvitsevat omat tunnukset ja integraatiotestauksen.

Lähteet (tarkistettu 1.10.2026):
- https://developers.tiktok.com/products/research-api/
- https://developers.tiktok.com/docs/en/tiktok-api-v2-video-object
- https://github.com/discord/discord-api-docs/blob/main/developers/platform/webhooks.mdx

- https://shopify.dev/docs/api/ajax/reference/product
- https://developers.tiktok.com/docs/en/research-api-faq
