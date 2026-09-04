import pathlib

F = pathlib.Path(__file__).parent.parent / "frontend"

# transmit.js was deleted: after the per-card redesign it targeted elements
# (#btn-transmit-stop, #rate, #tx-confirm, ...) that no longer exist, and its
# top-level onclick assignment threw on every page load. The backend
# /api/transmit endpoint is unaffected.
_SCRIPTS = ["pages.js", "map.js", "skyplot.js", "plots.js", "iqplot.js",
            "channels.js", "live.js", "trajectory.js", "log.js", "app.js"]


def test_all_frontend_files_present_and_wired():
    for name in ["index.html", "style.css"] + _SCRIPTS:
        assert (F / name).is_file(), name

    html = (F / "index.html").read_text()

    # Check that all script tags in index.html correspond to real files
    for src in _SCRIPTS:
        assert src in html

    # Check new shell structure: sidebar and page sections
    assert 'data-page="channels"' in html
    assert 'data-page="trajectory"' in html
    assert 'data-page="log"' in html
    assert 'id="app-shell"' in html
    assert 'id="sidebar"' in html
    assert 'id="page-container"' in html


def test_no_orphaned_transmit_js():
    assert not (F / "transmit.js").exists()
    assert "transmit.js" not in (F / "index.html").read_text()


def test_channel_card_requires_confirmation_checkbox():
    """The README's safety claim ("you confirm the isolated setup in the UI")
    must correspond to a real per-card checkbox whose value is what
    /api/live/start receives -- not a hardcoded true."""
    js = (F / "channels.js").read_text()
    assert '${id}-tx-confirm' in js
    assert 'confirm_isolated: document.getElementById(`${id}-tx-confirm`).checked' in js
    assert 'confirm_isolated: true' not in js
