import pathlib

F = pathlib.Path(__file__).parent.parent / "frontend"


def test_all_frontend_files_present_and_wired():
    for name in ["index.html", "style.css", "map.js", "skyplot.js",
                 "plots.js", "transmit.js", "app.js"]:
        assert (F / name).is_file(), name
    html = (F / "index.html").read_text()
    for src in ["map.js", "skyplot.js", "plots.js", "transmit.js", "app.js"]:
        assert src in html
    for panel in ["panel-map", "panel-scenario", "panel-constellation",
                  "panel-generate", "panel-receiver", "panel-lnav", "panel-transmit"]:
        assert panel in html


def test_transmit_js_requires_confirmation_checkbox():
    js = (F / "transmit.js").read_text()
    assert "confirm_isolated" in js
    html = (F / "index.html").read_text()
    assert 'id="tx-confirm"' in html
