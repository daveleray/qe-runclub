"""
Weekly run club stats emailer.
Pulls activities from a Strava club, saves stats to data/weekly_stats.json,
and emails a summary. The JSON file is committed back to the repo after each run,
building a permanent history that outlives Strava's ~6-week API window.

Required env vars:
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
    STRAVA_CLUB_ID
    EMAIL_FROM, EMAIL_TO (comma-separated), EMAIL_APP_PASSWORD

Optional env vars:
    WEEK_START   — Monday date (YYYY-MM-DD) to recap a specific past week
    OMNIBUS      — set to "true" to send a week-by-week recap for the last N weeks
    OMNIBUS_WEEKS — number of weeks for omnibus (default 6)
"""

import json
import os
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

DB_PATH = "data/weekly_stats.json"


# ── Database ──────────────────────────────────────────────────────────────────

def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH) as f:
            return json.load(f)
    return []


def save_db(records):
    os.makedirs("data", exist_ok=True)
    with open(DB_PATH, "w") as f:
        json.dump(records, f, indent=2)
    print(f"Saved {len(records)} weeks to {DB_PATH}")


def upsert_record(records, new_record):
    for i, r in enumerate(records):
        if r["week_start"] == new_record["week_start"]:
            records[i] = new_record
            return records
    records.append(new_record)
    records.sort(key=lambda r: r["week_start"])
    return records


def find_record(records, week_start_str):
    return next((r for r in records if r["week_start"] == week_start_str), None)


# ── Strava ────────────────────────────────────────────────────────────────────

