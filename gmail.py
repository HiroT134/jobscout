"""Read-only Gmail access for jobscout.

Only ever calls users.labels.list, users.messages.list, users.messages.get
and users.getProfile. Nothing here sends, deletes or modifies email.
"""

import base64
import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent
CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailError(Exception):
    pass


class SignInExpired(GmailError):
    """The saved refresh token was rejected (expired or revoked)."""


class NotSignedIn(GmailError):
    """No usable saved sign-in, and the caller does not allow a browser sign-in."""


def _check_token_scopes() -> None:
    """Refuse a cached token whose scopes are anything other than read-only."""
    granted = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("scopes") or []
    if set(granted) != set(SCOPES):
        raise GmailError(
            f"{TOKEN_FILE.name} has scopes {granted}, expected only gmail.readonly. "
            f"Delete {TOKEN_FILE.name} and run `python jobscout.py auth` again."
        )


def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        _check_token_scopes()
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            if not interactive:
                raise SignInExpired(
                    "Saved Gmail sign-in has expired or been revoked. "
                    "Run `python jobscout.py auth` to sign in again."
                ) from None
            creds = None  # fall through to a fresh sign-in

    if not (creds and creds.valid):
        if not interactive:
            raise NotSignedIn("Not authorised. Run `python jobscout.py auth` first.")
        if not CREDENTIALS_FILE.exists():
            raise GmailError(f"{CREDENTIALS_FILE.name} not found in {ROOT}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_service(interactive: bool = False):
    return build("gmail", "v1", credentials=get_credentials(interactive), cache_discovery=False)


def account_email(service) -> str:
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def label_id(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == name.lower():
            return label["id"]
    raise GmailError(f'Gmail label "{name}" not found')


def list_message_ids(
    service, label: str | None = None, query: str | None = None, limit: int | None = None
) -> list[str]:
    """Message IDs under the label and/or matching a Gmail search query, newest first."""
    params = {"userId": "me", "maxResults": 500}
    if label:
        params["labelIds"] = [label_id(service, label)]
    if query:
        params["q"] = query
    ids: list[str] = []
    page_token = None
    while True:
        resp = service.users().messages().list(pageToken=page_token, **params).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or (limit and len(ids) >= limit):
            break
    return ids[:limit] if limit else ids


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def get_headers(service, msg_id: str) -> dict:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["Subject", "From", "Date"])
        .execute()
    )
    h = _headers(msg["payload"])
    return {
        "id": msg["id"],
        "internal_date": int(msg["internalDate"]),
        "subject": h.get("subject", ""),
        "from": h.get("from", ""),
        "date": h.get("date", ""),
    }


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def _collect_bodies(part: dict, out: dict[str, list[str]]) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/html", "text/plain"):
        out[mime].append(_decode(data))
    for sub in part.get("parts", []):
        _collect_bodies(sub, out)


def get_message(service, msg_id: str) -> dict:
    """Full message with decoded html and plain-text bodies."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    h = _headers(msg["payload"])
    bodies: dict[str, list[str]] = {"text/html": [], "text/plain": []}
    _collect_bodies(msg["payload"], bodies)
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "internal_date": int(msg["internalDate"]),
        "subject": h.get("subject", ""),
        "from": h.get("from", ""),
        "date": h.get("date", ""),
        "html": "\n".join(bodies["text/html"]),
        "text": "\n".join(bodies["text/plain"]),
    }
