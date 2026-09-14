import json as jsonlib
import math
import os
import sys

from db import get_connection

SEED_FILE = os.path.join(os.path.dirname(__file__), "..", "seed", "seed_artists.json")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "dashboard.html")
CHARTJS_PATH = os.path.join(os.path.dirname(__file__), "vendor", "chart.umd.js")

TOP_GENRE_SLOTS = 3  # scatter/bubble color caps at 3 distinct hues before folding to "Other"
TOP_ARTISTS_LIMIT = 15
TOP_GENRES_LIMIT = 8


def fetch_kpis(cur):
    cur.execute("SELECT count(*) FROM artists")
    artist_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM events")
    event_count = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT genre_id) FROM events WHERE genre_id IS NOT NULL")
    genre_count = cur.fetchone()[0]

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed_total = len(jsonlib.load(f))

    return {
        "artist_count": artist_count,
        "event_count": event_count,
        "genre_count": genre_count,
        "seed_total": seed_total,
    }


def fetch_bubble_data(cur):
    cur.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            mb.began_active_year,
            mb.release_count,
            (SELECT count(*) FROM events e WHERE e.artist_id = a.artist_id) AS event_count,
            (
                SELECT g.genre_name FROM musicbrainz_source_genres msg
                JOIN genres g ON g.genre_id = msg.genre_id
                WHERE msg.mbid = a.mbid
                ORDER BY msg.tag_count DESC NULLS LAST, msg.genre_id ASC
                LIMIT 1
            ) AS genre_name
        FROM artists a
        JOIN artist_musicbrainz_source mb ON mb.mbid = a.mbid
        WHERE mb.began_active_year IS NOT NULL
        """
    )
    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    genre_freq = {}
    for row in rows:
        genre_freq[row["genre_name"] or "unknown"] = genre_freq.get(row["genre_name"] or "unknown", 0) + 1
    top_genres = sorted(genre_freq, key=lambda g: -genre_freq[g])[:TOP_GENRE_SLOTS]

    for row in rows:
        genre = row["genre_name"] or "unknown"
        row["bucket"] = genre if genre in top_genres else "Other"
        release_count = row["release_count"] or 0
        row["radius"] = round(min(24, 4 + math.sqrt(release_count) * 2), 1)

    return rows, top_genres


def fetch_genre_events(cur):
    cur.execute(
        """
        SELECT g.genre_name, count(*) AS event_count
        FROM events e
        JOIN genres g ON g.genre_id = e.genre_id
        GROUP BY g.genre_name
        ORDER BY event_count DESC
        LIMIT %s
        """,
        (TOP_GENRES_LIMIT,),
    )
    return [{"genre_name": row[0], "event_count": row[1]} for row in cur.fetchall()]


def fetch_top_artists(cur):
    cur.execute(
        """
        SELECT a.canonical_name, count(e.event_id) AS event_count
        FROM artists a
        JOIN events e ON e.artist_id = a.artist_id
        GROUP BY a.artist_id, a.canonical_name
        ORDER BY event_count DESC
        LIMIT %s
        """,
        (TOP_ARTISTS_LIMIT,),
    )
    return [{"canonical_name": row[0], "event_count": row[1]} for row in cur.fetchall()]


def fetch_events_timeline(cur):
    cur.execute(
        """
        SELECT to_char(date_trunc('month', event_date), 'YYYY-MM') AS month, count(*) AS event_count
        FROM events
        WHERE event_date IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """
    )
    return [{"month": row[0], "event_count": row[1]} for row in cur.fetchall()]


def fetch_confidence_histogram(cur):
    cur.execute(
        """
        SELECT
            CASE
                WHEN match_confidence >= 95 THEN '95-100'
                WHEN match_confidence >= 90 THEN '90-95'
                WHEN match_confidence >= 80 THEN '80-90'
                WHEN match_confidence >= 70 THEN '70-80'
                ELSE '60-70'
            END AS bucket,
            count(*) AS n
        FROM entity_resolution_map
        GROUP BY 1
        """
    )
    order = ["60-70", "70-80", "80-90", "90-95", "95-100"]
    counts = dict(cur.fetchall())
    return [{"bucket": b, "n": counts.get(b, 0)} for b in order]


PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Artist Activity Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-other: #c3c2b7;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-other: #52514e;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 16px 48px;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 24px; }
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .kpi-tile {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }
  .kpi-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .kpi-value { font-size: 28px; font-weight: 600; }
  .chart-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 16px;
  }
  .chart-card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }
  .chart-card h2 { font-size: 14px; margin: 0 0 2px; }
  .chart-card .caption { font-size: 12px; color: var(--text-secondary); margin: 0 0 12px; }
  .chart-card canvas { max-height: 320px; }
</style>
</head>
<body>
  <h1>Artist Activity Dashboard</h1>
  <p class="subtitle">Generated from the local Postgres instance -- run scripts/dashboard.py again after re-ingesting to refresh.</p>

  <div class="kpi-row">
    <div class="kpi-tile">
      <div class="kpi-label">Artists resolved</div>
      <div class="kpi-value">__ARTIST_COUNT__ / __SEED_TOTAL__</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">Total events</div>
      <div class="kpi-value">__EVENT_COUNT__</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">Genres represented</div>
      <div class="kpi-value">__GENRE_COUNT__</div>
    </div>
  </div>

  <div class="chart-grid">
    <div class="chart-card" style="grid-column: 1 / -1;">
      <h2>Catalog age vs. touring activity</h2>
      <p class="caption">Each dot is an artist -- x: year they began recording, y: current event count, size: release count, color: primary genre (top 3 + Other).</p>
      <canvas id="bubble"></canvas>
    </div>
    <div class="chart-card">
      <h2>Events by genre</h2>
      <p class="caption">Top __TOP_GENRES_LIMIT__ genres by total scheduled events.</p>
      <canvas id="genreBar"></canvas>
    </div>
    <div class="chart-card">
      <h2>Most active touring artists</h2>
      <p class="caption">Top __TOP_ARTISTS_LIMIT__ by event count.</p>
      <canvas id="artistBar"></canvas>
    </div>
    <div class="chart-card">
      <h2>Events over time</h2>
      <p class="caption">Scheduled events by month across all resolved artists.</p>
      <canvas id="timeline"></canvas>
    </div>
    <div class="chart-card">
      <h2>Entity resolution confidence</h2>
      <p class="caption">Distribution of match_confidence across every candidate in entity_resolution_map.</p>
      <canvas id="confidence"></canvas>
    </div>
  </div>

<script>__CHARTJS_SOURCE__</script>
<script>
const DATA = __DATA_JSON__;

const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const ink = isDark ? '#ffffff' : '#0b0b0b';
const inkSecondary = isDark ? '#c3c2b7' : '#52514e';
const grid = isDark ? '#2c2c2a' : '#e1e0d9';
const seriesColor = { s1: isDark ? '#3987e5' : '#2a78d6', s2: isDark ? '#d95926' : '#eb6834', s3: isDark ? '#199e70' : '#1baf7a', other: isDark ? '#52514e' : '#c3c2b7' };

Chart.defaults.color = inkSecondary;
Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
Chart.defaults.borderColor = grid;

const bubbleColorFor = (bucket) => {
  if (bucket === DATA.top_genres[0]) return seriesColor.s1;
  if (bucket === DATA.top_genres[1]) return seriesColor.s2;
  if (bucket === DATA.top_genres[2]) return seriesColor.s3;
  return seriesColor.other;
};

const bubbleGroups = {};
for (const row of DATA.bubble) {
  (bubbleGroups[row.bucket] ||= []).push({ x: row.began_active_year, y: row.event_count, r: row.radius, label: row.canonical_name });
}

new Chart(document.getElementById('bubble'), {
  type: 'bubble',
  data: {
    datasets: Object.entries(bubbleGroups).map(([bucket, points]) => ({
      label: bucket,
      data: points,
      backgroundColor: bubbleColorFor(bucket) + '99',
      borderColor: bubbleColorFor(bucket),
      borderWidth: 1,
    })),
  },
  options: {
    scales: {
      x: { title: { display: true, text: 'began recording (year)' }, grid: { color: grid }, ticks: { callback: (v) => Math.round(v) } },
      y: { title: { display: true, text: 'current event count' }, beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } },
    },
    plugins: {
      legend: { position: 'bottom' },
      tooltip: { callbacks: { label: (ctx) => `${ctx.raw.label}: ${ctx.raw.y} events` } },
    },
  },
});

new Chart(document.getElementById('genreBar'), {
  type: 'bar',
  data: {
    labels: DATA.genre_events.map(r => r.genre_name),
    datasets: [{ data: DATA.genre_events.map(r => r.event_count), backgroundColor: seriesColor.s1, borderRadius: 4, maxBarThickness: 24 }],
  },
  options: {
    indexAxis: 'y',
    plugins: { legend: { display: false } },
    scales: { x: { beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } }, y: { grid: { display: false } } },
  },
});

new Chart(document.getElementById('artistBar'), {
  type: 'bar',
  data: {
    labels: DATA.top_artists.map(r => r.canonical_name),
    datasets: [{ data: DATA.top_artists.map(r => r.event_count), backgroundColor: seriesColor.s1, borderRadius: 4, maxBarThickness: 24 }],
  },
  options: {
    indexAxis: 'y',
    plugins: { legend: { display: false } },
    scales: { x: { beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } }, y: { grid: { display: false } } },
  },
});

new Chart(document.getElementById('timeline'), {
  type: 'line',
  data: {
    labels: DATA.timeline.map(r => r.month),
    datasets: [{ data: DATA.timeline.map(r => r.event_count), borderColor: seriesColor.s1, backgroundColor: seriesColor.s1 + '1a', borderWidth: 2, pointRadius: 4, fill: true, tension: 0.15 }],
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } } },
  },
});

new Chart(document.getElementById('confidence'), {
  type: 'bar',
  data: {
    labels: DATA.confidence.map(r => r.bucket),
    datasets: [{ data: DATA.confidence.map(r => r.n), backgroundColor: seriesColor.s1, borderRadius: 4, maxBarThickness: 24 }],
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } } },
  },
});
</script>
</body>
</html>
"""


