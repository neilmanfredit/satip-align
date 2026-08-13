# SPDX-License-Identifier: CC-BY-NC-ND-4.0
"""
Minimal SAT>IP RTSP client for live signal monitoring.

Opens a direct RTSP session to the SAT>IP server (no TVHeadend required),
tunes to a transponder, and polls GET_PARAMETER every 2 seconds for
signal/lock/quality data. Each src= input runs in its own thread.
"""

import logging
import re
import socket
import threading
import time

logger = logging.getLogger(__name__)


class SatIpSession:
    """One SAT>IP RTSP session keeping a transponder tuned and reading signal."""

    def __init__(self, host: str, port: int, src: int,
                 freq_hz: int, pol: str, sr_ksps: int, msys: str):
        self.host = host
        self.port = port
        self.src = src
        # Convert Hz→MHz for SAT>IP URL (spec uses MHz with decimal)
        self._freq_mhz = freq_hz / 1000.0
        self._pol = pol.lower()
        self._sr_ksps = sr_ksps
        self._msys = msys.lower().replace('-', '')

        freq_label = f"{self._freq_mhz:.3f}".rstrip('0').rstrip('.')
        self._status: dict = {
            'src': src,
            'transponder': f"{freq_label}{pol.upper()} {self._msys.upper()}",
            'tuned': False,
            'lock': False,
            'signal': None,
            'quality': None,
            'error': None,
            'updated': None,
        }
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._cseq = 0
        self._session_id: str | None = None
        self._stream_url: str | None = None

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f'satip-src{self.src}')
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    # ── internals ─────────────────────────────────────────────────────────────

    def _update(self, **kw) -> None:
        with self._status_lock:
            self._status.update(kw)
            self._status['updated'] = time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session_loop()
            except Exception as exc:
                logger.warning("SAT>IP src=%d: %s", self.src, exc)
                self._update(tuned=False, lock=False, signal=None, quality=None,
                             error=str(exc))
                self._cleanup()
            if not self._stop.is_set():
                time.sleep(5)

    def _session_loop(self) -> None:
        # Bind UDP socket so the device has a real endpoint to stream to
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(('', 0))
        rtp_port = self._udp.getsockname()[1]
        self._udp.settimeout(0.1)

        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(10)
        self._tcp.connect((self.host, self.port))

        # Always use full 3-decimal MHz format — some devices reject "11425" without ".000"
        freq_str = f"{self._freq_mhz:.3f}"
        tune_url = (
            f"rtsp://{self.host}:{self.port}/"
            f"?src={self.src}&freq={freq_str}&pol={self._pol}"
            f"&msys={self._msys}&sr={self._sr_ksps}&fec=auto&pids=0"
        )

        try:
            # OPTIONS – some devices require this before anything
            self._send('OPTIONS', f"rtsp://{self.host}:{self.port}/")

            # DESCRIBE – delivers tuning params to the server and returns the
            # control URL (stream=N) that all subsequent requests must use.
            resp = self._send('DESCRIBE', tune_url, {'Accept': 'application/sdp'})
            if '200 OK' not in resp:
                raise ConnectionError(f"DESCRIBE rejected: {resp.splitlines()[0]}")

            self._stream_url = self._parse_control_url(resp)
            # minisatip v2.x embeds signal data in the SDP fmtp attribute —
            # parse it now so we show signal state immediately, before PLAY.
            initial_sig = self._parse_fmtp_signal(resp)
            logger.debug("SAT>IP src=%d: control URL → %s, initial signal: %s",
                         self.src, self._stream_url, initial_sig)

            # SETUP on the control URL
            resp = self._send('SETUP', self._stream_url, {
                'Transport': f'RTP/AVP;unicast;client_port={rtp_port}-{rtp_port + 1}'
            })
            if '200 OK' not in resp:
                raise ConnectionError(f"SETUP rejected: {resp.splitlines()[0]}")
            m = re.search(r'Session:\s*([^\r\n;]+)', resp)
            if not m:
                raise ConnectionError("No session ID in SETUP response")
            self._session_id = m.group(1).strip()

            # PLAY
            resp = self._send('PLAY', self._stream_url, {'Range': 'npt=0.000-'})
            if '200 OK' not in resp:
                raise ConnectionError(f"PLAY rejected: {resp.splitlines()[0]}")

            # Apply initial signal from DESCRIBE (tuner already locked at this point)
            self._update(tuned=True, error=None, **(initial_sig or {}))

            # Background thread discards incoming RTP so the OS buffer doesn't fill
            drain_stop = threading.Event()
            drain = threading.Thread(target=self._drain_udp, args=(drain_stop,), daemon=True)
            drain.start()

            try:
                while not self._stop.is_set():
                    sig = self._poll_signal()
                    if sig:
                        self._update(**sig)
                    time.sleep(3)
            finally:
                drain_stop.set()
        finally:
            self._cleanup()

    def _drain_udp(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._udp.recv(65536)
            except (socket.timeout, OSError):
                pass

    def _cleanup(self) -> None:
        try:
            if self._session_id and self._stream_url:
                self._send('TEARDOWN', self._stream_url)
        except Exception:
            pass
        for attr in ('_tcp', '_udp'):
            s = getattr(self, attr, None)
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._tcp = None
        self._udp = None
        self._session_id = None
        self._cseq = 0

    def _send(self, method: str, url: str,
              extra: dict | None = None, body: str | None = None,
              timeout: float = 10) -> str:
        self._cseq += 1
        parts = [
            f"{method} {url} RTSP/1.0",
            f"CSeq: {self._cseq}",
            "User-Agent: SatIPAligner/1.0",
        ]
        if self._session_id:
            parts.append(f"Session: {self._session_id}")
        if extra:
            parts.extend(f"{k}: {v}" for k, v in extra.items())
        if body:
            parts.append("Content-Type: text/parameters")
            parts.append(f"Content-Length: {len(body.encode())}")
        req = "\r\n".join(parts) + "\r\n\r\n"
        if body:
            req += body
        self._tcp.sendall(req.encode())
        return self._recv(timeout)

    def _recv(self, timeout: float = 10) -> str:
        data = b""
        self._tcp.settimeout(timeout)
        while True:
            chunk = self._tcp.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                hdr_end = data.index(b"\r\n\r\n") + 4
                hdr = data[:hdr_end].decode(errors='replace')
                m = re.search(r'Content-Length:\s*(\d+)', hdr, re.IGNORECASE)
                if m:
                    cl = int(m.group(1))
                    if len(data) >= hdr_end + cl:
                        break
                else:
                    break
        return data.decode(errors='replace')

    def _poll_signal(self) -> dict | None:
        # minisatip v2.x does not support GET_PARAMETER — signal state is embedded
        # in the SDP fmtp attribute returned by DESCRIBE on the active stream URL.
        try:
            resp = self._send('DESCRIBE', self._stream_url,
                              {'Accept': 'application/sdp'}, timeout=10)
            if '200 OK' in resp:
                return self._parse_fmtp_signal(resp)
        except socket.timeout:
            logger.debug("SAT>IP src=%d: DESCRIBE poll timed out", self.src)
        return None

    def _parse_fmtp_signal(self, resp: str) -> dict | None:
        # SAT>IP SDP: a=fmtp:33 ver=1.0;src=N;tuner=<id>,<level>,<lock>,<quality>,...
        # level: 0-255, lock: 0/1, quality: 0-15
        m = re.search(r'tuner=\d+,(\d+),(\d+),(\d+)', resp)
        if not m:
            return None
        level = int(m.group(1))
        lock = int(m.group(2))
        quality = int(m.group(3))
        return {
            'lock': lock == 1,
            'signal': round(level / 255 * 100, 1),
            'quality': round(quality / 15 * 100, 1),
        }

    def _parse_control_url(self, resp: str) -> str:
        sep = '\r\n\r\n' if '\r\n\r\n' in resp else '\n\n'
        body = resp.split(sep, 1)[1] if sep in resp else ''
        for line in body.splitlines():
            line = line.strip()
            if line.lower().startswith('a=control:'):
                url = line[len('a=control:'):].strip()
                if url.startswith('rtsp://'):
                    return url
                return f"rtsp://{self.host}:{self.port}/{url.lstrip('/')}"
        return f"rtsp://{self.host}:{self.port}/stream=1"

    def _parse_headers(self, resp: str) -> dict:
        result: dict = {}
        for line in resp.splitlines():
            k, _, v = line.partition(':')
            k = k.strip().lower()
            v = v.strip()
            if k == 'x-satip-signal':
                try:
                    result['signal'] = float(v)
                except ValueError:
                    pass
            elif k == 'x-satip-lock':
                result['lock'] = v in ('1', 'true')
            elif k == 'x-satip-quality':
                try:
                    result['quality'] = float(v)
                except ValueError:
                    pass
        return result

    def _parse_body(self, resp: str) -> dict | None:
        if '\r\n\r\n' not in resp and '\n\n' not in resp:
            return None
        sep = '\r\n\r\n' if '\r\n\r\n' in resp else '\n\n'
        body = resp.split(sep, 1)[1]
        result: dict = {}
        for line in body.splitlines():
            k, _, v = line.partition(':')
            k = k.strip().lower()
            v = v.strip()
            if k == 'signal':
                try:
                    result['signal'] = float(v)
                except ValueError:
                    pass
            elif k == 'lock':
                result['lock'] = v in ('1', 'true', 'yes', 'locked')
            elif k == 'quality':
                try:
                    result['quality'] = float(v)
                except ValueError:
                    pass
            elif k == 'level':
                try:
                    result['signal'] = float(v)
                except ValueError:
                    pass
        return result if result else None


# ── Reference transponders for aggregate dish alignment (Astra 28.2°E) ─────────
# Spread across H/V polarisations and DVB-S/S2 to exercise both LNB outputs
# and the full tuner range.
ASTRA_282_REFERENCE = [
    {'label': '11425H',  'freq_hz': 11425000, 'pol': 'H', 'sr_ksps': 27500, 'msys': 'dvbs'},
    {'label': '11344H',  'freq_hz': 11344000, 'pol': 'H', 'sr_ksps': 27500, 'msys': 'dvbs'},
    {'label': '11778V',  'freq_hz': 11778000, 'pol': 'V', 'sr_ksps': 27500, 'msys': 'dvbs'},
    {'label': '10818V',  'freq_hz': 10817500, 'pol': 'V', 'sr_ksps': 23000, 'msys': 'dvbs2'},
    {'label': '11671H',  'freq_hz': 11670750, 'pol': 'H', 'sr_ksps': 23000, 'msys': 'dvbs2'},
]


def _rtsp_recv(tcp: socket.socket) -> str:
    data = b""
    while True:
        chunk = tcp.recv(4096)
        if not chunk:
            break
        data += chunk
        if b"\r\n\r\n" in data:
            hdr_end = data.index(b"\r\n\r\n") + 4
            hdr = data[:hdr_end].decode(errors='replace')
            m = re.search(r'Content-Length:\s*(\d+)', hdr, re.IGNORECASE)
            if m:
                cl = int(m.group(1))
                if len(data) >= hdr_end + cl:
                    break
            else:
                break
    return data.decode(errors='replace')


def _rtsp_control_url(resp: str, host: str, port: int) -> str:
    sep = '\r\n\r\n' if '\r\n\r\n' in resp else '\n\n'
    body = resp.split(sep, 1)[1] if sep in resp else ''
    for line in body.splitlines():
        line = line.strip()
        if line.lower().startswith('a=control:'):
            url = line[len('a=control:'):].strip()
            if url.startswith('rtsp://'):
                return url
            return f"rtsp://{host}:{port}/{url.lstrip('/')}"
    return f"rtsp://{host}:{port}/stream=1"


def _rtsp_fmtp(resp: str) -> dict | None:
    m = re.search(r'tuner=\d+,(\d+),(\d+),(\d+)', resp)
    if not m:
        return None
    level, lock, quality = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return {
        'lock': lock == 1,
        'signal': round(level / 255 * 100, 1),
        'quality': round(quality / 15 * 100, 1),
    }


class AggregatedSession:
    """
    Cycles through a list of transponders on one src tuner, storing the most
    recent lock/signal/quality for each. Exposes aggregate metrics for the UI.

    For each transponder the flow is:
      DESCRIBE (allocates tuner, starts DVB tuning) →
      wait 0.9 s for frontend to lock →
      DESCRIBE on stream URL (reads settled state) →
      TCP close (releases tuner)
    """

    def __init__(self, host: str, port: int, src: int, transponders: list[dict]):
        self.host = host
        self.port = port
        self.src = src
        self._transponders = transponders
        self._readings: dict[str, dict] = {}
        self._rlock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f'satip-agg-src{self.src}')
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def get_status(self) -> dict:
        with self._rlock:
            readings = list(self._readings.values())
        total = len(self._transponders)
        if not readings:
            return {
                'src': self.src, 'ready': False,
                'locked': 0, 'total': total,
                'signal': None, 'quality': None, 'score': None,
                'transponders': [],
            }
        locked = sum(1 for r in readings if r.get('lock'))
        signals = [r['signal'] for r in readings if r.get('signal') is not None]
        qualities = [r['quality'] for r in readings if r.get('quality') is not None]
        avg_sig = round(sum(signals) / len(signals), 1) if signals else None
        avg_qual = round(sum(qualities) / len(qualities), 1) if qualities else None
        lock_pct = locked / total * 100
        score = round((lock_pct + avg_sig + avg_qual) / 3, 1) \
            if avg_sig is not None and avg_qual is not None else None
        return {
            'src': self.src, 'ready': True,
            'locked': locked, 'total': total,
            'signal': avg_sig, 'quality': avg_qual, 'score': score,
            'transponders': sorted(readings, key=lambda r: r['label']),
        }

    def _run(self) -> None:
        idx = 0
        while not self._stop.is_set():
            tp = self._transponders[idx % len(self._transponders)]
            try:
                reading = self._read_transponder(tp)
                with self._rlock:
                    self._readings[tp['label']] = reading
            except Exception as e:
                logger.debug("SAT>IP agg src=%d %s: %s", self.src, tp['label'], e)
                with self._rlock:
                    self._readings.setdefault(tp['label'], {'label': tp['label']})['error'] = str(e)
            idx += 1
            if not self._stop.is_set():
                time.sleep(0.4)

    def _read_transponder(self, tp: dict) -> dict:
        freq_mhz = tp['freq_hz'] / 1000.0
        pol = tp['pol'].lower()
        msys = tp['msys'].lower()
        tune_url = (
            f"rtsp://{self.host}:{self.port}/"
            f"?src={self.src}&freq={freq_mhz:.3f}&pol={pol}"
            f"&msys={msys}&sr={tp['sr_ksps']}&fec=auto&pids=0"
        )

        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.settimeout(8)
        cseq = 0

        def req(method, url, extra=None):
            nonlocal cseq
            cseq += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: SatIPAligner/1.0"]
            if extra:
                lines.extend(f"{k}: {v}" for k, v in extra.items())
            tcp.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            return _rtsp_recv(tcp)

        try:
            tcp.connect((self.host, self.port))

            resp = req('DESCRIBE', tune_url, {'Accept': 'application/sdp'})
            if '200 OK' not in resp:
                return {'label': tp['label'], 'lock': False, 'signal': None, 'quality': None}

            stream_url = _rtsp_control_url(resp, self.host, self.port)
            sig = _rtsp_fmtp(resp)

            time.sleep(0.9)  # let DVB frontend acquire lock

            resp2 = req('DESCRIBE', stream_url, {'Accept': 'application/sdp'})
            if '200 OK' in resp2:
                sig = _rtsp_fmtp(resp2) or sig

            return {
                'label': tp['label'],
                'lock': bool(sig.get('lock')) if sig else False,
                'signal': sig.get('signal') if sig else None,
                'quality': sig.get('quality') if sig else None,
                'error': None,
            }
        finally:
            tcp.close()


