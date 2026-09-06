import pathlib

F = pathlib.Path(__file__).parent.parent / "frontend"

# transmit.js was deleted: after the per-card redesign it targeted elements
# (#btn-transmit-stop, #rate, #tx-confirm, ...) that no longer exist, and its
# top-level onclick assignment threw on every page load. The backend
# /api/transmit endpoint is unaffected.
_SCRIPTS = ["pages.js", "map.js", "skyplot.js", "plots.js", "iqplot.js",
            "compare.js", "channels.js", "live.js", "trajectory.js", "log.js",
            "app.js"]


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


def test_impairments_panel_present_and_wired():
    """The advanced RF-impairments panel must exist, stay opt-in, and fold
    into the /api/generate body only when enabled -- so an untouched panel
    leaves the generate request byte-identical to before it existed."""
    js = (F / "channels.js").read_text()
    assert 'data-adv="imp"' in js   # advanced sub-tab
    assert '${id}-imp-enabled' in js
    for sfx in ("seed", "cfo", "ppm", "phn", "gain", "iqphase",
                "dci", "dcq", "snr", "clip", "bits"):
        assert f'${{id}}-imp-{sfx}' in js, sfx
    # noise_power must not be exposed (mutually exclusive with snr_db server-side)
    assert 'noise_power' not in js
    # opt-in: null unless the enable box is checked, then merged into body
    assert 'if (!document.getElementById(`${id}-imp-enabled`).checked) return null;' in js
    assert 'body.impairments = _imp;' in js


def test_impairments_field_test_presets():
    """The panel offers ready-made field-test presets as an alternative to
    hand-entering every knob; choosing one enables the panel, hand-editing
    a field reverts to Custom."""
    js = (F / "channels.js").read_text()
    assert '${id}-imp-preset' in js
    assert 'IMP_PRESETS' in js
    for name in ('bench', 'field', 'urban'):
        assert f'{name}:' in js
    assert "document.getElementById(`${id}-imp-enabled`).checked = true;" in js
    assert "_impPresetSel.value = 'manual';" in js


def test_models_scenario_presets():
    """The propagation / receiver-model panel offers scenario presets as an
    alternative to setting every field by hand; hand-editing a field
    reverts to Custom."""
    js = (F / "channels.js").read_text()
    assert '${id}-mdl-preset' in js
    assert 'MDL_PRESETS' in js
    for name in ('open-sky', 'urban-canyon', 'foliage-weak'):
        assert f"'{name}':" in js
    assert "_mdlPresetSel.value = 'manual';" in js


def test_precise_panel_advertises_auto_download_no_manual_step():
    """Picking precise mode must not require the operator to place or fetch
    an SP3 file: the panel says Preview/Generate auto-download it, and the
    old manual 'fetch' button is gone. The path field stays as an optional
    override only."""
    js = (F / "channels.js").read_text()
    assert '${id}-sp3-fetch' not in js
    assert 'auto-download' in js.lower()
    assert '${id}-sp3-path' in js          # optional manual override kept


def test_channel_card_requires_confirmation_checkbox():
    """The README's safety claim ("you confirm the isolated setup in the UI")
    must correspond to a real per-card checkbox whose value is what
    /api/live/start receives -- not a hardcoded true."""
    js = (F / "channels.js").read_text()
    assert '${id}-tx-confirm' in js
    assert 'confirm_isolated: document.getElementById(`${id}-tx-confirm`).checked' in js
    assert 'confirm_isolated: true' not in js


def test_channel_models_panel_present_and_opt_in():
    """The propagation / receiver-model panel must exist, default every
    sub-model to 'off', and fold into the request body only when something
    is enabled (untouched panel -> _channelModelsBody() returns null)."""
    js = (F / "channels.js").read_text()
    assert 'data-adv="mdl"' in js   # advanced sub-tab
    for sfx in ("iono", "tropo", "rxclk", "rxclk-bias", "rxclk-drift",
                "mp", "mp1-delay", "mp1-amp", "mp1-phase", "to-iq"):
        assert f'${{id}}-mdl-{sfx}' in js, sfx
    assert 'if (Object.keys(out).length === 0) return null;' in js
    assert 'out.models_to_iq =' in js
    # wired into both preview and generate
    assert '...(_channelModelsBody() || {})' in js
    assert 'const _mdl = _channelModelsBody();' in js
    assert '_renderModelSummary(d.channel_models)' in js