def render(kpis, bubble_rows, top_genres, genre_events, top_artists, timeline, confidence):
    data = {
        "bubble": bubble_rows,
        "top_genres": top_genres,
        "genre_events": genre_events,
        "top_artists": top_artists,
        "timeline": timeline,
        "confidence": confidence,
    }
    with open(CHARTJS_PATH, "r", encoding="utf-8") as f:
        chartjs_source = f.read()

    html = PAGE_TEMPLATE
    html = html.replace("__CHARTJS_SOURCE__", chartjs_source)
    html = html.replace("__DATA_JSON__", jsonlib.dumps(data))
    html = html.replace("__ARTIST_COUNT__", str(kpis["artist_count"]))
    html = html.replace("__SEED_TOTAL__", str(kpis["seed_total"]))
    html = html.replace("__EVENT_COUNT__", str(kpis["event_count"]))
    html = html.replace("__GENRE_COUNT__", str(kpis["genre_count"]))
    html = html.replace("__TOP_GENRES_LIMIT__", str(TOP_GENRES_LIMIT))
    html = html.replace("__TOP_ARTISTS_LIMIT__", str(TOP_ARTISTS_LIMIT))
    return html


def main() -> int:
    conn = get_connection()
    with conn.cursor() as cur:
        kpis = fetch_kpis(cur)
        bubble_rows, top_genres = fetch_bubble_data(cur)
        genre_events = fetch_genre_events(cur)
        top_artists = fetch_top_artists(cur)
        timeline = fetch_events_timeline(cur)
        confidence = fetch_confidence_histogram(cur)
    conn.close()

    if kpis["artist_count"] == 0:
        print("no rows in artists -- run populate_events.py first", file=sys.stderr)
        return 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html = render(kpis, bubble_rows, top_genres, genre_events, top_artists, timeline, confidence)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
