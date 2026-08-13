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
            logger.debug("SAT>IP src=%d: control URL → %s", self.src, self._stream_url)

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

            self._update(tuned=True, error=None, **self._parse_headers(resp))

            # Background thread discards incoming RTP so the OS buffer doesn't fill
            drain_stop = threading.Event()
            drain = threading.Thread(target=self._drain_udp, args=(drain_stop,), daemon=True)
            drain.start()

            try:
                # Give the DVB tuner time to acquire lock before first poll
                time.sleep(3)
                while not self._stop.is_set():
                    sig = self._poll_signal()
                    if sig:
                        self._update(**sig)
                    time.sleep(2)
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
        body = "Signal\r\nLock\r\nLevel\r\nQuality\r\n"
        try:
            # 30-second timeout: some devices hold GET_PARAMETER until the tuner locks
            resp = self._send('GET_PARAMETER', self._stream_url, body=body, timeout=30)
        except socket.timeout:
            logger.debug("SAT>IP src=%d: GET_PARAMETER timed out — will retry", self.src)
            return None
        parsed = self._parse_body(resp) or self._parse_headers(resp)
        return parsed if parsed else None

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


class SignalMonitor:
    """Manages per-src SAT>IP signal sessions."""

    def __init__(self) -> None:
        self._sessions: dict[int, SatIpSession] = {}
        self._lock = threading.Lock()

    def tune(self, host: str, port: int, tuners: list[dict]) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.stop()
            self._sessions.clear()
            for t in tuners:
                src = int(t.get('src', 1))
                sess = SatIpSession(
                    host=host,
                    port=port,
                    src=src,
                    freq_hz=int(t.get('freq_hz', 11425000)),
                    pol=t.get('pol', 'H'),
                    sr_ksps=int(t.get('sr_ksps', 27500)),
                    msys=t.get('msys', 'dvbs'),
                )
                sess.start()
                self._sessions[src] = sess

    def status(self) -> dict:
        with self._lock:
            return {str(src): s.get_status() for src, s in self._sessions.items()}

    def teardown(self) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.stop()
            self._sessions.clear()
