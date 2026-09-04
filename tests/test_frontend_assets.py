import pathlib

F = pathlib.Path(__file__).parent.parent / "frontend"


def test_all_frontend_files_present_and_wired():
    # Check files that exist now (Task 6 created pages.js)
    for name in ["index.html", "style.css", "pages.js", "map.js", "skyplot.js",
                 "plots.js", "iqplot.js", "transmit.js", "app.js"]:
        assert (F / name).is_file(), name
    # Note: channels.js, live.js, trajectory.js, log.js don't exist until Tasks 7-10

    html = (F / "index.html").read_text()

    # Check that Task 6 shell structure is wired
    for src in ["pages.js", "map.js", "skyplot.js", "plots.js", "iqplot.js", "transmit.js", "app.js"]:
        assert src in html

    # Check new shell structure: sidebar and page sections
    assert 'data-page="channels"' in html
    assert 'data-page="trajectory"' in html
    assert 'data-page="log"' in html
    assert 'id="app-shell"' in html
    assert 'id="sidebar"' in html
    assert 'id="page-container"' in html


def test_transmit_js_requires_confirmation_checkbox():
    js = (F / "transmit.js").read_text()
    assert "confirm_isolated" in js
    # Note: tx-confirm checkbox moved to per-channel card in Task 8
    # (old flat markup replaced by Task 6 shell, no longer checked here)
