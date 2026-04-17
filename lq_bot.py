"""
LQ Updater — reads Google Calendar for the current week,
generates a fresh index.html and pushes it to GitHub Pages.
No Anthropic API needed.
"""

import os
import base64
import json
import pickle
import re
from datetime import datetime, timedelta, timezone
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests

# ── Config ────────────────────────────────────────────────────────────────────
YOUR_EMAIL      = "laura.quijano@checkout.com"
GITHUB_REPO     = "laura-quijano-cko/lq"
GITHUB_TOKEN    = os.environ["PAT_TOKEN"]
GITHUB_BRANCH   = "main"
TZ              = "Europe/London"

# ── Google Auth ───────────────────────────────────────────────────────────────
def get_calendar_service():
    token_b64 = os.environ["GOOGLE_TOKEN_B64"]
    token_bytes = base64.b64decode(token_b64)
    creds = pickle.loads(token_bytes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)

# ── Get this week's accepted meetings (Mon–Fri, 9–18) ─────────────────────────
def get_week_meetings(service):
    # Find Monday of current week
    today = datetime.now(timezone.utc)
    mon = today - timedelta(days=today.weekday())
    mon = mon.replace(hour=0, minute=0, second=0, microsecond=0)
    fri = mon + timedelta(days=4, hours=23, minutes=59)

    result = service.events().list(
        calendarId="primary",
        timeMin=mon.isoformat(),
        timeMax=fri.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        timeZone=TZ,
    ).execute()

    SKIP = {"BLOCK", "LUNCH", "WOD", "INTERVIEW BLOCK", "HOLD"}
    meetings = {}

    for ev in result.get("items", []):
        title = ev.get("summary", "").strip()

        # Skip blocks, lunch, personal holds
        if any(s in title.upper() for s in SKIP):
            continue

        # Must have a dateTime (not all-day)
        start = ev.get("start", {}).get("dateTime")
        end   = ev.get("end",   {}).get("dateTime")
        if not start or not end:
            continue

        # Only accepted or organised by me
        attendees = ev.get("attendees", [])
        me = next((a for a in attendees if a.get("self")), None)
        organiser = ev.get("organizer", {}).get("email", "")
        if me:
            if me.get("responseStatus") not in ("accepted", "tentative"):
                continue
        elif organiser != YOUR_EMAIL:
            # No attendees list but I organised it = include
            if attendees:
                continue

        # Only events between 9-18
        s_dt = datetime.fromisoformat(start)
        e_dt = datetime.fromisoformat(end)
        if s_dt.hour < 9 or s_dt.hour >= 18:
            continue

        date_str = s_dt.strftime("%Y-%m-%d")
        if date_str not in meetings:
            meetings[date_str] = []

        meetings[date_str].append({
            "s":  s_dt.strftime("%H:%M"),
            "e":  e_dt.strftime("%H:%M"),
            "t":  title,
            "id": ev.get("id", "")[:16].replace("-",""),
        })

    return meetings, mon

# ── Generate index.html ───────────────────────────────────────────────────────
def generate_html(meetings_by_date, week_start):
    dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]

    # Build JS-safe meetings object
    meet_js = "{\n"
    for ds in dates:
        mts = meetings_by_date.get(ds, [])
        meet_js += f"  '{ds}': [\n"
        for m in mts:
            t = m['t'].replace("'", "\\'")
            meet_js += f"    {{s:'{m['s']}',e:'{m['e']}',t:'{t}',id:'{m['id']}'}},\n"
        meet_js += "  ],\n"
    meet_js += "}"

    dates_js = str(dates).replace('"', "'")
    week_label = f"{week_start.strftime('%d %b')} – {(week_start + timedelta(days=4)).strftime('%d %b %Y')}"

    # Read the template and inject the data
    html = HTML_TEMPLATE \
        .replace("%%MEETINGS%%", meet_js) \
        .replace("%%DATES%%", dates_js) \
        .replace("%%WEEK_LABEL%%", week_label)

    return html

# ── Push to GitHub ────────────────────────────────────────────────────────────
def push_to_github(html_content):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/index.html"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get current file SHA (needed to update)
    r = requests.get(url, headers=headers)
    sha = r.json().get("sha", "")

    # Push new content
    payload = {
        "message": f"LQ update: week of {datetime.now().strftime('%Y-%m-%d')}",
        "content": base64.b64encode(html_content.encode()).decode(),
        "branch": GITHUB_BRANCH,
        "sha": sha,
    }

    r = requests.put(url, headers=headers, json=payload)
    if r.status_code in (200, 201):
        print(f"✅ index.html updated successfully.")
    else:
        print(f"❌ Failed: {r.status_code} {r.text}")
        raise Exception("GitHub push failed")

# ── HTML Template (injected with real data) ───────────────────────────────────
# This is the full dashboard HTML with %%MEETINGS%%, %%DATES%%, %%WEEK_LABEL%%
# placeholders replaced at runtime.
HTML_TEMPLATE = """%%HTML%%"""

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("🗓 LQ Updater starting...")
    service = get_calendar_service()
    meetings, week_start = get_week_meetings(service)

    total = sum(len(v) for v in meetings.values())
    print(f"📅 Found {total} accepted meetings for week of {week_start.strftime('%d %b %Y')}")

    html = generate_html(meetings, week_start)
    push_to_github(html)
    print("🎉 Done — site will update in ~60 seconds.")

if __name__ == "__main__":
    main()
