"""
Weekly run club stats emailer.
Pulls last 7 days of activities from a Strava club and emails a summary.

Required env vars:
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
    STRAVA_CLUB_ID
    EMAIL_FROM, EMAIL_TO (comma-separated), EMAIL_APP_PASSWORD
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


def fetch_club_activities(token, club_id):
    """Fetch club activities from the past 7 days."""
    headers = {"Authorization": f"Bearer {token}"}
    after = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
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
    """Returns min/mile string like 9:32."""
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
            "name":       name,
            "runs":       len(athlete_runs),
            "miles":      meters_to_miles(total_dist),
            "avg_pace":   seconds_to_pace(avg_pace),
            "total_time": total_time,
        })

    leaderboard.sort(key=lambda x: x["miles"], reverse=True)

    return {
        "total_runs":  len(runs),
        "total_miles": meters_to_miles(sum(r["distance"] for r in runs)),
        "participants": len(by_athlete),
        "leaderboard": leaderboard,
    }


# ── Email ─────────────────────────────────────────────────────────────────────

def format_html(stats, week_label):
    rows = ""
    medal = ["🥇", "🥈", "🥉"]
    for i, athlete in enumerate(stats["leaderboard"]):
        rank = medal[i] if i < 3 else f"{i + 1}"
        rows += f"""
        <tr style="background:{'#f9f9f9' if i % 2 else '#fff'}">
            <td style="padding:8px 12px">{rank}</td>
            <td style="padding:8px 12px">{athlete['name']}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['runs']}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['miles']:.1f}</td>
            <td style="padding:8px 12px;text-align:center">{athlete['avg_pace']}</td>
        </tr>"""

    return f"""
<html><body style="font-family:sans-serif;color:#222;max-width:600px;margin:auto">
  <h2 style="color:#fc4c02">🏃 Run Club Weekly Recap — {week_label}</h2>
  <p style="font-size:1.1em">
    <strong>{stats['total_runs']}</strong> runs &nbsp;·&nbsp;
    <strong>{stats['total_miles']:.1f} miles</strong> total &nbsp;·&nbsp;
    <strong>{stats['participants']}</strong> athletes
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:0.95em">
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
  </table>
  <p style="font-size:0.8em;color:#888;margin-top:24px">
    Powered by Strava · Data covers the past 7 days
  </p>
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    club_id = os.environ["STRAVA_CLUB_ID"]

    print("Fetching Strava token...")
    token = get_access_token()

    print(f"Fetching activities for club {club_id}...")
    week_runs = fetch_club_activities(token, club_id)
    print(f"Found {len(week_runs)} runs this week.")

    week_label = datetime.now().strftime("Week of %b %d, %Y")

    if not week_runs:
        print("No runs this week — sending a nudge email.")
        html = f"""
        <html><body style="font-family:sans-serif;color:#222;max-width:600px;margin:auto">
          <h2 style="color:#fc4c02">🏃 Run Club Weekly Recap — {week_label}</h2>
          <p>No runs logged this week. Get out there! 👟</p>
        </body></html>"""
        send_email(f"Run Club Recap — {week_label}", html)
        return

    stats = build_stats(week_runs)
    html  = format_html(stats, week_label)
    send_email(f"Run Club Recap — {week_label}", html)


if __name__ == "__main__":
    main()
