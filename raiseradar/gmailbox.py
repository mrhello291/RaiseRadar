"""Your Gmail Drafts and Sent folders over IMAP, with the same app password as sending.

Used for two things only:
  * Editing: a copy of each draft is put in Gmail Drafts, so you can change it in the Gmail app and send it yourself.
  * Never sending twice: before the bot sends, it looks in Sent. If you already emailed that address, it does not.

It never reads other mail: every search is for one recipient address or for the bot's own marker header.
Copies the bot made are only removed while they are unchanged, so an edit of yours is never thrown away.
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime
from email.message import EmailMessage
from email.policy import default as default_policy

MARKER = "X-RaiseRadar-Draft"
DRAFTS, SENT, TRASH = "\\Drafts", "\\Sent", "\\Trash"
FALLBACK = {"\\Drafts": "[Gmail]/Drafts", "\\Sent": "[Gmail]/Sent Mail", "\\Trash": "[Gmail]/Trash"}


class MailboxError(Exception):
    pass


def marker_value(draft_id: int, chash: str) -> str:
    return f"rr-{draft_id}-{chash}"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", "")).strip()


def plain_body(msg) -> str:
    if isinstance(msg, bytes):
        msg = email.message_from_bytes(msg, policy=default_policy)
    part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    try:
        return part.get_content() if part is not None else ""
    except (KeyError, LookupError):
        return ""


def _q(s: str) -> str:
    if any(c in s for c in '"\\\r\n'):
        raise MailboxError("bad character in a search term")
    return f'"{s}"'


def _imap_date(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d.strftime("%d-%b-%Y")


class Gmailbox:
    def __init__(self, host: str, port: int, user: str, password: str, imap_factory=None):
        if not user or not password:
            raise MailboxError("OUTREACH_SMTP_USER / OUTREACH_SMTP_PASSWORD are not set")
        self.host, self.port, self.user, self.password = host, port, user, password
        self.factory = imap_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=30))
        self.c = None
        self._folders: dict[str, str] = {}

    def __enter__(self):
        try:
            self.c = self.factory(self.host, self.port)
            self.c.login(self.user, self.password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"Gmail IMAP login failed ({exc}); is IMAP on in Gmail settings?") from None
        except OSError as exc:
            raise MailboxError(f"cannot reach {self.host}: {exc}") from None
        return self

    def __exit__(self, *a):
        try:
            self.c.logout()
        except Exception:
            pass
        return False

    def _ok(self, res, what: str):
        typ, data = res
        if typ != "OK":
            raise MailboxError(f"{what} failed: {data!r}"[:200])
        return data

    def folder(self, flag: str) -> str:
        """The folder name for a special-use flag (\\Drafts, \\Sent, \\Trash); names differ by Gmail language."""
        if not self._folders:
            try:
                for line in self._ok(self.c.list(), "LIST"):
                    s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                    m = re.match(r'\((?P<flags>[^)]*)\) "[^"]*" (?P<name>.+)$', s)
                    if m:
                        for f in FALLBACK:
                            if f.lower() in m["flags"].lower():
                                self._folders[f] = m["name"].strip().strip('"')
            except MailboxError:
                pass
        return self._folders.get(flag) or FALLBACK[flag]

    def _select(self, flag: str, readonly: bool) -> None:
        self._ok(self.c.select(f'"{self.folder(flag)}"', readonly=readonly), f"opening {flag}")

    def _search(self, *criteria: str) -> list[bytes]:
        data = self._ok(self.c.uid("SEARCH", None, *criteria), "SEARCH")
        return (data[0] or b"").split() if data else []

    def _fetch(self, uid: bytes) -> bytes:
        data = self._ok(self.c.uid("FETCH", uid, "(BODY.PEEK[])"), "FETCH")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                return item[1]
        return b""

    # -- drafts ------------------------------------------------------------------------
    def save_draft(self, msg: EmailMessage, marker: str) -> None:
        del msg[MARKER]
        msg[MARKER] = marker
        self._ok(self.c.append(f'"{self.folder(DRAFTS)}"', "(\\Draft)", imaplib.Time2Internaldate(time.time()),
                               msg.as_bytes()), "saving the draft")

    def _drafts_to(self, to_email: str, readonly: bool) -> list[tuple[bytes, email.message.Message]]:
        self._select(DRAFTS, readonly=readonly)
        return [(u, email.message_from_bytes(self._fetch(u), policy=default_policy)) for u in self._search("TO", _q(to_email))]

    def draft_state(self, to_email: str, body: str) -> str:
        """'unchanged' (the bot's copy is there as it was), 'edited' (a draft to this address differs from the bot's text),
        or 'gone' (no draft to this address: sent from Gmail or deleted)."""
        drafts = self._drafts_to(to_email, readonly=True)
        if not drafts:
            return "gone"
        want = norm(body)
        return "unchanged" if all(norm(plain_body(m)) == want for _u, m in drafts) else "edited"

    def delete_draft(self, to_email: str, marker: str, body: str) -> int:
        """Move the bot's own copy to Trash, but only if you did not change it. Returns how many were moved."""
        want, moved = norm(body), 0
        for u, m in self._drafts_to(to_email, readonly=False):
            if (m.get(MARKER) or "").strip() != marker or norm(plain_body(m)) != want:
                continue
            typ, _ = self.c.uid("MOVE", u, f'"{self.folder(TRASH)}"')
            if typ != "OK":        # a server without MOVE
                self.c.uid("STORE", u, "+FLAGS", "(\\Deleted)")
                self.c.expunge()
            moved += 1
        return moved

    # -- sent --------------------------------------------------------------------------
    def sent_to(self, to_email: str, since_iso: str | None = None) -> int:
        """How many messages in Sent are addressed to this address (optionally on or after a date)."""
        self._select(SENT, readonly=True)
        crit = ["TO", _q(to_email)] + (["SINCE", _imap_date(since_iso)] if since_iso else [])
        return len(self._search(*crit))


def check_login(host: str, port: int, user: str, password: str, imap_factory=None) -> str:
    """Log in and find the Drafts folder, changing nothing. Returns its name."""
    with Gmailbox(host, port, user, password, imap_factory) as box:
        return box.folder(DRAFTS)
