"""Read-only HTTP API for querying the meteor radar SQLite database.

Listens on 0.0.0.0:8765 (Tailscale-reachable). No auth — Tailscale is the boundary.

Endpoints:
  GET /health                              → {"status": "ok", "last_alive": "..."}
  GET /events?limit=50&since=ISO&rfi=0    → list of event rows (no spectrogram blob)
  GET /spectrogram?ids=1,2,3&max_rows=80  → raw spectrogram data per event id
  GET /stats                              → hourly counts, totals, SNR distribution
  GET /baseline                           → latest baseline_state row
"""

import json
import sqlite3
import struct
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


def handle_spectrogram(params):
    ids_str = params.get('ids', [''])[0]
    max_rows = int(params.get('max_rows', ['80'])[0])
    if not ids_str:
        return _json({"error": "ids parameter required"}, 400)

    try:
        event_ids = [int(x) for x in ids_str.split(',') if x.strip()]
    except ValueError:
        return _json({"error": "ids must be comma-separated integers"}, 400)

    try:
        conn = _db()
        result = {}
        placeholders = ','.join('?' * len(event_ids))
        rows = conn.execute(
            f"SELECT id, spectrogram, spectrogram_shape FROM events WHERE id IN ({placeholders})",
            event_ids
        ).fetchall()
        conn.close()

        for row in rows:
            eid = row['id']
            blob = row['spectrogram']
            shape_str = row['spectrogram_shape']
            if not blob or not shape_str:
                continue

            n_rows, n_cols = (int(x) for x in shape_str.split(','))
            n_floats = n_rows * n_cols
            if len(blob) < n_floats * 4:
                continue

            # Unpack float32 values
            flat = struct.unpack_from(f'{n_floats}f', blob, 0)
            grid = [list(flat[r * n_cols:(r + 1) * n_cols]) for r in range(n_rows)]

            # Downsample rows via max-pooling if over max_rows
            if n_rows > max_rows:
                step = n_rows / max_rows
                downsampled = []
                for i in range(max_rows):
                    r0 = int(i * step)
                    r1 = max(r0 + 1, int((i + 1) * step))
                    pool_rows = grid[r0:r1]
                    merged = [max(pool_rows[r][c] for r in range(len(pool_rows))) for c in range(n_cols)]
                    downsampled.append(merged)
                grid = downsampled
                n_rows = max_rows

            # Round to 2dp to reduce JSON size
            grid = [[round(v, 2) for v in row] for row in grid]
            result[str(eid)] = {"rows": n_rows, "cols": n_cols, "data": grid}

        return _json(result)
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
            '/spectrogram': lambda: handle_spectrogram(params),
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