def get_access_token():
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_club_activities(token, club_id, after):
    headers = {"Authorization": f"Bearer {token}"}
    activities = []
    page = 1
    while True:
        resp = requests.get(
            f"https://www.strava.com/api/v3/clubs/{club_id}/activities",
            headers=headers,
            params={"per_page": 200, "page": page, "after": after},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 200:
            break
        page += 1
        time.sleep(0.5)
    return [a for a in activities if a.get("type") == "Run"]


def activity_fingerprint(a):
    return (
        a.get("athlete", {}).get("firstname"),
        a.get("athlete", {}).get("lastname"),
        a.get("name"),
        a.get("distance"),
        a.get("moving_time"),
    )


def subtract_activities(larger, smaller):
    seen = set(activity_fingerprint(a) for a in smaller)
    return [a for a in larger if activity_fingerprint(a) not in seen]


# ── Stats ─────────────────────────────────────────────────────────────────────

def meters_to_miles(m):
    return m / 1609.344


def seconds_to_pace(seconds_per_meter):
    if not seconds_per_meter:
        return "N/A"
    spm = seconds_per_meter * 1609.344
    mins = int(spm // 60)
    secs = int(spm % 60)
    return f"{mins}:{secs:02d}"


def build_stats(runs):
    if not runs:
        return None

    by_athlete = {}
    for r in runs:
        name = f"{r.get('athlete', {}).get('firstname', '?')} {r.get('athlete', {}).get('lastname', '?')}"
        by_athlete.setdefault(name, []).append(r)

    leaderboard = []
    for name, athlete_runs in by_athlete.items():
        total_dist = sum(r["distance"] for r in athlete_runs)
        total_time = sum(r["moving_time"] for r in athlete_runs)
        avg_pace   = total_time / total_dist if total_dist else 0
        leaderboard.append({
            "name":     name,
            "runs":     len(athlete_runs),
            "miles":    round(meters_to_miles(total_dist), 2),
            "avg_pace": seconds_to_pace(avg_pace),
        })

    leaderboard.sort(key=lambda x: x["miles"], reverse=True)

    return {
        "total_runs":  len(runs),
        "total_miles": round(meters_to_miles(sum(r["distance"] for r in runs)), 2),
        "participants": len(by_athlete),
        "leaderboard": leaderboard,
    }


def stats_to_record(stats, week_start, week_end):
    return {
        "week_start":  week_start.strftime("%Y-%m-%d"),
        "week_end":    week_end.strftime("%Y-%m-%d"),
        "label":       f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}",
        "total_runs":  stats["total_runs"],
        "total_miles": stats["total_miles"],
        "participants": stats["participants"],
        "leaderboard": stats["leaderboard"],
    }


# ── Email ─────────────────────────────────────────────────────────────────────

def leaderboard_table(leaderboard):
    medal = ["🥇", "🥈", "🥉"]
    rows = ""
    for i, athlete in enumerate(leaderboard):
        rank = medal[i] if i < 3 else str(i + 1)
        rows += f"""
        <tr style="background:{'#f9f9f9' if i % 2 else '#fff'}">
            <td style="padding:8px 12px">{rank}</td>
            <td style="padding:8px 12px">{athlete['name']}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['runs']}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['miles']:.1f}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['avg_pace']}</td>
        </tr>"""
    return f"""
  <table style="width:100%;border-collapse:collapse;font-size:0.95em;margin-bottom:24px">
    <thead>
      <tr style="background:#fc4c02;color:#fff">
        <th style="padding:8px 12px">#</th>
        <th style="padding:8px 12px;text-align:left">Athlete</th>
        <th style="padding:8px 12px">Runs</th>
        <th style="padding:8px 12px">Miles</th>
        <th style="padding:8px 12px">Avg Pace</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""


def format_week_section(record):
    if not record or not record.get("leaderboard"):
        label = record["label"] if record else "Unknown week"
        return f'<h3 style="color:#fc4c02">{label}</h3><p style="color:#888;margin-bottom:24px">No runs logged.</p>'
    return f"""
  <h3 style="color:#fc4c02;margin-bottom:4px">{record['label']}</h3>
  <p style="margin:0 0 8px 0;font-size:0.95em">
    <strong>{record['total_runs']}</strong> runs &nbsp;·&nbsp;
    <strong>{record['total_miles']:.1f} mi</strong> total &nbsp;·&nbsp;
    <strong>{record['participants']}</strong> athletes
  </p>
  {leaderboard_table(record['leaderboard'])}"""


def wrap_html(title, body_content):
    return f"""
<html><body style="font-family:sans-serif;color:#222;max-width:620px;margin:auto">
  <h2 style="color:#fc4c02">🏃 {title}</h2>
  {body_content}
  <p style="font-size:0.8em;color:#888;margin-top:24px">Powered by Strava</p>
</body></html>"""


def send_email(subject, html_body):
    sender     = os.environ["EMAIL_FROM"]
    password   = os.environ["EMAIL_APP_PASSWORD"]
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"Email sent to {recipients}")


# ── Week resolution ───────────────────────────────────────────────────────────

def resolve_week_start():
    """Return week_start as a UTC midnight datetime snapped to Monday."""
    week_start_str = os.environ.get("WEEK_START", "").strip()
    if week_start_str:
        return datetime.strptime(week_start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc)
    last_monday = today - timedelta(days=today.weekday() + 7)
    return last_monday.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_weekly(token, club_id, db):
    week_start = resolve_week_start()
    week_end   = week_start + timedelta(days=7)
    week_key   = week_start.strftime("%Y-%m-%d")

    existing = find_record(db, week_key)
    if existing:
        print(f"Found {week_key} in DB — skipping Strava fetch.")
        record = existing
    else:
        print(f"Fetching from Strava for week of {week_key}...")
        runs = fetch_club_activities(token, club_id, int(week_start.timestamp()))
        print(f"Found {len(runs)} runs.")
        stats  = build_stats(runs)
        record = stats_to_record(stats, week_start, week_end) if stats else {
            "week_start":  week_key,
            "week_end":    week_end.strftime("%Y-%m-%d"),
            "label":       f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}",
            "total_runs":  0, "total_miles": 0, "participants": 0, "leaderboard": [],
        }
        db = upsert_record(db, record)
        save_db(db)

    html = wrap_html(f"Run Club Recap — {record['label']}", format_week_section(record))
    send_email(f"Run Club Recap — {record['label']}", html)
    return db


def run_omnibus(token, club_id, db, num_weeks=6):
    today = datetime.now(timezone.utc)
    this_monday = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    weeks_needed = []
    for w in range(1, num_weeks + 1):
        week_start = this_monday - timedelta(weeks=w)
        week_end   = week_start + timedelta(weeks=1)
        weeks_needed.append((week_start, week_end))

    # Identify which weeks are missing from DB and need a Strava fetch
    missing = [(ws, we) for ws, we in weeks_needed
               if not find_record(db, ws.strftime("%Y-%m-%d"))]

    if missing:
        print(f"{len(missing)} weeks missing from DB — fetching from Strava...")
        # Cumulative subtraction hack for missing weeks
        oldest = min(ws for ws, _ in missing)
        cumulative = []
        for w in range(1, num_weeks + 1):
            week_end   = this_monday - timedelta(weeks=w - 1)
            week_start = this_monday - timedelta(weeks=w)
            week_key   = week_start.strftime("%Y-%m-%d")

            if find_record(db, week_key):
                # Already in DB; reset cumulative since we can't subtract across a gap
                cumulative = []
                continue

            batch      = fetch_club_activities(token, club_id, int(week_start.timestamp()))
            week_runs  = subtract_activities(batch, cumulative)
            cumulative = batch
            stats      = build_stats(week_runs)
            label      = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
            record = stats_to_record(stats, week_start, week_end) if stats else {
                "week_start": week_key,
                "week_end":   week_end.strftime("%Y-%m-%d"),
                "label":      label,
                "total_runs": 0, "total_miles": 0, "participants": 0, "leaderboard": [],
            }
            db = upsert_record(db, record)
            print(f"  {label}: {len(week_runs)} runs")
            time.sleep(0.5)

        save_db(db)

    # Build email from DB records newest-first
    sections = ""
    for week_start, week_end in weeks_needed:
        record = find_record(db, week_start.strftime("%Y-%m-%d"))
        sections += format_week_section(record) if record else \
            f'<h3 style="color:#fc4c02">{week_start.strftime("%b %d")} – {week_end.strftime("%b %d, %Y")}</h3>' \
            f'<p style="color:#888;margin-bottom:24px">No data (outside Strava history).</p>'

    oldest_label = weeks_needed[-1][0].strftime("%b %d")
    newest_label = weeks_needed[0][1].strftime("%b %d, %Y")
    title        = f"Run Club Omnibus — {oldest_label} – {newest_label}"
    send_email(title, wrap_html(title, sections))
    return db


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    club_id = os.environ["STRAVA_CLUB_ID"]

    print("Fetching Strava token...")
    token = get_access_token()
    db    = load_db()
    print(f"Loaded {len(db)} weeks from DB.")

    if os.environ.get("OMNIBUS", "").strip().lower() == "true":
        num_weeks = int(os.environ.get("OMNIBUS_WEEKS", "6"))
        run_omnibus(token, club_id, db, num_weeks)
    else:
        run_weekly(token, club_id, db)


if __name__ == "__main__":
    main()
