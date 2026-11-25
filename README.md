# CarData Logbook

Automaattinen ajopäiväkirja Home Assistantiin autoille, jotka ovat HA:ssa BMW CarData -integraation
([bmw-cardata-ha](https://github.com/kvanbiesen/bmw-cardata-ha)) kautta. Lisäosa löytää autot VIN:n
perusteella, tunnistaa ajot ja tekee niistä Verohallinnon vaatimukset täyttävät PDF- ja Excel-raportit.
Se ei avaa omaa yhteyttä BMW:lle, vaan lukee vain Home Assistantin dataa.

*An automatic driving logbook add-on for Home Assistant, built on the BMW CarData integration. It
discovers vehicles by VIN, detects trips and produces PDF and Excel reports meeting Finnish tax
authority requirements. It never opens its own BMW connection. The UI and reports are in Finnish for now.*

## Asennus

1. Home Assistant: **Asetukset → Lisäosat → Lisäosakauppa → ⋮ → Repositoriot**.
2. Lisää `https://github.com/zuiker1974/cardata-logbook`.
3. Asenna **CarData Logbook**, käynnistä ja lisää autot ajopäiväkirjan **Autot**-sivulla.

Tarkemmat ohjeet: [cardata_logbook/DOCS.md](cardata_logbook/DOCS.md).

## Kehitys

```
pip install -r cardata_logbook/requirements.txt pytest httpx
TZ=Europe/Helsinki python -m pytest -q tests
python tools/privacy_check.py
```

`tools/privacy_check.py` estää VIN-numeroiden, tarkkojen koordinaattien ja sähköpostiosoitteiden
päätymisen repoon. Käytä testeissä synteettistä dataa.

BMW ja CarData ovat BMW AG:n tavaramerkkejä. Tämä projekti ei ole BMW:n tekemä eikä hyväksymä.

## Lisenssi

MIT
