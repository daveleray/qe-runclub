"""
Weekly run club stats emailer.
Pulls activities from a Strava club and emails a summary.

Required env vars:
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
    STRAVA_CLUB_ID
    EMAIL_FROM, EMAIL_TO (comma-separated), EMAIL_APP_PASSWORD

Optional env vars:
    WEEK_START   — Monday date (YYYY-MM-DD) to recap a specific past week
    OMNIBUS      — set to "true" to send a combined last-6-weeks recap
"""

import os
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests


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
    """Fetch club run activities since `after` (Unix timestamp)."""
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
            "miles":    meters_to_miles(total_dist),
            "avg_pace": seconds_to_pace(avg_pace),
        })

    leaderboard.sort(key=lambda x: x["miles"], reverse=True)

    return {
        "total_runs":  len(runs),
        "total_miles": meters_to_miles(sum(r["distance"] for r in runs)),
        "participants": len(by_athlete),
        "leaderboard": leaderboard,
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


def format_weekly_html(stats, label):
    if not stats:
        return f'<h3 style="color:#fc4c02">{label}</h3><p style="color:#888">No runs logged.</p>'
    return f"""
  <h3 style="color:#fc4c02;margin-bottom:4px">{label}</h3>
  <p style="margin:0 0 8px 0;font-size:0.95em">
    <strong>{stats['total_runs']}</strong> runs &nbsp;·&nbsp;
    <strong>{stats['total_miles']:.1f} mi</strong> total &nbsp;·&nbsp;
    <strong>{stats['participants']}</strong> athletes
  </p>
  {leaderboard_table(stats['leaderboard'])}"""


def wrap_html(title, body_content, footer):
    return f"""
<html><body style="font-family:sans-serif;color:#222;max-width:620px;margin:auto">
  <h2 style="color:#fc4c02">🏃 {title}</h2>
  {body_content}
  <p style="font-size:0.8em;color:#888;margin-top:24px">{footer}</p>
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


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_weekly(token, club_id):
    week_start_str = os.environ.get("WEEK_START", "").strip()
    if week_start_str:
        week_start = datetime.strptime(week_start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        week_start = datetime.now(timezone.utc) - timedelta(days=7)

    week_end   = week_start + timedelta(days=7)
    label      = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
    after      = int(week_start.timestamp())

    print(f"Fetching activities since {week_start.strftime('%Y-%m-%d')}...")
    runs = fetch_club_activities(token, club_id, after)
    print(f"Found {len(runs)} runs.")

    stats = build_stats(runs)
    if not stats:
        html = wrap_html(
            f"Run Club Recap — {label}",
            "<p>No runs logged this week. Get out there! 👟</p>",
            "Powered by Strava",
        )
    else:
        html = wrap_html(
            f"Run Club Recap — {label}",
            format_weekly_html(stats, label),
            "Powered by Strava · Data covers the selected week",
        )
    send_email(f"Run Club Recap — {label}", html)


def run_omnibus(token, club_id, num_weeks=6):
    today      = datetime.now(timezone.utc)
    # snap back to most recent Monday
    days_since_monday = today.weekday()
    this_monday = (today - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = this_monday - timedelta(weeks=num_weeks)
    after = int(start.timestamp())

    label = f"Last {num_weeks} Weeks — {start.strftime('%b %d')} to {this_monday.strftime('%b %d, %Y')}"
    print(f"Omnibus: fetching activities since {start.strftime('%Y-%m-%d')}...")
    runs = fetch_club_activities(token, club_id, after)
    print(f"Found {len(runs)} runs across {num_weeks} weeks.")

    stats = build_stats(runs)
    if not stats:
        html = wrap_html(
            f"Run Club Omnibus — {label}",
            "<p>No runs found in this period.</p>",
            "Powered by Strava",
        )
    else:
        html = wrap_html(
            f"Run Club Omnibus — {label}",
            format_weekly_html(stats, label),
            f"Powered by Strava · Combined stats for the last {num_weeks} weeks",
        )
    send_email(f"Run Club Omnibus — {label}", html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    club_id = os.environ["STRAVA_CLUB_ID"]

    print("Fetching Strava token...")
    token = get_access_token()

    if os.environ.get("OMNIBUS", "").strip().lower() == "true":
        run_omnibus(token, club_id)
    else:
        run_weekly(token, club_id)


if __name__ == "__main__":
    main()
