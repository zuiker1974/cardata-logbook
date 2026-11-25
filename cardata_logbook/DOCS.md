# CarData Logbook (Home Assistant -lisäosa)

Automaattinen ajopäiväkirja autoille, jotka ovat Home Assistantissa **BMW CarData** -integraation
([bmw-cardata-ha](https://github.com/kvanbiesen/bmw-cardata-ha)) kautta. Lisäosa löytää autot itse,
tunnistaa ajot ja tekee niistä Verohallinnon vaatimukset täyttävät PDF- ja Excel-raportit.

- **Ei omaa yhteyttä BMW:lle.** BMW sallii vain yhden CarData-streamin per tili, ja sen pitää jo HA:n
  CarData-integraatio. Lisäosa vain lukee HA:n tiloja ja entiteettirekisteriä (WebSocket), eikä se muuta
  mitään HA:ssa. Olemassa olevat automaatiot toimivat ennallaan.
- **Autot löytyvät automaattisesti** VIN:n perusteella. Käyttövoima (BEV, PHEV, ICE) tulee CarDatasta.
- **Autot pidetään aina erillään:** oma tietokanta, omat raportit, omat kansiot ja omat
  HA-sensorit per auto. Yhdistää voi vain saman auton matkoja.
- **Verottajaa varten:** mittarilukemien jatkuvuuden tarkistus, muutosloki jota ei voi muokata,
  kuukausien lukitus ja lukitun raportin SHA-256-tiiviste.
- **Raportit:** PDF (kuukausi ja vuosi), Excel-vuosityökirja kommenttisarakkeella ja CSV.
  Tulosteet voi rajata: kaikki ajot, vain työajot, vain yksityisajot tai vain työmatkat.
- **Kulut:** lataukset (kWh) ja tankkaukset (l), vain määrät.
- **Pilvitallennus (valinnainen):** lisäosa kirjoittaa kansioon `/share/ajopaivakirja/`, ja esimerkiksi
  **Rclone Backup** -lisäosa voi viedä sen Google Driveen. Omaa Google-kirjautumista ei tarvita.

Käyttöliittymä ja raportit ovat toistaiseksi suomeksi.

---

## 1. Asennus

1. Avaa HA: **Asetukset → Lisäosat → Lisäosakauppa**.
2. Kolmen pisteen valikko (⋮) → **Repositoriot** → lisää osoite
   `https://github.com/zuiker1974/cardata-logbook` → **Lisää**.
3. Päivitä sivu. Lisäosakaupan alalaitaan ilmestyy **CarData Logbook**. Avaa se ja paina **Asenna**.
4. **Käynnistä**. Ota halutessasi käyttöön **Näytä sivupalkissa**, jolloin ajopäiväkirja näkyy
   HA:n sivupalkissa nimellä *Ajopäiväkirja*.
5. Avaa ajopäiväkirjan **Autot**-sivu ja lisää autosi (kohta 2).

Lisäosa käyttää Supervisorin omaa tunnusta, joten pitkäikäistä tunnusta ei tarvita. Jos Mosquitto
MQTT -välittäjä on asennettu, lisäosa luo autokohtaiset sensorit automaattisesti.

## 2. Autot

Autot hallitaan ajopäiväkirjan **Autot**-sivulla (yläpalkki). Lisäosa löytää autot itse Home
Assistantin BMW CarData -integraatiosta: jokaisen CarData-entiteetin tunnisteessa on auton VIN, joten
oikeat entiteetit löytyvät nimistä riippumatta (myös latausentiteetit, joiden nimessä ei ole auton nimeä).
Metadatasta tulevat merkki, malli ja käyttövoima. Lisäosa ei avaa omaa yhteyttä BMW:lle.

**Auton lisääminen:** Autot-sivun *Löydetyt autot* -listassa → *Lisää* → tarkista tunniste, syötä
rekisteritunnus ja kuljettaja → *Lisää auto* → *Käynnistä uudelleen nyt*. Tarkista lopuksi
Asetukset-sivulta, että entiteeteissä on ✓.

| Kenttä | Merkitys |
|---|---|
| Tunniste | Auton pysyvä tunniste (esim. `530e`, `ix3`). Tietokanta ja raporttikansiot nimetään sen mukaan, eikä sitä voi muuttaa. |
| Rekisteritunnus | Näkyy raporteissa ja tiedostonimissä. |
| Merkki, malli, VIN | Täytetään CarDatasta. VIN yhdistää auton sen entiteetteihin. |
| Käyttövoima | Täyssähkö (BEV), ladattava hybridi (PHEV) tai polttomoottori (ICE, myös mild hybrid). Määrää, seurataanko latausta, polttoainetta vai molempia. Ehdotus tulee CarDatasta. |
| Polttoaine | Bensiini tai diesel (PHEV ja ICE). |
| Lataustauko | Kuinka kauan lataus kesken matkan saa kestää ennen kuin matka katsotaan päättyneeksi. 0 = pois. Kotilataus ei koskaan ole lataustauko. |
| Arvio kWh / varaus-% | Muualla kuin kotona ladatun energian arvio varauksen noususta (530e 0,12). 0 = ei arviota. |
| Ladataan kotilaturilla | Käytetäänkö yhteistä kotilaturin energiamittaria. |
| Etsi entiteetit automaattisesti | Pois päältä vain, jos auto ei ole CarDatassa ja kaikki entiteetit syötetään käsin. |
| Entiteetit | Tyhjä kenttä = haetaan automaattisesti. Käsin syötetty entiteetti ohittaa löydetyn. Pakolliset: sijainti ja mittarilukema. |

**Kotilaturi** asetetaan kerran Autot-sivulla (esim. laturin istuntoenergia, Wh tai kWh). Jos sama laturi
on usealla autolla, mitattu energia kirjataan autolle, jonka varaus nousi kytkennän aikana.

Muut kuin CarData-autot toimivat, kun entiteetit syötetään käsin.

Jos pakollinen entiteetti puuttuu, auto näkyy tilassa *Ei konfiguroitu*, eikä se vaikuta toisen auton
kirjaukseen.

**Siirto toisesta asennuksesta:** kirjoita vanhassa asennuksessa *Asetukset → Kirjoita siirtopaketti*
ja pysäytä vanha lisäosa. Uusi asennus tuo paketin (`/share/ajopaivakirja/.siirto/`) ensimmäisellä
käynnistyksellä, jos sen omat tiedot ovat tyhjät.

Jos siirtopakettia ei ole, uusi asennus palauttaa autot raporttikansion tietokantavarmuuskopioista
(`<auto>/varmuuskopiot/`, kirjoitetaan jokaisen matkan jälkeen ja kerran päivässä). Matkat, kulut,
lukitukset ja muutosloki palautuvat. Tarkista palautuksen jälkeen Autot-sivulta VIN ja kotilaturi.

### Muut asetukset

| Asetus | Oletus | Merkitys |
|---|---|---|
| `trip_end_timeout_s` | 600 | Matka päättyy, kun auto on ollut paikallaan näin kauan. |
| `trip_start_distance_m` | 100 | Sijainnin siirtymä, jonka jälkeen auto katsotaan liikkuneeksi. |
| `ghost_motion_max_s` | 90 | Lyhyet "liikkeessä"-jaksot ilman todellista siirtymää ohitetaan (esim. BMW 530e lähettää pysäköinnin jälkeen usein haamuliikkeen). |
| `home_zones` | `zone.home` | Vyöhykkeet, joilla lataus ei ole lataustauko. |
| `stream_status_entity` | tyhjä = haetaan automaattisesti | Kun CarData-yhteys on poikki, matkaa ei päätetä aikakatkaisulla. |
| `backfill_max_hours` | 24 | Yhteyskatkon jälkeen puuttuva jakso haetaan HA:n historiasta enintään näin pitkältä ajalta. |
| `odometer_gap_tolerance_km` | 1 | Mittarilukemien sallittu ero peräkkäisten matkojen välillä. |
| `auto_lock_after_days` | 15 | Kuukausi lukitaan automaattisesti, kun sen päättymisestä on kulunut näin monta päivää ja tarkistukset ovat kunnossa (0 = vain käsin). |
| `commute_counts_as_private` | true | "Vain yksityisajot" -tuloste sisältää työmatkat. |
| `archive_filtered` | `[business]` | Lukittaessa arkistoidaan myös työajojen osaraportti. |
| `month_folder_format` | `%m` | Kuukausikansion nimi (`11`). |
| `report_xlsx`, `report_csv` | true, false | Kirjoitetaanko Excel ja CSV raporttikansioon. |
| `refuel_min_l` | 3 | Tankin litramäärän nousu, joka kirjataan tankkaukseksi. |

## 3. Käyttö

Ajopäiväkirja aukeaa HA:n sivupalkista. Etusivulla on autot korttina, ja jokaisella autolla on oma
näkymänsä (`/v/<tunniste>`). Kaikkien autojen yhteistä näkymää ei ole.

- **Matkat:** suodata vuoden, kuukauden ja ajotyypin mukaan. Ajotyyppi, tarkoitus ja kommentti
  tallentuvat heti. Työajolle tarkoitus on pakollinen ennen jakson lukitusta.
- **Yhdistäminen:** valitse peräkkäiset matkapätkät ja paina *Yhdistä valitut matkat*. *Pura* palauttaa ne.
- **Mittariaukot:** jos seuraava matka alkaa suuremmalla lukemalla kuin edellinen päättyi, listassa
  näkyy varoitusrivi. Siitä voi lisätä puuttuvan ajon tai kuitata aukon perustellusti.
- **Korjaukset:** matkan *tiedot*-sivulla voi korjata paikan, ajan tai mittarilukeman. Korjaus vaatii
  perustelun, alkuperäinen arvo säilyy ja muutos näkyy muutoshistoriassa ja raportissa.
- **Kulut:** auton lataukset ja tankkaukset (vain määrät) sekä kuukauden summat. Yksikköhinnan
  (€/kWh tai €/l) voi lisätä riville, mutta se ei ole pakollinen, eikä euromääriä lasketa. Väärin
  tunnistetun rivin määrän voi korjata tai poistaa laskennasta perustellusti, ja puuttuvan latauksen
  (esim. julkinen laturi) voi lisätä käsin.
- **Raportit ja lukitus:** lataa PDF tai Excel selaimella, tuo kommentoitu Excel ja lukitse kuukausi.

### Kululaskenta

- **Lataus:** kytkentä alkaa, kun auton latausmenetelmä vaihtuu (esim. `AC_TYPE2PLUG`), ja päättyy, kun se
  palaa tilaan `NOCHARGING` tai auto lähtee liikkeelle. Kotona määrä on laturin oman mittarin istuntolukema
  (energia seinästä, sisältää lataushäviöt). BMW ilmoittaa kytkennän joskus jopa 40 min myöhässä, joten
  laturin juuri ennen kytkentätietoa alkanut istunto lasketaan mukaan. Muualla ladattu energia arvioidaan
  varaustason noususta (`charge_kwh_per_pct`), ja rivi merkitään arvioksi.
  BMW ei aina ilmoita kaapelin irrotusta, jolloin latausmenetelmä jää päälle koko päiväksi. Kun auto
  on lähtenyt liikkeelle, seuraava lataus (esim. työpaikalla) tunnistetaan auton lataustilasta
  (`CHARGINGACTIVE`).
- **Tankkaus (PHEV ja ICE):** tankin litramäärän nousu vähintään `refuel_min_l` litraa. Auto ilmoittaa vain
  kokonaisia litroja, joten tarkkuus on noin ±1 l. Aika ja paikka ovat se hetki, jolloin auto ilmoitti
  nousun.
- **Kulunut polttoaine** on kuukauden aikana tankin lukemien laskujen summa.
- Kuukausi-PDF:ssä on Kulut-osio, vuosi-PDF:ssä kuukausittainen yhteenveto ja Excelissä Kulut-välilehti,
  jossa Yksikköhinta ja Kommentti ovat muokattavia ja tuodaan takaisin Kulu-ID:n perusteella.
  Ajotyyppikohtaisissa osaraporteissa kuluja ei näytetä, koska niitä ei voi jakaa ajoille.
- Lukitulla jaksolla määrää ei voi muuttaa, mutta yksikköhinnan ja kommentin voi lisätä.

### Lukitus

Lukittaessa tarkistetaan mittarijatkuvuus ja työajojen tarkoitukset, tuodaan odottavat Excelit ja
tallennetaan kuukauden PDF muuttumattomana kansioon `arkisto/`. Tiedoston SHA-256 tallennetaan
tietokantaan ja viereiseen `.sha256`-tiedostoon, ja PDF:n alatunnisteessa on raportin sisällön
SHA-256. Lukitun kuukauden matkoja ei voi muuttaa, mutta jälkikommentin voi lisätä (se merkitään
"lisätty lukituksen jälkeen"). Lukituksen avaaminen vaatii perustelun, ja uusi lukitus tuottaa uuden
arkistoversion (`_v2`) vanhaa poistamatta.

## 4. Kommentoidun Excelin palautus

Excel-työkirjassa on kuukausivälilehdet (01–12), joissa **Kommentti**, **Ajotyyppi** ja **Tarkoitus**
ovat muokattavissa (keltainen otsikko). Muut sarakkeet tulevat autosta ja ovat lukittuja. Piilotettu
Matka-ID-sarake yhdistää rivit takaisin ajopäiväkirjaan.

Kaksi tapaa palauttaa:

1. **Selaimella (suositus):** *Raportit ja lukitus → Tuo Excel*. Tulos näkyy heti.
2. **Driven kautta:** tallenna muokattu tiedosto Drivessa auton kansioon
   `Ajopaivakirja/<auto>_<rekisteritunnus>/tuonti/`. Rclone Backup hakee sen 15 minuutin välein,
   ajopäiväkirja tuo sen ja siirtää käsitellyn tiedoston kansioon `tuonnit/`.

> **Älä muokkaa Drivessa olevia raporttitiedostoja paikallaan.** Varmuuskopiojob kopioi
> paikallisen version Driveen aina, kun se muuttuu, ja ylikirjoittaa Drivessa tehdyt muutokset.

Tuonnissa:
- toisen auton Excel hylätään (auton tunniste ja VIN tarkistetaan);
- vain Excelissä muuttuneet kentät tuodaan; jos sama kenttä on muuttunut myös selaimessa, kommentit
  yhdistetään ja ajotyypin tai tarkoituksen ristiriita jää ratkaistavaksi Raportit-sivulle;
- lukittuja mittaussarakkeita ei koskaan tuoda, vaikka taulukon suojaus olisi poistettu;
- lukitulle kuukaudelle tuodaan vain kommentit (jälkikommentteina).

## 5. Vienti Google Driveen Rclone Backupilla (valinnainen)

1. Asenna ja käynnistä CarData Logbook kerran, jotta kansio `/share/ajopaivakirja/` autokansioineen
   syntyy. Tarkista se esim. Samba-jaosta tai File editorista.
2. Avaa HA: **Asetukset → Lisäosat → Rclone Backup → Määritykset**.
3. Avaa kolmen pisteen valikosta (⋮) **Muokkaa YAML-muodossa**.
4. Lisää `jobs:`-listan loppuun kaksi uutta jobia. Älä muuta olemassa olevia jobeja. Sisennyksen on
   oltava sama kuin muissa jobeissa (jokainen job alkaa `- name:` samalla tasolla), ja remoten nimi
   (tässä `google_drive:`) on sama kuin Rclone-asetuksissasi.

   ```yaml
     - name: Ajopaivakirja                    # raportit ja varmuuskopiot Driveen
       schedule: 20 3 * * *                   # päivittäin 03:20, tietokannan varmuuskopion jälkeen
       command: copy                          # copy, ei sync: Drivesta ei koskaan poisteta mitään
       source: /share/ajopaivakirja
       destination: google_drive:Ajopaivakirja
       exclude:
         - "*/tuonti/**"
         - "*.tmp"

     - name: Ajopaivakirja tuonti             # Driven tuonti-kansiot takaisin HA:han
       schedule: "*/15 * * * *"               # 15 minuutin välein
       command: move                          # siirtää tiedoston pois Drivesta, kun se on haettu
       source: google_drive:Ajopaivakirja
       destination: /share/ajopaivakirja
       include:
         - "*/tuonti/*.xlsx"
   ```

5. Paina **Tallenna** ja käynnistä Rclone Backup uudelleen (*Info → Käynnistä uudelleen*). Uudet jobit
   tulevat voimaan vasta uudelleenkäynnistyksen jälkeen.
6. **Testaa heti:** avaa *Loki*-välilehti ja tarkista, että käynnistyksessä näkyvät uudet jobit
   ilman virheitä. Kansio `Ajopaivakirja` ilmestyy Driveen ensimmäisellä ajolla (03:20). Jos haluat
   testata heti, vaihda *Ajopaivakirja*-jobin `schedule` hetkeksi muutaman minuutin päähän, tallenna,
   käynnistä uudelleen ja palauta aikataulu testin jälkeen.
7. **Testaa tuonti:** lataa auton Excel selaimella, lisää kommentti, tallenna tiedosto Drivessa kansioon
   `Ajopaivakirja/<auto>/tuonti/` ja odota enintään 15 minuuttia. Tiedosto katoaa Driven
   tuonti-kansiosta, ja kommentti näkyy ajopäiväkirjassa. Seuraavan varmuuskopioajon jälkeen käsitelty
   tiedosto näkyy Drivessa kansiossa `tuonnit/`.
8. **Jos siirto ei toimi:** tarkista Rclone Backupin loki. Yleisin syy on vanhentunut
   `google_drive`-remoten token. Ajopäiväkirjan tiedostot odottavat sillä välin `/share`-kansiossa eivätkä katoa.

Raportit voi aina ladata myös suoraan selaimella, joten Driveen ei tarvitse odottaa.

### Kansiorakenne

```
/share/ajopaivakirja/                     →  google_drive:Ajopaivakirja/
├── auto1_ABC-123/
│   ├── 2026/
│   │   ├── Ajopaivakirja_auto1_ABC-123_2026.xlsx
│   │   ├── Ajopaivakirja_auto1_ABC-123_2026_vuosiraportti.pdf
│   │   └── 11/
│   │       ├── Ajopaivakirja_auto1_ABC-123_2026_11.pdf
│   │       ├── Ajopaivakirja_auto1_ABC-123_2026_11_tyoajot.pdf
│   │       └── Ajopaivakirja_auto1_ABC-123_2026_11_yksityisajot.pdf
│   ├── arkisto/2026/                      (lukitut kuukaudet, ei koskaan ylikirjoiteta)
│   ├── varmuuskopiot/                     (tietokanta kerran päivässä ja jokaisen matkan jälkeen)
│   ├── tuonti/                            (Drivesta tuotavat Excelit)
│   └── tuonnit/                           (käsitellyt ja hylätyt tuonnit)
└── auto2_XYZ-789/
    └── (sama rakenne)
```

Raportit päivittyvät noin 5 minuutin kuluttua matkan päättymisestä tai muutoksesta.

## 6. Home Assistant -sensorit

Jos MQTT on käytössä, jokaiselle autolle luodaan oma laite (*CarData Logbook &lt;tunniste&gt;*)
ja sensorit `sensor.bmw_logbook_<auto>_status`, `_last_trip_distance`, `_last_trip_route`,
`_month_business_km`, `_month_private_km`, `_month_charged_kwh`, `_last_export` ja `_last_import`
(PHEV:lle myös `_month_refuelled_l` ja `_month_fuel_used_l`). Nämä ovat uusia entiteettejä
eivätkä muuta olemassa olevia.

## 7. Miten ajot tunnistetaan

- Ajo alkaa, kun auto oikeasti liikkuu: sijainti siirtyy yli 100 m tai mittarilukema kasvaa. Alkuajaksi
  otetaan liiketilan "on"-hetki, ja alkupaikaksi ja -lukemaksi viimeisin pysäköintipaikka ja -lukema.
- Ajo päättyy, kun auto on ollut paikallaan 10 minuuttia. Loppuajaksi otetaan pysähtymishetki.
- CarData kirjoittaa saman sijainnin HA:hon noin viisi kertaa: toistot ohitetaan.
- Pysäköinnin jälkeinen "haamuliike", jonka aikana auto ei liiku, ohitetaan, eikä se
  siirrä matkan loppuaikaa.
- Lataus kesken matkan muualla kuin kotona pitää matkan koossa lataustauon ajan, ja tauko kirjataan
  raporttiin (esim. "Lataustauko 35 min, Ionity Esimerkkilä").
- Osoitteet haetaan OpenStreetMapin Nominatim-palvelusta (enintään 1 haku sekunnissa, välimuistilla).

