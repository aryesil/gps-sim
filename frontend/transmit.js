// frontend/transmit.js
window.startTransmit = function (outdir) {
  const body = {
    outdir,
    sample_rate: Number(document.getElementById('rate').value),
    sample_format: document.getElementById('fmt').value,
    uri: document.getElementById('tx-uri').value,
    lo_hz: Number(document.getElementById('tx-lo').value),
    tx_gain_db: Number(document.getElementById('tx-gain').value),
    confirm_isolated: document.getElementById('tx-confirm').checked,
    dry_run: document.getElementById('tx-dryrun').checked,
  };
  fetch('/api/transmit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => {
    if (r.status === 403) { alert('Tick the isolated-setup confirmation and set ALLOW_TX.'); return; }
    const rd = r.body.getReader(), dec = new TextDecoder();
    (function pump() {
      rd.read().then(({ value, done }) => {
        if (done) return;
        dec.decode(value).split('\n\n').forEach(chunk => {
          const line = chunk.replace(/^data: /, '').trim();
          if (!line) return;
          document.getElementById('tx-readout').textContent = line;
          try {
            const msg = JSON.parse(line);
            if (typeof msg.fraction === 'number') _drawTxProgress(msg.fraction);
          } catch (e) { /* not JSON, ignore */ }
        });
        pump();
      });
    })();
  });
};
document.getElementById('btn-transmit-stop').onclick =
  () => fetch('/api/transmit/stop', { method: 'POST' });

function _drawTxProgress(fraction) {
  const c = document.getElementById('tx-progress'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  g.fillStyle = '#eee'; g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = '#2a6'; g.fillRect(0, 0, c.width * Math.max(0, Math.min(1, fraction)), c.height);
}
