# SPDX-License-Identifier: CC-BY-NC-ND-4.0
"""
SAT>IP Satellite Alignment and Signal Monitor
Mobile-friendly Flask app for dish alignment and SAT>IP signal checking.
Talks directly to the SAT>IP device — no TVHeadend required.
"""

import logging
import os

import requests as http
from flask import Flask, jsonify, render_template, request

from satip_rtsp import SignalMonitor

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
monitor = SignalMonitor()

# ── satellite catalogue ────────────────────────────────────────────────────────

SATELLITES = [
    {"name": "Astra 2 — 28.2°E  (Freesat UK)", "lon": 28.2},
    {"name": "Eutelsat 28A — 28.5°E", "lon": 28.5},
    {"name": "Astra 1 — 19.2°E  (Germany/Europe)", "lon": 19.2},
    {"name": "Hotbird — 13°E  (Mediterranean)", "lon": 13.0},
    {"name": "Thor/Intelsat — 0.8°W  (Nordic)", "lon": -0.8},
    {"name": "Amos — 4.0°W", "lon": -4.0},
    {"name": "Atlantic Bird — 12.5°W", "lon": -12.5},
    {"name": "Telstar 11N — 37.5°W", "lon": -37.5},
]

# Astra 28.2°E Ku-band transponder presets (from verified UK Freesat network scan)
# freq_hz: frequency in Hz; sr_ksps: symbol rate in ksps (= kbaud)
TUNE_PRESETS = [
    {"label": "11425.0H — Freesat bootstrap (DVB-S)",   "freq_hz": 11425000, "pol": "H", "sr_ksps": 27500, "msys": "dvbs"},
    {"label": "10773.25H — BBC One NW HD (DVB-S)",       "freq_hz": 10773250, "pol": "H", "sr_ksps": 27500, "msys": "dvbs"},
    {"label": "10817.5V (DVB-S2)",                       "freq_hz": 10817500, "pol": "V", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "10936.0V (DVB-S2)",                       "freq_hz": 10936000, "pol": "V", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "11023.25H (DVB-S2)",                      "freq_hz": 11023250, "pol": "H", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "11225.0V (DVB-S2)",                       "freq_hz": 11225000, "pol": "V", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "11305.0H (DVB-S2)",                       "freq_hz": 11305000, "pol": "H", "sr_ksps": 27500, "msys": "dvbs2"},
    {"label": "11344.0H (DVB-S)",                        "freq_hz": 11344000, "pol": "H", "sr_ksps": 27500, "msys": "dvbs"},
    {"label": "11386.5H (DVB-S)",                        "freq_hz": 11386500, "pol": "H", "sr_ksps": 27500, "msys": "dvbs"},
    {"label": "11493.75H (DVB-S2)",                      "freq_hz": 11493750, "pol": "H", "sr_ksps": 22000, "msys": "dvbs2"},
    {"label": "11582.25H (DVB-S2)",                      "freq_hz": 11582250, "pol": "H", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "11670.75H (DVB-S2)",                      "freq_hz": 11670750, "pol": "H", "sr_ksps": 23000, "msys": "dvbs2"},
    {"label": "11778.0V (DVB-S)",                        "freq_hz": 11778000, "pol": "V", "sr_ksps": 27500, "msys": "dvbs"},
    {"label": "12012.0V (DVB-S2)",                       "freq_hz": 12012000, "pol": "V", "sr_ksps": 27500, "msys": "dvbs2"},
    {"label": "12109.5H (DVB-S2)",                       "freq_hz": 12109500, "pol": "H", "sr_ksps": 27500, "msys": "dvbs2"},
    {"label": "12207.0V (DVB-S2)",                       "freq_hz": 12207000, "pol": "V", "sr_ksps": 27500, "msys": "dvbs2"},
    {"label": "12285.0V (DVB-S2)",                       "freq_hz": 12285000, "pol": "V", "sr_ksps": 27500, "msys": "dvbs2"},
    {"label": "12363.0V (DVB-S2)",                       "freq_hz": 12363000, "pol": "V", "sr_ksps": 27500, "msys": "dvbs2"},
]

# ── routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', satellites=SATELLITES, presets=TUNE_PRESETS)

@app.route('/api/postcode/<postcode>')
def lookup_postcode(postcode):
    pc = postcode.strip().upper().replace(' ', '')
    try:
        r = http.get(f'https://api.postcodes.io/postcodes/{pc}', timeout=8)
        data = r.json()
        if data.get('status') == 200:
            res = data['result']
            return jsonify({
                'lat': res['latitude'],
                'lon': res['longitude'],
                'place': res.get('admin_district') or res.get('postcode') or pc,
            })
        return jsonify({'error': 'Postcode not found — try without space, e.g. SK61AA'}), 404
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

@app.route('/api/signal/tune', methods=['POST'])
def tune():
    body = request.json or {}
    host = (body.get('host') or '').strip()
    port = int(body.get('port') or 554)
    tuners = body.get('tuners') or []
    if not host:
        return jsonify({'error': 'host is required'}), 400
    if not tuners:
        return jsonify({'error': 'tuners list is required'}), 400
    monitor.tune(host, port, tuners)
    return jsonify({'ok': True, 'count': len(tuners)})

@app.route('/api/signal/status')
def signal_status():
    return jsonify(monitor.status())

@app.route('/api/signal/teardown', methods=['POST'])
def teardown():
    monitor.teardown()
    return jsonify({'ok': True})

@app.route('/api/signal/tune-aggregate', methods=['POST'])
def tune_aggregate():
    body = request.json or {}
    host = (body.get('host') or '').strip()
    port = int(body.get('port') or 554)
    src_list = [int(s) for s in (body.get('src_list') or [1])]
    if not host:
        return jsonify({'error': 'host is required'}), 400
    monitor.tune_aggregate(host, port, src_list)
    return jsonify({'ok': True, 'srcs': src_list})

@app.route('/api/signal/aggregate-status')
def aggregate_status():
    return jsonify(monitor.aggregate_status())

@app.route('/api/satellites')
def satellites():
    return jsonify(SATELLITES)

@app.route('/api/presets')
def presets():
    return jsonify(TUNE_PRESETS)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 9081))
    app.run(host='0.0.0.0', port=port, threaded=True)
