# Signal-tuotetutka

Monitori voi lähettää uuden tuotteen samassa kierroksessa sekä Discordiin että Signaliin. Signal tarkistaa Shopify-saatavuuden ja käynnistää videohaun taustalla. `SIGNAL_URL` ja `SIGNAL_FEED_TOKEN` ottavat integraation käyttöön; ilman niitä nykyinen toiminta jatkuu ennallaan.

## Paikallinen käyttö

Aja Signal samalla koneella ja aseta sille `RADAR_FEED_TOKEN`. Monitorissa aseta `SIGNAL_URL=http://127.0.0.1:8765` ja sama arvo `SIGNAL_FEED_TOKEN`-muuttujaan. Älä laita tokenia koodiin tai repositoryyn.

## GitHub Actions

Lisää repository secrets:

- `SIGNAL_URL`: verkosta saavutettava Signalin HTTPS-osoite.
- `SIGNAL_FEED_TOKEN`: sama salaisuus kuin Signalin `RADAR_FEED_TOKEN`.

GitHub Actions ei pääse koneesi 127.0.0.1-osoitteeseen. Paikallista Signalia käytettäessä aja monitori samalla koneella. Actions-käyttö edellyttää turvallisesti julkaistua Signalin feed-vastaanotinta; älä julkaise paikallisen MVP:n avoimia manuaalisia kirjoitusrajapintoja. Actionsin 5 minuutin schedule ei takaa sekuntitason havaintoa; Signal-validointi alkaa, kun monitorin havaitsema launch vastaanotetaan.

`signal_outbox.json` säilyttää toimittamattomat launchit ja tallentuu Actionsin tilakommitissa. Se sisältää tuotteen julkisen nimen, URL:n, kuvan ja kilpailijan, ei tokenia. Jokaisella launchilla on vakaa tapahtumatunnus. Signal kuittaa ja deduplikoi uudelleenlähetykset; onnistuneet poistetaan jonosta. Vastaanottimen ollessa poissa päältä viesti jää jonoon ja sitä yritetään seuraavalla kierroksella. Signalin verkkopyynnöt käyttävät enintään 30 sekunnin budjettia kierroksella ja enintään 3 sekunnin socket-aikakatkaisua per pyyntö.

Ensimmäinen uuden kaupan baseline ja `--dry-run` eivät lähetä tuotteita Signaliin. Palvelutuotteet kuten toimitusvakuutus ohitetaan. Integraatio käynnistyy uusista havainnoista; se ei automaattisesti lähetä repositoryn vanhaa seen.json-katalogia.

Live-videoiden löytämiseen Signal tarvitsee `TIKTOK_PROVIDER_URL`-adapterin ja tarvittaessa `TIKTOK_PROVIDER_TOKEN`-tunnuksen. GitHub-kytkentä ei yksin tuo TikTok-dataa. Winner-pisteet eivät ole myynnin prosenttitodennäköisyys; sold out on tukisignaali, ei todiste myyntimäärästä.

Testit: `python -m unittest -v test_signal_bridge.py test_shopify_monitor.py`. Testit eivät postaa oikeisiin palveluihin.
