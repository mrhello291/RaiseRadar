"""Approve from your phone: a push notification (ntfy) with Send / Edit in Gmail / Skip buttons.

How it works, with no open port on your computer:
  1. The bot publishes a notification to your private topic. It shows the draft, why the role fits and the job post's
     requirements; tapping it opens the job post. Edit in Gmail opens your Gmail drafts (see gmailbox.py).
  2. A button tap makes your phone POST a short signed message to a second, private "reply" topic.
  3. The next `outreach run` reads that topic and acts only on messages whose signature is valid.

The signature is an HMAC of (draft id, action, hash of the exact text and recipient), keyed with a secret that only
this computer knows. Knowing a topic name is therefore not enough to approve anything, and editing a draft after
the notification went out invalidates the old buttons.

Privacy: the notification contains the draft text, so it passes through the ntfy server. Use random topic names
(`outreach setup` makes them) or point `[outreach] ntfy_server` at your own ntfy instance.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx

ACTIONS = ("approve", "skip")


def content_hash(to_email: str, subject: str, body: str) -> str:
    return hashlib.sha256(f"{to_email}\n{subject}\n{body}".encode("utf-8")).hexdigest()[:16]


def sign(secret: str, draft_id: int, action: str, chash: str) -> str:
    return hmac.new(secret.encode(), f"{draft_id}:{action}:{chash}".encode(), hashlib.sha256).hexdigest()[:24]


def parse_decision(message: str, secret: str, drafts: dict[int, str]) -> tuple[int, str] | None:
    """`approve:12:<sig>` -> (12, 'approve') if the signature matches that draft's current content, else None.
    `drafts` maps draft id -> current content hash."""
    parts = (message or "").strip().split(":")
    if len(parts) != 3:
        return None
    try:
        action, did, sig = parts[0], int(parts[1]), parts[2]
    except (ValueError, IndexError):
        return None
    if action not in ACTIONS or did not in drafts:
        return None
    return (did, action) if hmac.compare_digest(sig, sign(secret, did, action, drafts[did])) else None


MAX_BYTES = 3900           # ntfy turns a longer message into an attachment


def clip_bytes(text: str, limit: int = MAX_BYTES) -> str:
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:limit - 4].decode("utf-8", "ignore").rstrip() + " ..."


def approval_text(draft: dict, facts: dict | None = None, edit_url: str | None = None) -> str:
    """What the notification says: who, why it fits, the email itself, then the job post's own words to check it against."""
    f = facts or {}
    head = f"To: {draft['to_email']}\nRole: {draft['role_title']} at {draft['company']} (fit {draft['score']:.2f})"
    why = []
    if f.get("skills"):
        why.append("Your skills it asks for: " + ", ".join(f["skills"]))
    if f.get("project"):
        why.append(f"Lead project: {f['project']}" + (f" (matches {', '.join(f['project_hits'])})" if f.get("project_hits") else ""))
    if f.get("exp_min") is not None:
        why.append(f"Asks for {f['exp_min']:g}+ years" + (f" (you: {f['years']:g})" if f.get("years") is not None else ""))
    if f.get("contact_source"):
        why.append(f"Address found on: {f['contact_source']}")
    edit = ("\nTo change it: Edit in Gmail, fix it, send it from Gmail. The bot then sees it in Sent and will not send its own copy."
            if edit_url else "")
    jd = f"\n\n--- From the job post ---\n{f['jd']}" if f.get("jd") else ""
    link = f"\n(Tap this notification to open the job post.)" if draft.get("job_url") else ""
    return clip_bytes(f"{head}\n" + "\n".join(why) + f"{link}{edit}\n\nSubject: {draft['subject']}\n\n{draft['body']}{jd}")


def publish(server: str, topic: str, reply_topic: str, secret: str, draft: dict, client: httpx.Client | None = None,
            facts: dict | None = None, edit_url: str | None = None) -> bool:
    """Send the approval request. True if the server accepted it.
    Buttons: Send and Skip (signed taps), plus Edit in Gmail when a copy of the draft is in your Gmail Drafts."""
    chash = content_hash(draft["to_email"], draft["subject"], draft["body"])
    base = server.rstrip("/")
    tap = lambda label, action: {"action": "http", "label": label, "url": f"{base}/{reply_topic}", "method": "POST",
                                 "body": f"{action}:{draft['id']}:{sign(secret, draft['id'], action, chash)}", "clear": True}
    actions = [tap("Send", "approve")]
    if edit_url:
        actions.append({"action": "view", "label": "Edit in Gmail", "url": edit_url})
    actions.append(tap("Skip", "skip"))
    payload = {"topic": topic, "title": f"Send to {draft['company']}?", "message": approval_text(draft, facts, edit_url),
               "priority": 3, "tags": ["email"], "actions": actions}
    if draft.get("job_url", "").startswith(("https://", "http://")):
        payload["click"] = draft["job_url"]
    c = client or httpx.Client(timeout=20)
    try:
        r = c.post(base, json=payload)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def publish_text(server: str, topic: str, title: str, message: str, client: httpx.Client | None = None) -> bool:
    """A plain notification (no buttons). True if the server accepted it."""
    c = client or httpx.Client(timeout=20)
    try:
        r = c.post(server.rstrip("/"), json={"topic": topic, "title": title, "message": clip_bytes(message), "priority": 3, "tags": ["link"]})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def poll(server: str, reply_topic: str, since: str = "all", client: httpx.Client | None = None) -> tuple[list[dict], str]:
    """New messages on the reply topic: ([{id, message}], cursor to pass as `since` next time)."""
    c = client or httpx.Client(timeout=20)
    try:
        r = c.get(f"{server.rstrip('/')}/{reply_topic}/json", params={"poll": "1", "since": since})
    except httpx.HTTPError:
        return [], since
    if r.status_code != 200:
        return [], since
    out, cursor = [], since
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") == "message":
            out.append({"id": m.get("id", ""), "message": m.get("message", "")})
            cursor = m.get("id") or cursor
    return out, cursor
