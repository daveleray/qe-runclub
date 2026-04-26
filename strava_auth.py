"""
Run this ONCE locally to get your Strava refresh token.
It will print the tokens you need to add as GitHub Secrets.

Usage:
    pip install requests
    python strava_auth.py
"""

import http.server
import threading
import urllib.parse
import webbrowser
import requests

CLIENT_ID = input("Strava Client ID: ").strip()
CLIENT_SECRET = input("Strava Client Secret: ").strip()

auth_code = None

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Got it! You can close this tab.</h2>")

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("localhost", 8080), Handler)
thread = threading.Thread(target=server.handle_request)
thread.start()

auth_url = (
    f"https://www.strava.com/oauth/authorize"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri=http://localhost:8080"
    f"&response_type=code"
    f"&scope=read,activity:read"
)
print("\nOpening browser for Strava authorization...")
webbrowser.open(auth_url)
thread.join()

resp = requests.post("https://www.strava.com/oauth/token", data={
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": auth_code,
    "grant_type": "authorization_code",
})
resp.raise_for_status()
data = resp.json()

print("\n--- Add these as GitHub Secrets ---")
print(f"STRAVA_CLIENT_ID:     {CLIENT_ID}")
print(f"STRAVA_CLIENT_SECRET: {CLIENT_SECRET}")
print(f"STRAVA_REFRESH_TOKEN: {data['refresh_token']}")
print("-----------------------------------\n")
