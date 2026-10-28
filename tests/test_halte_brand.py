from fastapi.testclient import TestClient
from fastapi_app.main import app

DISCLAIMER = (
    "Halte is an independent app, not affiliated with the City of Cape Town, "
    "MyCiTi or the University of Cape Town."
)

def test_halte_title_wordmark_and_disclaimer():
    with TestClient(app) as c:
        html = c.get("/").text
    assert "<title>" in html and "Halte</title>" in html
    assert 'property="og:site_name" content="Halte"' in html
    assert 'aria-label="Halte home"' in html
    assert DISCLAIMER in html
    assert "MyCiTi Timetable</title>" not in html
