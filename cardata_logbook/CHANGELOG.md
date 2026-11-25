# Muutosloki

## 1.0.2
- Korjaus: lataus muualla kuin kotona jäi kirjaamatta, jos auto lähti kotoa ilman irrotustietoa
  (latausmenetelmä pysyi `AC_TYPE2PLUG`). Lataus tunnistetaan nyt myös auton lataustilasta.
- Päivityksen jälkeen lisäosa hakee kerran Home Assistantin historiasta (`backfill_max_hours`, oletus 24 h)
  lataukset, jotka puuttuvat kirjanpidosta. Jo kirjattuihin latauksiin ei kosketa.

## 1.0.1
- Uusi logo ilman BMW-nimeä (lisäosakaupan kuva näytti vielä vanhaa nimeä).
- Palautus raporttikansion tietokantavarmuuskopioista, jos siirtopakettia ei ole.

## 1.0.0
- Ensimmäinen julkinen versio nimellä **CarData Logbook**.
- Autot löytyvät automaattisesti Home Assistantin BMW CarData -integraatiosta VIN:n perusteella, ja niitä hallitaan Autot-sivulla.
- Käyttövoimat BEV, PHEV ja ICE, polttoaine bensiini tai diesel.
- Ajojen tunnistus CarDatan liiketilasta, sijainnista ja mittarilukemasta (haamuliikkeet ja myöhässä tuleva pysähdys huomioidaan), lataustauot kesken matkan.
- Kululaskenta (kWh ja litrat), yhteinen kotilaturi usealle autolle.
- Raportit PDF, Excel (kommenttien tuonti) ja CSV; mittarijatkuvuus, muutosloki, kuukausien lukitus SHA-256-arkistolla.
- Siirtopaketti: tiedot voi siirtää toisesta asennuksesta (Asetukset → Kirjoita siirtopaketti).