class SignalMonitor:
    """Manages per-src SAT>IP signal sessions (single-transponder or aggregate)."""

    def __init__(self) -> None:
        self._sessions: dict[int, SatIpSession] = {}
        self._agg_sessions: dict[int, AggregatedSession] = {}
        self._lock = threading.Lock()

    def tune(self, host: str, port: int, tuners: list[dict]) -> None:
        with self._lock:
            self._stop_all()
            for t in tuners:
                src = int(t.get('src', 1))
                sess = SatIpSession(
                    host=host, port=port, src=src,
                    freq_hz=int(t.get('freq_hz', 11425000)),
                    pol=t.get('pol', 'H'),
                    sr_ksps=int(t.get('sr_ksps', 27500)),
                    msys=t.get('msys', 'dvbs'),
                )
                sess.start()
                self._sessions[src] = sess

    def tune_aggregate(self, host: str, port: int, src_list: list[int]) -> None:
        with self._lock:
            self._stop_all()
            for src in src_list:
                sess = AggregatedSession(
                    host=host, port=port, src=src,
                    transponders=ASTRA_282_REFERENCE,
                )
                sess.start()
                self._agg_sessions[src] = sess

    def status(self) -> dict:
        with self._lock:
            return {str(src): s.get_status() for src, s in self._sessions.items()}

    def aggregate_status(self) -> list[dict]:
        with self._lock:
            return [s.get_status() for s in self._agg_sessions.values()]

    def teardown(self) -> None:
        with self._lock:
            self._stop_all()

    def _stop_all(self) -> None:
        for s in self._sessions.values():
            s.stop()
        for s in self._agg_sessions.values():
            s.stop()
        self._sessions.clear()
        self._agg_sessions.clear()