def test_signal_engine_panel_present_and_opt_in():
    js = (F / "channels.js").read_text()
    assert 'data-adv="eng"' in js   # advanced sub-tab
    assert '${id}-engine' in js
    for sfx in ("fade-model", "fade-sigma", "fade-coh", "fade-seed", "fs", "quant"):
        assert f'${{id}}-{sfx}' in js, sfx
    # opt-in: default engine is gps-sdr-sim and an untouched panel adds nothing
    assert "value=\"gps-sdr-sim\"" in js
    assert 'if (_engineBody() === null) ' in js or 'const _eng = _engineBody();' in js


def test_compare_is_visualised_not_dumped_as_text():
    """The SP3-vs-broadcast compare must render through compare.js
    (canvas charts), not by writing raw lines into a <div>."""
    cmp = (F / "compare.js").read_text()
    assert 'window.renderCompare' in cmp
    assert "getContext('2d')" in cmp
    js = (F / "channels.js").read_text()
    assert 'renderCompare(`${id}-sp3-compare-out`, d)' in js
    # the sweep is requested so the chart is a curve over time
    assert 'sweep_s: dur' in js
    assert (F / "index.html").read_text().count("compare.js?v=") == 1


def test_compare_output_relocated_to_full_width_region_with_plain_language():
    """The compare result no longer stretches the narrow sim column: it
    renders in a dedicated full-width region below .channel-top, its charts
    lay out in a responsive grid, and the RMS jargon is replaced by a
    plain-language verdict plus a percentage."""
    js = (F / "channels.js").read_text()
    cmp = (F / "compare.js").read_text()
    css = (F / "style.css").read_text()
    assert 'id="${id}-compare-region"' in js
    assert 'region.hidden = false;' in js
    reg = js.index('id="${id}-compare-region"')
    assert reg > js.index('class="channel-top"')
    assert reg < js.index('id="${id}-sp3-compare-out"')          # out lives in the region
    assert '.compare-region' in css and '.compare-grid' in css
    assert 'compare-verdict' in cmp
    assert '% of one GPS code chip' in cmp
    assert "'position RMS'" not in cmp and "'range RMS'" not in cmp
    # dismissible, and re-opens from memory when inputs are unchanged
    assert '${id}-compare-close' in js
    assert 'document.getElementById(`${id}-compare-region`).hidden = true;' in js
    assert 'st._cmpKey === key && st._cmpData' in js
    # charts drawn at display pixel density
    assert 'setupCanvas' in cmp and 'devicePixelRatio' in cmp


def test_long_help_paragraphs_replaced_by_hover_info_icons():
    """Verbose <div class="hint"> blurbs sitting under form selects are
    gone; a small hover-tooltip marker carries the same text with no
    layout footprint."""
    js = (F / "channels.js").read_text()
    css = (F / "style.css").read_text()
    assert 'class="info" title=' in js
    assert js.count('class="info"') >= 6
    assert '.info {' in css
    assert '<div class="hint">precise: SP3' not in js
    assert '<div class="hint">Deterministic, seeded' not in js
    assert '<div class="hint">Reflections' not in js
    # the auto-download explanation survives, now inside a tooltip
    assert 'auto-download' in js.lower()


def test_engine_panel_has_systems_multiselect():
    js = (F / "channels.js").read_text()
    for s in ("G", "R", "E", "C", "J", "S"):
        assert f'${{id}}-sys-{s}' in js, s
    assert "systems" in js
    # opt-in: only added when more than G
    assert "sysSel.length > 1" in js or "sys.length > 1" in js


def test_skyplot_colours_by_system():
    js = (F / "skyplot.js").read_text()
    for s in ("R", "E", "C", "J", "S"):
        assert f"{s}:" in js  # _SYS_COLOR entry
    assert "svid" in js and "svid" in (F / "plots.js").read_text()


def test_skyplot_click_select_assigns_numeric_prn():
    # B1: the LNAV PRN field is <input type="number">; assigning a non-numeric
    # svid ("G01") blanks it and breaks click-select for every system. The
    # click handler must assign the bare numeric prn.
    js = (F / "skyplot.js").read_text()
    assert "prnInput.value = best.prn;" in js
    assert "best.svid || best.prn" not in js
