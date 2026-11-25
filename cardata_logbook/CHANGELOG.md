# Muutosloki

## 1.0.0
- Ensimmäinen julkinen versio nimellä **CarData Logbook**.
- Autot löytyvät automaattisesti Home Assistantin BMW CarData -integraatiosta VIN:n perusteella, ja niitä hallitaan Autot-sivulla.
- Käyttövoimat BEV, PHEV ja ICE, polttoaine bensiini tai diesel.
- Ajojen tunnistus CarDatan liiketilasta, sijainnista ja mittarilukemasta (haamuliikkeet ja myöhässä tuleva pysähdys huomioidaan), lataustauot kesken matkan.
- Kululaskenta (kWh ja litrat), yhteinen kotilaturi usealle autolle.
- Raportit PDF, Excel (kommenttien tuonti) ja CSV; mittarijatkuvuus, muutosloki, kuukausien lukitus SHA-256-arkistolla.
- Siirtopaketti: tiedot voi siirtää toisesta asennuksesta (Asetukset → Kirjoita siirtopaketti). Ilman pakettia autot palautetaan raporttikansion tietokantavarmuuskopioista.
