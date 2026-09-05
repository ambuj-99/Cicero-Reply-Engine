#!/usr/bin/env python3
"""
authorize.py -- run this ONCE to connect Gmail and Google Calendar.

Before running, you need a file called credentials.json. Getting it:

  1. Go to console.cloud.google.com and create a project (any name).
  2. APIs & Services > Library. Enable "Gmail API" and "Google Calendar API".
  3. APIs & Services > OAuth consent screen. Choose "External", fill in the
     three required fields, and add YOUR OWN email under "Test users".
     Do not submit for verification -- you do not need it for a prototype.
  4. APIs & Services > Credentials > Create credentials > OAuth client ID.
     Application type: "Desktop app".
  5. Download the JSON, rename it credentials.json, put it beside this file.

Then:  python authorize.py

A browser opens, you approve, and two token files are written. They are
long-lived, so you only do this once.

SCOPES: deliberately minimal. gmail.modify lets us read and send but NOT
delete. calendar.events lets us manage events but not read your settings or
other calendars. Ask for the least you need -- it is both safer and much
easier to justify in a security review.
"""
import os, sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]

def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Run:  pip install -r requirements.txt")
        sys.exit(1)

    if not os.path.exists("credentials.json"):
        print("credentials.json is missing. Read the notes at the top of this file.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    for path in ("token_gmail.json", "token_calendar.json"):
        with open(path, "w") as f:
            f.write(creds.to_json())
        os.chmod(path, 0o600)          # tokens are credentials; lock them down
        print(f"wrote {path}")

    print("\nDone. Now run:  python run.py --live")
    print("Add --send only when you are ready for real mail to leave.")

if __name__ == "__main__":
    main()
