import io

import pytest
from fastapi.testclient import TestClient
from helpers import settings
from openpyxl import load_workbook
from test_logbook import trip

from logbook.main import LogbookApp
from logbook.web_ui import create_app


@pytest.fixture
def client(tmp_path):
    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    app = LogbookApp(s)
    a = app.services["530e"].db
    a.insert_trip(trip(1, 6, 10000, 15, "business", "Asiakas"))
    a.insert_trip(trip(1, 15, 10015, 15))
    a.insert_trip(trip(2, 8, 10050, 10))  # aukko 20 km
    app.services["ix3"].db.insert_trip(trip(1, 9, 500, 25))
    c = TestClient(create_app(app, restrict=False), headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    c.app_state = app
    return c


def test_pages_render(client):
    r = client.get("/")
    assert r.status_code == 200 and "530e" in r.text and "ix3" in r.text
    r = client.get("/v/530e?year=2026&month=10")
    assert r.status_code == 200
    assert "Kirjaamatta 20 km" in r.text
    assert "/api/hassio_ingress/abc/v/530e/trips/1/field" in r.text
    assert "Asiakas" in r.text
    # vain valitun auton matkat näkyvät
    assert "10050" in r.text.replace(" ", "").replace(" ", "")
    assert client.get("/v/530e/trips/1").status_code == 200
    assert client.get("/v/530e/reports?year=2026").status_code == 200
    assert client.get("/v/530e/new?prev=2&next=3").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.get("/v/tuntematon").status_code == 404


def test_inline_edit_and_cross_vehicle_merge(client):
    r = client.post("/v/530e/trips/2/field", json={"field": "comment", "value": "Testikommentti"})
    assert r.status_code == 200 and r.json()["trip"]["comment"] == "Testikommentti"
    r = client.post("/v/530e/trips/2/field", json={"field": "end_odometer", "value": "1"})
    assert r.status_code == 400
    # ix3:n matka 1 ei ole 530e:n tietokannassa (530e:n id 4 ei ole olemassa)
    r = client.post("/v/530e/trips/merge", data={"ids": ["1", "4"]}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert r.headers["location"].startswith("/api/hassio_ingress/abc/v/530e")
    r = client.post("/v/530e/trips/merge", data={"ids": ["1", "2"]}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert len(client.app_state.services["530e"].db.list_trips()) == 2
    assert len(client.app_state.services["ix3"].db.list_trips()) == 1


def test_downloads_and_upload(client):
    r = client.get("/v/530e/download?kind=month_pdf&year=2026&month=10&filter=business")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert "Ajopaivakirja_530e_ABC-123_2026_10_tyoajot.pdf" in r.headers["content-disposition"]
    r = client.get("/v/ix3/download?kind=xlsx&year=2026&filter=private&include_commute=0&include_commute=1")
    assert r.status_code == 200
    assert "Ajopaivakirja_ix3_XYZ-789_2026_yksityisajot.xlsx" in r.headers["content-disposition"]
    r = client.get("/v/530e/download?kind=csv&year=2026")
    assert r.status_code == 200 and "vehicle_id;rekisterinumero" in r.content.decode("utf-8-sig")
    # Excel takaisin
    xlsx = client.get("/v/530e/download?kind=xlsx&year=2026").content
    wb = load_workbook(io.BytesIO(xlsx))
    wb["10"].cell(row=4, column=12, value="Excel-kommentti")
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/v/530e/import", files={"file": ("k.xlsx", buf.getvalue())}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert client.app_state.services["530e"].db.get_trip(1)["comment"] == "Excel-kommentti"
    # iX3:n Excel ei mene 530e:lle
    ix3 = client.get("/v/ix3/download?kind=xlsx&year=2026").content
    r = client.post("/v/530e/import", files={"file": ("ix3.xlsx", ix3)}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_gap_ack_and_lock_flow(client):
    r = client.post("/v/530e/periods/lock", data={"year": 2026, "month": 10}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # aukko estää lukituksen
    client.post("/v/530e/gaps/ack", data={"prev": 2, "next": 3, "reason": "Huollon koeajo"})
    r = client.post("/v/530e/periods/lock", data={"year": 2026, "month": 10}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    r = client.get("/v/530e/download?kind=month_pdf&year=2026&month=10")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    r = client.post("/v/530e/trips/1/field", json={"field": "purpose", "value": "x"})
    assert r.status_code == 400 and "lukittu" in r.json()["error"]


def test_ingress_restriction(tmp_path):
    s = settings(export_dir=str(tmp_path / "share"))
    s.data_dir = str(tmp_path / "data")
    c = TestClient(create_app(LogbookApp(s), restrict=True))
    assert c.get("/").status_code == 403
