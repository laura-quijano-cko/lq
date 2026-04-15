"""
LQ BOT — Meeting Action Item Extractor & Slack Notifier
Scans Google Calendar for recent meetings, reads transcripts from Google Drive,
extracts action items using Claude AI, and posts a daily digest to Slack.
"""

import os
import json
import re
from datetime import datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import pickle

# ── Config ────────────────────────────────────────────────────────────────────
YOUR_EMAIL        = "laura.quijano@checkout.com"
SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]       # set in GitHub secrets
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]     # set in GitHub secrets
LOOKBACK_HOURS    = 24                                   # how far back to scan
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Google Auth ───────────────────────────────────────────────────────────────
def get_google_credentials():
    creds = None
    token_path = "token.pickle"

    if os.path.exists(token_path):
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # First-time setup: opens browser to authorise
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return creds

# ── Step 1: Get recent meetings you attended ──────────────────────────────────
def get_recent_meetings(calendar_service):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)

    result = calendar_service.events().list(
        calendarId="primary",
        timeMin=since.isoformat(),
        timeMax=now.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    meetings = []
    for event in result.get("items", []):
        # Skip blocks and events with no attendees (solo events)
        if event.get("summary", "").upper() == "BLOCK":
            continue
        attendees = event.get("attendees", [])
        if not attendees:
            continue
        # Confirm you actually attended (accepted or no response = you were there)
        you = next((a for a in attendees if a.get("self")), None)
        if you and you.get("responseStatus") == "declined":
            continue
        meetings.append(event)

    return meetings

# ── Step 2: Find transcript in Google Drive for a given meeting ───────────────
def find_transcript(drive_service, event):
    meeting_title = event.get("summary", "")
    start = event.get("start", {}).get("dateTime", "")[:10]  # YYYY-MM-DD

    # Google Meet transcripts are saved as Docs named: "<meeting title> - Transcript"
    query = f"name contains 'Transcript' and name contains '{meeting_title[:30]}' and mimeType='application/vnd.google-apps.document'"

    results = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, createdTime)",
        orderBy="createdTime desc",
    ).execute()

    files = results.get("files", [])
    if not files:
        return None

    # Pick the file closest to the meeting date
    for f in files:
        if start in f.get("createdTime", ""):
            return f
    return files[0]  # fallback to most recent match

# ── Step 3: Read transcript text from a Drive Doc ────────────────────────────
def read_transcript(drive_service, file_id):
    content = drive_service.files().export(
        fileId=file_id,
        mimeType="text/plain"
    ).execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content

# ── Step 4: Extract action items using Claude ─────────────────────────────────
def extract_actions(transcript_text, meeting_title, your_email):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are an expert meeting assistant. Analyse the following meeting transcript and extract ALL action items.

Meeting: {meeting_title}
User's email: {your_email}

For each action item, identify:
1. What the action is
2. Who it is assigned to (name and/or email if mentioned)
3. Whether it is assigned to the user ({your_email}) — true/false
4. Any deadline mentioned (or null)

Respond ONLY with a valid JSON array. No preamble, no markdown. Example format:
[
  {{
    "action": "Send updated pricing deck to Visa team",
    "assigned_to": "Laura Quijano",
    "assigned_to_me": true,
    "deadline": "2026-04-18"
  }},
  {{
    "action": "Share API documentation",
    "assigned_to": "Jack Stannard",
    "assigned_to_me": false,
    "deadline": null
  }}
]

If there are no action items, return an empty array: []

TRANSCRIPT:
{transcript_text[:8000]}
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)

# ── Step 5: Post daily digest to Slack ───────────────────────────────────────
def post_slack_digest(actions_by_meeting):
    slack = WebClient(token=SLACK_BOT_TOKEN)

    # Get your own Slack user ID
    identity = slack.auth_test()
    user_id = identity["user_id"]

    total_mine = sum(
        1 for items in actions_by_meeting.values()
        for a in items if a["assigned_to_me"]
    )
    total_others = sum(
        1 for items in actions_by_meeting.values()
        for a in items if not a["assigned_to_me"]
    )

    today = datetime.now().strftime("%A %d %B %Y")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🤖 LQ BOT — Action Items for {today}"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{total_mine}* action(s) assigned to you · *{total_others}* assigned to others in your meetings"
            }
        },
        {"type": "divider"}
    ]

    if not actions_by_meeting:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ No action items found in recent meetings. Enjoy your day!"}
        })
    else:
        for meeting, actions in actions_by_meeting.items():
            if not actions:
                continue

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📅 {meeting}*"}
            })

            mine = [a for a in actions if a["assigned_to_me"]]
            others = [a for a in actions if not a["assigned_to_me"]]

            if mine:
                my_lines = "\n".join(
                    f"  • {a['action']}" + (f" _(by {a['deadline']})_" if a.get("deadline") else "")
                    for a in mine
                )
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*🙋 Your actions:*\n{my_lines}"}
                })

            if others:
                other_lines = "\n".join(
                    f"  • *{a['assigned_to']}:* {a['action']}" + (f" _(by {a['deadline']})_" if a.get("deadline") else "")
                    for a in others
                )
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*👥 Others' actions:*\n{other_lines}"}
                })

            blocks.append({"type": "divider"})

    slack.chat_postMessage(
        channel=user_id,  # DM to yourself
        blocks=blocks,
        text=f"LQ BOT: {total_mine} action(s) assigned to you from recent meetings"
    )
    print(f"✅ Slack digest sent — {total_mine} actions assigned to you, {total_others} to others.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("🤖 LQ BOT starting...")
    creds = get_google_credentials()
    calendar_service = build("calendar", "v3", credentials=creds)
    drive_service    = build("drive",    "v3", credentials=creds)

    meetings = get_recent_meetings(calendar_service)
    print(f"📅 Found {len(meetings)} meeting(s) in the last {LOOKBACK_HOURS} hours.")

    actions_by_meeting = {}

    for event in meetings:
        title = event.get("summary", "Untitled Meeting")
        print(f"\n🔍 Processing: {title}")

        transcript_file = find_transcript(drive_service, event)
        if not transcript_file:
            print(f"   ⚠️  No transcript found — skipping.")
            continue

        print(f"   📄 Transcript found: {transcript_file['name']}")
        transcript_text = read_transcript(drive_service, transcript_file["id"])
        actions = extract_actions(transcript_text, title, YOUR_EMAIL)
        print(f"   ✅ {len(actions)} action(s) extracted.")

        if actions:
            actions_by_meeting[title] = actions

    post_slack_digest(actions_by_meeting)

if __name__ == "__main__":
    main()
