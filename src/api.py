"""Read-only HTTP API for querying the meteor radar SQLite database.

Listens on 0.0.0.0:8765 (Tailscale-reachable). No auth — Tailscale is the boundary.

Endpoints:
  GET /health                         → {"status": "ok", "last_alive": "..."}
  GET /events?limit=50&since=ISO&rfi=0 → list of event rows (no spectrogram blob)
  GET /stats                          → hourly counts, totals, SNR distribution
  GET /baseline                       → latest baseline_state row
"""

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = '/mnt/hdd/meteor_radar/meteor_radar.db'
PORT = 8765


def _db():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _json(obj, status=200):
    body = json.dumps(obj, default=str).encode()
    return status, body


def handle_health():
    try:
        conn = _db()
        row = conn.execute(
            "SELECT last_alive FROM baseline_state ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_alive = row['last_alive'] if row else None
        conn.close()
        return _json({"status": "ok", "last_alive": last_alive})
    except Exception as e:
        return _json({"status": "error", "error": str(e)}, 500)


def handle_events(params):
    limit = int(params.get('limit', ['50'])[0])
    since = params.get('since', [None])[0]
    rfi = params.get('rfi', ['0'])[0]  # '0'=exclude, '1'=include, 'only'=only rfi

    try:
        conn = _db()
        where = []
        args = []

        if rfi == '0':
            where.append("suspected_rfi = 0")
        elif rfi == 'only':
            where.append("suspected_rfi = 1")

        if since:
            where.append("timestamp >= ?")
            args.append(since)

        sql = """
            SELECT id, timestamp, duration_ms, peak_power_db, snr_db,
                   integrated_power, frequency_centroid_hz, bandwidth_hz,
                   suspected_rfi, cluster_id, baseline_mean_db, baseline_std_db,
                   spectrogram_shape, fft_bin_width_hz
            FROM events
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        args.append(limit)

        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return _json({"count": len(rows), "events": rows})
    except Exception as e:
        return _json({"error": str(e)}, 500)


def handle_stats():
    try:
        conn = _db()

        total = conn.execute("SELECT COUNT(*) FROM events WHERE suspected_rfi=0").fetchone()[0]
        rfi_total = conn.execute("SELECT COUNT(*) FROM events WHERE suspected_rfi=1").fetchone()[0]

        since_24h = conn.execute("""
            SELECT COUNT(*) FROM events
            WHERE suspected_rfi=0
              AND replace(timestamp, 'T', ' ') >= datetime('now', '-24 hours')
        """).fetchone()[0]

        snr = conn.execute("""
            SELECT AVG(snr_db), MIN(snr_db), MAX(snr_db)
            FROM events WHERE suspected_rfi=0
        """).fetchone()

        duration = conn.execute("""
            SELECT AVG(duration_ms), MIN(duration_ms), MAX(duration_ms)
            FROM events WHERE suspected_rfi=0
        """).fetchone()

        hourly = conn.execute("""
            SELECT strftime('%Y-%m-%dT%H:00:00Z', timestamp) AS hour, COUNT(*) AS n
            FROM events
            WHERE suspected_rfi=0
              AND replace(timestamp, 'T', ' ') >= datetime('now', '-24 hours')
            GROUP BY hour
            ORDER BY hour
        """).fetchall()

        conn.close()
        return _json({
            "total_events": total,
            "total_rfi": rfi_total,
            "last_24h_events": since_24h,
            "snr_db": {"avg": snr[0], "min": snr[1], "max": snr[2]},
            "duration_ms": {"avg": duration[0], "min": duration[1], "max": duration[2]},
            "hourly_last_24h": [dict(r) for r in hourly],
        })
    except Exception as e:
        return _json({"error": str(e)}, 500)


def handle_baseline():
    try:
        conn = _db()
        row = conn.execute(
            "SELECT saved_at, mean_db, std_db, sample_count, last_alive FROM baseline_state ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return _json({"error": "no baseline saved yet"}, 404)
        return _json(dict(row))
    except Exception as e:
        return _json({"error": str(e)}, 500)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request stdout noise

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip('/')

        routes = {
            '/health': lambda: handle_health(),
            '/events': lambda: handle_events(params),
            '/stats': lambda: handle_stats(),
            '/baseline': lambda: handle_baseline(),
        }

        handler = routes.get(path)
        if handler is None:
            status, body = _json({"error": "not found"}, 404)
        else:
            status, body = handler()

        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"meteor-radar API listening on :{PORT}")
    server.serve_forever()


if __name__ == '__main__':
    main()
