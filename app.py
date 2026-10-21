from __future__ import annotations

import html

import json

import os

import random

from datetime import datetime, timedelta, timezone

from pathlib import Path



from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    ChatMember,
)

from telegram.ext import (

    Application,

    CommandHandler,

    CallbackQueryHandler,

    ChatMemberHandler,

    ChatJoinRequestHandler,

    ContextTypes,

    MessageHandler,

    filters,

)

from telegram.error import TelegramError


# --------------------------------------------------
# MEMBERSHIP STORAGE (SQLite) — separate from poll_data.json
# --------------------------------------------------
# Saves join/leave events in their own SQLite file. Never touches poll data.

import re
import sqlite3
from typing import Optional

# Telegram member statuses (same strings as telegram.ChatMember constants).
MEMBER_STATUSES = {"creator", "administrator", "member"}
RESTRICTED = "restricted"
EVENT_TYPES = {"join", "leave", "status_change"}


def is_present(status: Optional[str], is_member: Optional[bool] = None) -> bool:
    """True if a user with this status is currently inside the group.

    A "restricted" user may or may not be in the group, so Telegram's
    is_member flag decides that case.
    """
    if status in MEMBER_STATUSES:
        return True
    if status == RESTRICTED:
        return bool(is_member)
    return False  # "left", "kicked" or unknown


def classify_membership_change(
    old_status: Optional[str], old_is_member: Optional[bool],
    new_status: Optional[str], new_is_member: Optional[bool],
) -> Optional[str]:
    """Return "join", "leave", "status_change", or None for no real change."""
    was_in = is_present(old_status, old_is_member)
    now_in = is_present(new_status, new_is_member)
    if not was_in and now_in:
        return "join"
    if was_in and not now_in:
        return "leave"
    if old_status != new_status:
        return "status_change"  # e.g. promoted to admin, restricted, banned after leaving
    return None


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def normalize_username(raw) -> Optional[str]:
    """Turn '@Name', 'Name', 't.me/Name' or 'https://t.me/Name' into 'name'.

    Returns None if it does not look like a Telegram username.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    for prefix in ("https://", "http://", "www."):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        if text.lower().startswith(host):
            text = text[len(host):]
    text = text.lstrip("@").split("/")[0].split("?")[0].strip()
    return text.lower() if USERNAME_RE.match(text) else None


class MembershipStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS membership_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('join','leave','status_change')),
                    old_status TEXT,
                    new_status TEXT,
                    occurred_at_utc TEXT NOT NULL,
                    update_key TEXT UNIQUE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_membership_chat_time ON membership_events(chat_id, occurred_at_utc)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_membership_user_time ON membership_events(user_id, occurred_at_utc)")
            # Approved scholar usernames (lowercase, no @), e.g. from the Google Form sheet.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS allowed_usernames (
                    username TEXT PRIMARY KEY,
                    source TEXT,
                    added_at_utc TEXT NOT NULL
                )
            """)
            # Join requests the bot has seen. One row per (group, user).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS join_requests (
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT,
                    requested_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decided_by TEXT,
                    decided_at_utc TEXT,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)

    def record_event(
        self, *, chat_id: int, chat_title: str, user_id: int,
        username: Optional[str], display_name: str, event_type: str,
        old_status: Optional[str] = None, new_status: Optional[str] = None,
        occurred_at: Optional[datetime] = None, update_key: Optional[str] = None,
    ) -> bool:
        """Record one event. Returns False for a duplicate update_key."""
        if event_type not in EVENT_TYPES:
            raise ValueError("event_type must be join, leave, or status_change")
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        timestamp_utc = timestamp.astimezone(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO membership_events
                    (chat_id, chat_title, user_id, username, display_name, event_type,
                     old_status, new_status, occurred_at_utc, update_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (chat_id, chat_title, user_id, username, display_name, event_type,
                      old_status, new_status, timestamp_utc, update_key))
            return True
        except sqlite3.IntegrityError as exc:
            if update_key and "update_key" in str(exc):
                return False
            raise

    def recent_events(
        self, *, limit: int = 50, chat_id: Optional[int] = None,
        event_type: Optional[str] = None,
    ) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses, params = [], []
        if chat_id is not None:
            clauses.append("chat_id = ?"); params.append(chat_id)
        if event_type is not None:
            if event_type not in EVENT_TYPES:
                raise ValueError("invalid event_type")
            clauses.append("event_type = ?"); params.append(event_type)
        sql = "SELECT * FROM membership_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at_utc DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def count_events(self, *, chat_id: Optional[int] = None, event_type: Optional[str] = None) -> int:
        clauses, params = [], []
        if chat_id is not None:
            clauses.append("chat_id = ?"); params.append(chat_id)
        if event_type is not None:
            if event_type not in EVENT_TYPES:
                raise ValueError("invalid event_type")
            clauses.append("event_type = ?"); params.append(event_type)
        sql = "SELECT COUNT(*) FROM membership_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def stats_by_chat(self, *, since: Optional[datetime] = None) -> list[dict]:
        """Join/leave counts per group, optionally only events after `since`."""
        params: list[object] = []
        where = ""
        if since is not None:
            if since.tzinfo is None:
                raise ValueError("since must be timezone-aware")
            where = "WHERE occurred_at_utc >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        sql = f"""
            SELECT chat_id,
                   (SELECT chat_title FROM membership_events e2
                     WHERE e2.chat_id = e.chat_id
                     ORDER BY occurred_at_utc DESC, id DESC LIMIT 1) AS chat_title,
                   SUM(event_type = 'join')  AS joins,
                   SUM(event_type = 'leave') AS leaves
            FROM membership_events e
            {where}
            GROUP BY chat_id
            ORDER BY chat_title
        """
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def first_event_time(self) -> Optional[str]:
        """UTC ISO time of the oldest stored event (when tracking effectively began)."""
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(occurred_at_utc) FROM membership_events").fetchone()
            return row[0] if row else None

    # ---------------- allowlist ----------------

    def add_usernames(self, usernames, source: str = "manual") -> tuple[int, int]:
        """Add normalized usernames. Returns (newly_added, already_present)."""
        now = datetime.now(timezone.utc).isoformat()
        added = existing = 0
        with self._connect() as conn:
            for name in dict.fromkeys(usernames):  # keep order, drop repeats
                cur = conn.execute(
                    "INSERT OR IGNORE INTO allowed_usernames (username, source, added_at_utc) VALUES (?, ?, ?)",
                    (name, source, now),
                )
                if cur.rowcount:
                    added += 1
                else:
                    existing += 1
        return added, existing

    def remove_username(self, username: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM allowed_usernames WHERE username = ?", (username,)).rowcount > 0

    def is_allowed(self, username: Optional[str]) -> bool:
        name = normalize_username(username)
        if not name:
            return False
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM allowed_usernames WHERE username = ?", (name,)).fetchone() is not None

    def list_usernames(self) -> list[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT username FROM allowed_usernames ORDER BY username")]

    # ---------------- join requests ----------------

    def save_join_request(self, *, chat_id: int, chat_title: str, user_id: int,
                          username: Optional[str], display_name: str, requested_at: datetime) -> None:
        """Store (or refresh) a request as pending."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO join_requests (chat_id, chat_title, user_id, username, display_name,
                                           requested_at_utc, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    chat_title = excluded.chat_title, username = excluded.username,
                    display_name = excluded.display_name, requested_at_utc = excluded.requested_at_utc,
                    status = 'pending', decided_by = NULL, decided_at_utc = NULL
            """, (chat_id, chat_title, user_id, normalize_username(username), display_name,
                  requested_at.astimezone(timezone.utc).isoformat()))

    def set_request_status(self, chat_id: int, user_id: int, status: str, decided_by: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE join_requests SET status = ?, decided_by = ?, decided_at_utc = ?
                WHERE chat_id = ? AND user_id = ?
            """, (status, decided_by, datetime.now(timezone.utc).isoformat(), chat_id, user_id))

    def get_join_request(self, chat_id: int, user_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM join_requests WHERE chat_id = ? AND user_id = ?",
                               (chat_id, user_id)).fetchone()
            return dict(row) if row else None

    def pending_requests(self, *, usernames=None, user_id: Optional[int] = None,
                         limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM join_requests WHERE status = 'pending'"
        params: list[object] = []
        if usernames is not None:
            names = list(usernames)
            if not names:
                return []
            sql += f" AND username IN ({','.join('?' * len(names))})"
            params += names
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY requested_at_utc LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]




# --------------------------------------------------

# SETTINGS

# --------------------------------------------------



load_dotenv()



BOT_TOKEN = os.getenv("BOT_TOKEN")



if not BOT_TOKEN:

    raise RuntimeError("BOT_TOKEN not found in .env file")


# Authorized bot-admin Telegram user IDs.
# Set ADMIN_USER_IDS in .env as comma-separated IDs.
ADMIN_USER_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ADMIN_USER_IDS", "").split(",")
    if user_id.strip().isdigit()
}

if not ADMIN_USER_IDS:
    print("WARNING: ADMIN_USER_IDS is not configured. Private admin commands will be unavailable.")




BASE_DIR = Path(__file__).resolve().parent

DATA_FILE = BASE_DIR / "poll_data.json"


# --------------------------------------------------
# MEMBERSHIP TRACKING SETTINGS
# --------------------------------------------------
# Groups to track, as comma-separated Telegram chat IDs (e.g. -1001234567890).
# Only these groups are recorded. Leave empty to disable tracking.
TRACKED_CHAT_IDS = {
    int(chat_id.strip())
    for chat_id in os.getenv("TRACKED_CHAT_IDS", "").split(",")
    if chat_id.strip().lstrip("-").isdigit()
}

# SQLite file for membership events. Kept separate from poll_data.json.
MEMBERSHIP_DB_PATH = Path(os.getenv("MEMBERSHIP_DB_PATH") or "membership_events.sqlite3")
if not MEMBERSHIP_DB_PATH.is_absolute():
    MEMBERSHIP_DB_PATH = BASE_DIR / MEMBERSHIP_DB_PATH

# Which events send a private alert to ADMIN_USER_IDS: "leave", "join", or "join,leave".
# Every event is stored either way; this only controls alerts.
MEMBERSHIP_ALERTS = {
    kind.strip().lower()
    for kind in os.getenv("MEMBERSHIP_ALERTS", "leave").split(",")
} & {"join", "leave"}


# --------------------------------------------------
# JOIN REQUEST AUTO-APPROVAL SETTINGS
# --------------------------------------------------
def parse_chat_ids(value: str) -> set:
    return {
        int(chat_id.strip())
        for chat_id in (value or "").split(",")
        if chat_id.strip().lstrip("-").isdigit()
    }


# Groups where the bot handles join requests. Empty = feature off.
APPROVAL_CHAT_IDS = parse_chat_ids(os.getenv("APPROVAL_CHAT_IDS", ""))

# Landing groups (Announcements + Scholar Introduction).
#  - In these groups: approve if the username is in the allowlist.
#  - In every other approval group: approve if the person is already in ALL of these.
MANDATORY_CHAT_IDS = parse_chat_ids(os.getenv("MANDATORY_CHAT_IDS", ""))



# Automatic polls: every 2 days at the same time.

POLL_INTERVAL = timedelta(days=2)



# Each poll accepts responses for 24 hours.

POLL_DURATION = timedelta(hours=24)



# Fresh schedule starts on 30 September 2026 at 6:30 PM IST.

IST = timezone(timedelta(hours=5, minutes=30))

FIRST_POLL_AT = datetime(2026, 9, 30, 18, 30, tzinfo=IST)



# --------------------------------------------------

# WELLBEING POLL POOL

# --------------------------------------------------



POLLS = [

    {"question": 'How are you feeling today? 💙', "options": ['😊 Great', '🙂 Good', '😐 Okay', '😔 Not great', '🫂 Need some support']},

    {"question": 'What helps you relax after a busy day? 🌿', "options": ['🎵 Music', '🚶 Walking', '📱 Entertainment', '😴 Rest', '💬 Talking to someone']},

    {"question": 'How would you rate your energy level today? ⚡', "options": ['⚡ Very high', '🙂 Good', '😐 Average', '😴 Low']},

    {"question": 'What usually helps you deal with stress? 🧘', "options": ['🎵 Music', '🏃 Exercise', '😴 Rest', '💬 Talking', '🧘 Quiet time']},

    {"question": 'How supported do you feel this week? 💙', "options": ['🫶 Very supported', '🙂 Supported', '😐 Neutral', '😔 Not much', '🫂 Need more support']},

    {"question": 'What is one thing that improves your mood? ☀️', "options": ['🎵 Music', '👥 Friends', '🏃 Exercise', '🎬 Movies/Shows', '🌿 Time alone']},

    {"question": 'How well have you been balancing studies/work and personal time? ⚖️', "options": ['😊 Very well', '🙂 Well', "😐 It's okay", '😕 Not very well']},

    {"question": 'What helps you stay motivated? 🚀', "options": ['🎯 Goals', '👨\u200d👩\u200d👧 Family', '👥 Friends', '💪 Self-discipline', '🌟 Inspiration']},

    {"question": 'How has your week been so far? 🌱', "options": ['🌟 Excellent', '😊 Good', '😐 Average', '😕 Difficult', '🫂 Need a break']},

    {"question": 'Which activity helps you recharge the most? 🔋', "options": ['😴 Sleeping', '🎵 Music', '🏃 Exercise', '🎮 Entertainment', '👥 Socialising']},

    {"question": 'How often do you take time for yourself? 🌿', "options": ['🌟 Every day', '🙂 Often', '😐 Sometimes', '😕 Rarely']},

    {"question": 'What would help make this week better? 💙', "options": ['😴 More rest', '📚 Less workload', '👥 More social time', '🏃 More activity', '🌿 More personal time']},

    {"question": 'How are you managing your current workload? 📚', "options": ['💪 Very well', '🙂 Well', '😐 Manageable', '😓 Difficult']},

    {"question": 'What do you prefer when you need a mental break? 🧠', "options": ['🎵 Music', '🚶 Walk', '😴 Sleep', '🎬 Watch something', '🌿 Quiet time']},

    {"question": 'How positive do you feel about the coming week? ☀️', "options": ['🌟 Very positive', '😊 Positive', '😐 Neutral', '😕 A little worried']},

    {"question": 'Which helps you feel more refreshed? 🌱', "options": ['😴 Good sleep', '💧 Staying hydrated', '🏃 Physical activity', '🎵 Music', '🌿 Time outdoors']},

    {"question": 'How often do you connect with friends or people you trust? 💬', "options": ['😊 Very often', '🙂 Often', '😐 Sometimes', '😕 Rarely']},

    {"question": 'What is your preferred way to unwind? 🌙', "options": ['🎵 Music', '📖 Reading', '🎬 Movies/Shows', '🎮 Games', '😴 Sleep']},

    {"question": 'How confident are you about handling challenges this week? 💪', "options": ['💪 Very confident', '🙂 Confident', '😐 Not sure', '😕 A little worried']},

    {"question": "What's one small thing that made you happy recently? 😊", "options": ["👥 Someone's support", '🎉 Good news', '🏆 Small achievement', '🌿 A peaceful moment', '☀️ Something unexpected']},

]



# --------------------------------------------------

# DATA STORAGE

# --------------------------------------------------



def default_data():

    return {

        "chat_id": None,

        "used_polls": [],

        "total_sent": 0,

        "next_poll_at": None,

        "poll_instances": [],

    }





def load_data():

    if not DATA_FILE.exists():

        return default_data()



    try:

        with open(DATA_FILE, "r", encoding="utf-8") as file:

            data = json.load(file)



        # Add missing fields safely if an older data file is ever used.

        defaults = default_data()

        for key, value in defaults.items():

            data.setdefault(key, value)



        return data

    except Exception as e:

        print(f"Could not load poll data; starting fresh: {e}")

        return default_data()





def save_data(data):

    with open(DATA_FILE, "w", encoding="utf-8") as file:

        json.dump(data, file, indent=2, ensure_ascii=False)





# --------------------------------------------------

# POLL SELECTION

# --------------------------------------------------



def get_next_poll(data):

    used = set(data.get("used_polls", []))



    available = [

        i for i in range(len(POLLS))

        if i not in used

    ]



    # Start a new cycle only after every poll has been used.

    if not available:

        data["used_polls"] = []

        available = list(range(len(POLLS)))



    poll_index = random.choice(available)

    data["used_polls"].append(poll_index)



    return poll_index





# --------------------------------------------------

# TIME HELPERS

# --------------------------------------------------



def format_ist(timestamp):

    if not timestamp:

        return "Not scheduled yet"



    return datetime.fromtimestamp(

        timestamp, tz=timezone.utc

    ).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")





# --------------------------------------------------

# POLL CLOSING

# --------------------------------------------------



async def close_poll(context: ContextTypes.DEFAULT_TYPE):

    """Close a poll after 24 hours and disable its buttons."""

    job_data = context.job.data

    chat_id = job_data["chat_id"]

    message_id = job_data["message_id"]



    data = load_data()



    instance = None

    for item in reversed(data.get("poll_instances", [])):

        if (

            item.get("message_id") == message_id

            and item.get("chat_id") == chat_id

        ):

            instance = item

            break



    if instance is None or instance.get("closed"):

        return



    instance["closed"] = True

    instance["closed_at"] = datetime.now(timezone.utc).isoformat()

    save_data(data)



    try:

        original_text = instance.get("message_text", "")

        closed_text = (

            f"{original_text}\n\n"

            "🔒 <b>This poll is now closed.</b>\n"

            "Responses were accepted for 24 hours only."

        )



        await context.bot.edit_message_text(

            chat_id=chat_id,

            message_id=message_id,

            text=closed_text,

            parse_mode="HTML",

            reply_markup=None,

        )

        print(f"Poll closed: message #{message_id}")

    except Exception as e:

        # Even if Telegram cannot edit the message, the stored poll is

        # already marked closed, so late responses remain blocked.

        print(f"Could not visually close poll #{message_id}: {e}")





def schedule_poll_expiry(application, instance):

    expires_at = datetime.fromisoformat(instance["expires_at"])



    application.job_queue.run_once(

        close_poll,

        when=expires_at,

        data={

            "chat_id": instance["chat_id"],

            "message_id": instance["message_id"],

        },

        name=f"close_poll_{instance['message_id']}",

    )





# --------------------------------------------------

# AUTOMATIC POLL SCHEDULING

# --------------------------------------------------



async def automatic_poll_job(context: ContextTypes.DEFAULT_TYPE):

    scheduled_for = context.job.data["scheduled_for"]

    await send_random_poll(

        context,

        update_schedule=True,

        scheduled_for=scheduled_for,

    )





def schedule_next_automatic_poll(application, scheduled_for):

    application.job_queue.run_once(

        automatic_poll_job,

        when=scheduled_for,

        data={"scheduled_for": scheduled_for},

        name=f"automatic_poll_{int(scheduled_for.timestamp())}",

    )





# --------------------------------------------------

# SEND POLL

# --------------------------------------------------



async def send_random_poll(

    context: ContextTypes.DEFAULT_TYPE,

    update_schedule: bool = True,

    scheduled_for=None,

):

    data = load_data()

    chat_id = data.get("chat_id")



    if not chat_id:

        print("No chat ID configured yet.")

        return



    poll_index = get_next_poll(data)

    poll = POLLS[poll_index]



    try:

        keyboard = []



        for option_index, option in enumerate(poll["options"]):

            keyboard.append([

                InlineKeyboardButton(

                    option,

                    callback_data=f"wb:{poll_index}:{option_index}",

                )

            ])



        warning = (

            "⚠️ <b>Important:</b> This is a non-anonymous check-in. "

            "Your response will be recorded with your Telegram account "

            "and may be visible to community admins. "

            "Please do not share personal or sensitive information.\n\n"

            "⏳ <b>This poll is open for 24 hours only.</b>\n\n"

        )



        message_text = (

            "🌿 <b>Wellbeing Check-in</b>\n\n"

            f"{poll['question']}\n\n"

            f"{warning}"

            "<b>Choose one option:</b>"

        )



        message = await context.bot.send_message(

            chat_id=chat_id,

            text=message_text,

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(keyboard),

        )



        sent_at = datetime.now(timezone.utc)

        expires_at = sent_at + POLL_DURATION



        instance = {

            "message_id": message.message_id,

            "chat_id": chat_id,

            "poll_index": poll_index,

            "question": poll["question"],

            "options": poll["options"],

            "message_text": message_text,

            "sent_at": sent_at.isoformat(),

            "expires_at": expires_at.isoformat(),

            "closed": False,

            "responses": {},

        }



        data.setdefault("poll_instances", []).append(instance)



        # Keep the file reasonably sized.

        if len(data["poll_instances"]) > 100:

            data["poll_instances"] = data["poll_instances"][-100:]



        data["total_sent"] = data.get("total_sent", 0) + 1



        if update_schedule:

            # scheduled_for is the intended automatic slot. Using it instead

            # of "now" keeps the 2-day schedule locked to 6:30 PM IST.

            if scheduled_for is None:

                scheduled_for = sent_at



            next_poll_at = scheduled_for + POLL_INTERVAL

            # If the bot was offline for a long time, next_poll_at can still be
            # in the past. The job queue silently skips past-dated jobs, which
            # would stop automatic polls completely. Move forward in 2-day steps
            # (keeping 6:30 PM IST) to the first future slot. Missed slots are
            # skipped, not sent in a burst.
            while next_poll_at <= sent_at:
                next_poll_at += POLL_INTERVAL

            data["next_poll_at"] = next_poll_at.timestamp()



        save_data(data)



        # Schedule the 24-hour closing.

        schedule_poll_expiry(context.application, instance)



        # Schedule the next automatic poll only for automatic polls.

        if update_schedule:

            schedule_next_automatic_poll(

                context.application,

                next_poll_at,

            )



        print(

            f"Poll sent: #{poll_index + 1} | "

            f"Closes: {expires_at.isoformat()}"

        )



    except Exception as e:

        print(f"Error sending poll: {e}")





# --------------------------------------------------

# POLL RESPONSE

# --------------------------------------------------



async def handle_poll_response(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

):

    """Record one non-anonymous response to a custom poll."""

    query = update.callback_query



    if not query or not query.message or not query.from_user:

        return



    try:

        _, poll_index_text, option_index_text = query.data.split(":")

        poll_index = int(poll_index_text)

        option_index = int(option_index_text)

    except (ValueError, AttributeError):

        await query.answer("Invalid poll response.", show_alert=True)

        return



    data = load_data()

    message_id = query.message.message_id

    chat_id = query.message.chat_id



    instance = None



    for item in reversed(data.get("poll_instances", [])):

        if (

            item.get("message_id") == message_id

            and item.get("chat_id") == chat_id

            and item.get("poll_index") == poll_index

        ):

            instance = item

            break



    if instance is None:

        await query.answer(

            "This poll is no longer available for responses.",

            show_alert=True,

        )

        return



    # Enforce the 24-hour window even if the expiry job has not executed yet.

    expires_at = datetime.fromisoformat(instance["expires_at"])

    if instance.get("closed") or datetime.now(timezone.utc) >= expires_at:

        if not instance.get("closed"):

            instance["closed"] = True

            instance["closed_at"] = datetime.now(timezone.utc).isoformat()

            save_data(data)



        await query.answer(

            "🔒 This poll is closed. Responses were accepted for 24 hours only.",

            show_alert=True,

        )



        try:

            await query.message.edit_reply_markup(reply_markup=None)

        except Exception:

            pass



        return



    if option_index < 0 or option_index >= len(instance["options"]):

        await query.answer("Invalid response.", show_alert=True)

        return



    user_key = str(query.from_user.id)



    if user_key in instance["responses"]:

        await query.answer(

            "Your response has already been recorded for this poll.",

            show_alert=True,

        )

        return



    user = query.from_user

    display_name = user.full_name or user.first_name or "Unknown"



    instance["responses"][user_key] = {

        "user_id": user.id,

        "name": display_name,

        "username": user.username,

        "option_index": option_index,

        "option": instance["options"][option_index],

        "responded_at": datetime.now(timezone.utc).isoformat(),

    }



    save_data(data)



    await query.answer("✅ Your response has been recorded.")



    print(

        f"Response recorded: {display_name} "

        f"({user.id}) -> {instance['options'][option_index]}"

    )





# --------------------------------------------------

# RESULTS

# --------------------------------------------------



def format_poll_results(instance):

    responses = instance.get("responses", {})

    options = instance.get("options", [])



    counts = [0] * len(options)



    for response in responses.values():

        index = response.get("option_index")

        if isinstance(index, int) and 0 <= index < len(counts):

            counts[index] += 1



    lines = [

        "🌿 <b>Wellbeing Poll Results</b>",

        "",

        f"<b>{instance.get('question', 'Poll')}</b>",

        "",

        f"👥 Total responses: <b>{len(responses)}</b>",

        "",

    ]



    for index, option in enumerate(options):

        lines.append(f"{option} — <b>{counts[index]}</b>")



    if responses:

        lines.extend(["", "👤 <b>Individual Responses</b>", ""])



        for response in responses.values():

            name = response.get("name", "Unknown")

            username = response.get("username")



            if username:

                name = f"{name} (@{username})"



            lines.append(

                f"• {name} — {response.get('option', 'Unknown')}"

            )

    else:

        lines.extend(["", "No responses yet."])



    return "\n".join(lines)





def split_message(text, max_length=4000):

    if len(text) <= max_length:

        return [text]



    chunks = []

    current = ""



    for line in text.split("\n"):

        candidate = f"{current}\n{line}" if current else line



        if len(candidate) <= max_length:

            current = candidate

        else:

            if current:

                chunks.append(current)



            while len(line) > max_length:

                chunks.append(line[:max_length])

                line = line[max_length:]



            current = line



    if current:

        chunks.append(current)



    return chunks





async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the latest poll's results privately to an authorized admin."""
    if not await is_private_admin(update, context):
        await update.message.reply_text(
            "⛔ This command is available to authorized admins only."
        )
        return

    data = load_data()
    instances = data.get("poll_instances", [])

    if not instances:
        await update.message.reply_text(
            "No custom wellbeing poll has been sent yet."
        )
        return

    latest = instances[-1]

    await update.message.reply_text(
        "🔐 <b>Admin-only results</b>\n\n"
        "The latest wellbeing poll results are below.",
        parse_mode="HTML",
    )

    for chunk in split_message(format_poll_results(latest)):
        await update.message.reply_text(
            chunk,
            parse_mode="HTML",
        )

# --------------------------------------------------

# ADMIN CHECK

# --------------------------------------------------



async def is_private_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check whether the private-chat user is an authorized bot admin."""
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == "private" and user.id in ADMIN_USER_IDS)


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check whether the user is an administrator/creator of the current group."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat or chat.type not in ["group", "supergroup"]:
        return False

    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ["administrator", "creator"]


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compatibility helper for context-specific admin access."""
    if update.effective_chat and update.effective_chat.type == "private":
        return await is_private_admin(update, context)
    return await is_group_admin(update, context)


# /START

# --------------------------------------------------



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        if not await is_private_admin(update, context):
            await update.message.reply_text("⛔ This bot is restricted to authorized admins.")
            return
        await update.message.reply_text(
            "🌿 <b>Buddy4Study Wellbeing Poll Bot</b>\n\n"
            "👑 Admin control panel is active.\n\n"
            "Use /help to view available admin commands.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "🌿 Buddy4Study Wellbeing Poll Bot is active!\n\n"
        "Group setup is available to group admins via /setup."
    )








# --------------------------------------------------

# /SETUP

# --------------------------------------------------



async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.type not in ["group", "supergroup"]:

        await update.message.reply_text(

            "Please use /setup inside the Wellbeing Support group."

        )

        return



    # Must be BOTH a group admin AND listed in ADMIN_USER_IDS. The bot is an
    # admin in all community groups (for membership tracking), so a plain
    # group-admin check would let any admin redirect the polls by mistake.
    user = update.effective_user

    if not user or user.id not in ADMIN_USER_IDS or not await is_group_admin(update, context):

        await update.message.reply_text(

            "⛔ /setup is available to authorized bot admins only."

        )

        return



    data = load_data()

    current_chat_id = data.get("chat_id")

    confirmed = bool(context.args) and context.args[0].lower() == "confirm"

    if current_chat_id and current_chat_id != update.effective_chat.id and not confirmed:

        await update.message.reply_text(

            "⚠️ Wellbeing polls are already connected to another group.\n\n"

            "If you really want to move the polls to THIS group, send:\n"

            "/setup confirm"

        )

        return



    data["chat_id"] = update.effective_chat.id



    # If this is a fresh setup, the first automatic slot is today at 6:30 PM IST.

    if data.get("next_poll_at") is None:

        data["next_poll_at"] = FIRST_POLL_AT.timestamp()



    save_data(data)



    # Show /next and /setup in the menu of THIS group only.

    await set_group_command_menu(context.application, update.effective_chat.id)



    await update.message.reply_text(

        "✅ Wellbeing Support group connected!\n\n"

        f"📊 Polls available: {len(POLLS)}\n"

        "⏱️ Schedule: Every 2 days at 6:30 PM IST\n"

        "⏳ Each poll: Open for 24 hours\n"

        "🎲 Selection: Random\n"

        "🚫 Repeats: Only after the full poll pool is used\n\n"

        "Open the bot in private chat and use /test for an admin-side test."

    )





# --------------------------------------------------

# /TEST

# --------------------------------------------------



async def test_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run a private admin-side test. It never sends a poll to the group."""
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    data = load_data()
    if not data.get("chat_id"):
        await update.message.reply_text(
            "⚠️ No Wellbeing Support group is configured yet.\n\n"
            "A group admin must run /setup in the Wellbeing Support group first."
        )
        return

    try:
        me = await context.bot.get_me()
        await update.message.reply_text(
            "🧪 <b>Bot Test Successful</b>\n\n"
            f"🤖 Bot: @{me.username or me.first_name}\n"
            "📍 Group configuration: Connected\n"
            "🔐 Admin authorization: Working\n"
            "💾 Data storage: Accessible\n"
            "📊 Poll system: Ready\n\n"
            "No poll was sent to the group.",
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"Private test failed: {e}")
        await update.message.reply_text("❌ Test failed. Please check the bot logs.")








# --------------------------------------------------

# /NEXT

# --------------------------------------------------



async def next_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the next poll directly to the configured group. Group admins only."""
    if not await is_group_admin(update, context):
        if update.effective_chat.type == "private":
            await update.message.reply_text(
                "⛔ /next can only be used by a group admin inside the Wellbeing Support group."
            )
        else:
            await update.message.reply_text("⛔ This command is available to group admins only.")
        return

    data = load_data()
    if data.get("chat_id") != update.effective_chat.id:
        await update.message.reply_text("⚠️ This group is not the configured Wellbeing Support group.")
        return

    await send_random_poll(context, update_schedule=False)

    try:
        await update.message.delete()
    except Exception:
        pass








# --------------------------------------------------

# /HELP

# --------------------------------------------------



async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    await update.message.reply_text(
        "🌿 <b>Buddy4Study Wellbeing Poll Bot</b>\n\n"
        "👑 <b>Admin commands (Private)</b>\n\n"
        "/status — View poll and schedule status\n"
        "/results — View latest poll results privately\n"
        "/test — Test the bot privately (no group poll)\n"
        "/help — Show available commands\n\n"
        "👥 <b>Membership tracking (Private)</b>\n\n"
        "/trackstatus — Is tracking working? Which groups?\n"
        "/memberstats — Joins and leaves per group\n"
        "/recentleaves — Latest people who left (e.g. /recentleaves 20)\n"
        "/recentjoins — Latest people who joined\n\n"
        "🚪 <b>Join requests (Private)</b>\n\n"
        "/pending — Requests waiting for your review\n"
        "/allow @user1 @user2 — Add to scholar list\n"
        "/disallow @user — Remove from list\n"
        "/allowlist — Show the scholar list\n"
        "📄 Send the Google Form sheet as a .csv file to import it\n\n"
        "📢 <b>Group command</b>\n"
        "/next — Send the next random unused poll to the group\n\n"
        "⏱️ Automatic polls: Every 2 days at 6:30 PM IST\n"
        "⏳ Poll duration: 24 hours\n"
        "🎲 Random selection: ON\n"
        "🚫 No-repeat cycle: ON",
        parse_mode="HTML",
    )














# --------------------------------------------------

# /STATUS

# --------------------------------------------------





async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    data = load_data()
    used = len(data.get("used_polls", []))
    remaining = len(POLLS) - used
    total_sent = data.get("total_sent", 0)
    next_poll_at = data.get("next_poll_at")

    status_message = (
        "🌿 Wellbeing Poll Bot Status\n\n"
        f"📊 Total polls: {len(POLLS)}\n"
        f"🎲 Used in current cycle: {used}\n"
        f"🔄 Remaining before reset: {remaining}\n"
        f"📨 Total polls sent: {total_sent}\n"
        "⏱️ Schedule: Every 2 days at 6:30 PM IST\n"
        "⏳ Poll duration: 24 hours\n"
        f"🗓️ Next automatic poll: {format_ist(next_poll_at)}"
    )
    await update.message.reply_text(status_message)








# --------------------------------------------------

# STARTUP SCHEDULING

# --------------------------------------------------



def schedule_existing_poll_expiries(application, data):

    """Restore expiry jobs after a bot restart."""

    now = datetime.now(timezone.utc)



    for instance in data.get("poll_instances", []):

        if instance.get("closed"):

            continue



        expires_at = datetime.fromisoformat(instance["expires_at"])



        if expires_at <= now:

            # Mark expired now. The message will be cleaned up when possible.

            instance["closed"] = True

            instance["closed_at"] = now.isoformat()

        else:

            schedule_poll_expiry(application, instance)



    save_data(data)





def setup_automatic_schedule(application, data):

    next_poll_at = data.get("next_poll_at")

    now = datetime.now(timezone.utc)



    if next_poll_at is None:

        next_dt = FIRST_POLL_AT

        data["next_poll_at"] = next_dt.timestamp()

        save_data(data)

        next_poll_at = next_dt.timestamp()



    scheduled_for = datetime.fromtimestamp(next_poll_at, tz=timezone.utc)



    # If the bot starts after a missed automatic slot, send that missed poll

    # immediately, then continue on the original 2-day cadence.

    if scheduled_for <= now:

        # Use the missed slot as the reference so the next one remains 2 days

        # after the intended 6:30 PM IST schedule.

        application.job_queue.run_once(

            automatic_poll_job,

            when=timedelta(seconds=5),

            data={"scheduled_for": scheduled_for},

            name=f"missed_automatic_poll_{int(scheduled_for.timestamp())}",

        )

    else:

        schedule_next_automatic_poll(application, scheduled_for)





# --------------------------------------------------

# MAIN

# --------------------------------------------------



# --------------------------------------------------
# MEMBERSHIP TRACKING
# --------------------------------------------------
# Records joins/leaves in the groups listed in TRACKED_CHAT_IDS.
# Needs: the bot is an ADMIN in each tracked group, and run_polling()
# requests chat_member updates (see main). Telegram does not send past
# events, so history starts from the moment tracking is switched on.


def get_membership_store(context: ContextTypes.DEFAULT_TYPE) -> MembershipStore:
    return context.application.bot_data["membership_store"]


def format_utc_iso_as_ist(iso_text: str) -> str:
    return datetime.fromisoformat(iso_text).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def describe_member(display_name: str, username, user_id: int) -> str:
    name = html.escape(display_name or "Unknown")
    handle = f" (@{html.escape(username)})" if username else ""
    return f"{name}{handle} · <code>{user_id}</code>"


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Send a private alert to every admin. Failures are counted, never fatal."""
    stats = context.application.bot_data.setdefault(
        "notify_stats", {"sent": 0, "failed": 0, "last_error": None}
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            stats["sent"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["last_error"] = f"{datetime.now(IST).strftime('%d %b %I:%M %p')} → admin {admin_id}: {e}"
            print(f"Membership alert to admin {admin_id} failed: {e}")


async def track_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    change = update.chat_member
    if not change:
        return

    chat = change.chat

    # Joining a landing group can unlock pending requests in the other groups.
    if chat.id in MANDATORY_CHAT_IDS and APPROVAL_CHAT_IDS:
        old_m, new_m = change.old_chat_member, change.new_chat_member
        if classify_membership_change(
            old_m.status, getattr(old_m, "is_member", None),
            new_m.status, getattr(new_m, "is_member", None),
        ) == "join":
            await recheck_pending(context, user_id=new_m.user.id)

    if chat.id not in TRACKED_CHAT_IDS:
        # Print each untracked group once, to help find group IDs during setup.
        seen = context.application.bot_data.setdefault("untracked_seen", set())
        if chat.id not in seen:
            seen.add(chat.id)
            print(f"Membership update from UNTRACKED group: {chat.title} ({chat.id})")
        return

    old, new = change.old_chat_member, change.new_chat_member
    event_type = classify_membership_change(
        old.status, getattr(old, "is_member", None),
        new.status, getattr(new, "is_member", None),
    )
    if event_type is None:
        return

    member = new.user
    occurred_at = change.date  # timezone-aware UTC from Telegram
    update_key = (
        f"{chat.id}:{member.id}:{int(occurred_at.timestamp())}:{old.status}>{new.status}"
    )

    try:
        is_new = get_membership_store(context).record_event(
            chat_id=chat.id,
            chat_title=chat.title or str(chat.id),
            user_id=member.id,
            username=member.username,
            display_name=member.full_name,
            event_type=event_type,
            old_status=old.status,
            new_status=new.status,
            occurred_at=occurred_at,
            update_key=update_key,
        )
    except Exception as e:
        print(f"Could not save membership event in {chat.id}: {e}")
        await notify_admins(
            context,
            "⚠️ <b>Membership tracker error</b>\n\n"
            f"Could not save an event from <b>{html.escape(chat.title or str(chat.id))}</b>. "
            "Please check the bot logs.",
        )
        return

    if not is_new or event_type not in MEMBERSHIP_ALERTS:
        return  # duplicate update, or this kind of event has no alert

    actor = change.from_user
    by_someone_else = actor and actor.id != member.id
    if event_type == "join":
        heading = "🟢 <b>Member joined</b>" if not by_someone_else else "🟢 <b>Member added</b>"
    elif new.status == ChatMember.BANNED:
        heading = "⛔ <b>Member removed</b>"
    else:
        heading = "🔴 <b>Member left</b>" if not by_someone_else else "⛔ <b>Member removed</b>"

    lines = [
        heading,
        "",
        f"👥 Group: <b>{html.escape(chat.title or str(chat.id))}</b>",
        f"👤 {describe_member(member.full_name, member.username, member.id)}",
        f"🕒 {occurred_at.astimezone(IST).strftime('%d %b %Y, %I:%M %p IST')}",
    ]
    if by_someone_else:
        lines.append(f"🛠️ By: {html.escape(actor.full_name)}")
    await notify_admins(context, "\n".join(lines))


def parse_limit(args, default=10, maximum=50) -> int:
    if args and args[0].isdigit():
        return max(1, min(int(args[0]), maximum))
    return default


async def send_recent_members(update: Update, context: ContextTypes.DEFAULT_TYPE, event_type: str):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    limit = parse_limit(context.args)
    rows = get_membership_store(context).recent_events(limit=limit, event_type=event_type)
    title = "🔴 <b>Recent leaves</b>" if event_type == "leave" else "🟢 <b>Recent joins</b>"
    if not rows:
        await update.message.reply_text(f"{title}\n\nNothing recorded yet.", parse_mode="HTML")
        return

    lines = [f"{title} (latest {len(rows)})", ""]
    for row in rows:
        how = " · removed" if row["new_status"] == ChatMember.BANNED else ""
        lines.append(
            f"• {describe_member(row['display_name'], row['username'], row['user_id'])}\n"
            f"   {html.escape(row['chat_title'])} · {format_utc_iso_as_ist(row['occurred_at_utc'])}{how}"
        )
    for chunk in split_message("\n".join(lines)):
        await update.message.reply_text(chunk, parse_mode="HTML")


async def recent_leaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_recent_members(update, context, "leave")


async def recent_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_recent_members(update, context, "join")


async def member_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    store = get_membership_store(context)
    week = {r["chat_id"]: r for r in store.stats_by_chat(since=datetime.now(timezone.utc) - timedelta(days=7))}
    all_time = store.stats_by_chat()
    if not all_time:
        await update.message.reply_text("📊 No membership events recorded yet.")
        return

    started = store.first_event_time()
    lines = ["📊 <b>Membership stats</b>", ""]
    for row in all_time:
        w = week.get(row["chat_id"], {"joins": 0, "leaves": 0})
        lines.append(
            f"<b>{html.escape(row['chat_title'])}</b>\n"
            f"   Last 7 days: +{w['joins']} joined, −{w['leaves']} left\n"
            f"   Since tracking began: +{row['joins']} / −{row['leaves']}"
        )
    if started:
        lines += ["", f"ℹ️ Tracking data starts {format_utc_iso_as_ist(started)}."]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def track_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return

    store = get_membership_store(context)
    stats = context.application.bot_data.get("notify_stats", {"sent": 0, "failed": 0, "last_error": None})
    lines = ["👥 <b>Membership tracking status</b>", ""]

    if not TRACKED_CHAT_IDS:
        lines.append("⚠️ No groups configured. Add TRACKED_CHAT_IDS to .env.")
    else:
        lines.append(f"Tracked groups: {len(TRACKED_CHAT_IDS)}")
        me = await context.bot.get_me()
        for chat_id in sorted(TRACKED_CHAT_IDS):
            try:
                chat = await context.bot.get_chat(chat_id)
                bot_member = await context.bot.get_chat_member(chat_id, me.id)
                ok = bot_member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
                mark = "✅" if ok else "❌ bot is not admin"
                lines.append(f"• {html.escape(chat.title or str(chat_id))} — {mark}")
            except Exception as e:
                lines.append(f"• <code>{chat_id}</code> — ❌ cannot reach ({html.escape(str(e))})")

    alerts = ", ".join(sorted(MEMBERSHIP_ALERTS)) or "off"
    lines += [
        "",
        f"🔔 Alerts for: {alerts}",
        f"🗂️ Events stored: {store.count_events()}",
        f"📨 Alerts since restart: {stats['sent']} sent, {stats['failed']} failed",
    ]
    if stats.get("last_error"):
        lines.append(f"Last failure: {html.escape(stats['last_error'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------
# JOIN REQUEST AUTO-APPROVAL
# --------------------------------------------------
# Landing groups: approve if the username is on the allowlist (Google Form list).
# Other groups: approve if the person is already in ALL landing groups.
# Otherwise: keep the request pending and send admins Approve/Decline buttons.
# The bot needs admin rights with "Add members / Invite users" in each group.
# Telegram does not let bots list requests made before the bot was running;
# those must be approved by hand once.


async def get_chat_title(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    cache = context.application.bot_data.setdefault("chat_titles", {})
    if chat_id not in cache:
        try:
            cache[chat_id] = (await context.bot.get_chat(chat_id)).title or str(chat_id)
        except TelegramError:
            return str(chat_id)
    return cache[chat_id]


async def is_member_of(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False  # e.g. "user not found" = never joined
    return is_present(member.status, getattr(member, "is_member", None))


async def check_join_request(context, chat_id: int, user_id: int, username) -> tuple:
    """Return (approve?, reason shown to admins when not approved)."""
    if chat_id in MANDATORY_CHAT_IDS:
        if not normalize_username(username):
            return False, "No Telegram username, so it can't be matched with the list."
        if get_membership_store(context).is_allowed(username):
            return True, ""
        return False, "Username is not in the scholar list."

    if not MANDATORY_CHAT_IDS:
        return False, "Landing groups are not configured (MANDATORY_CHAT_IDS)."
    missing = [
        await get_chat_title(context, c)
        for c in sorted(MANDATORY_CHAT_IDS)
        if not await is_member_of(context, c, user_id)
    ]
    if not missing:
        return True, ""
    return False, "Not in: " + ", ".join(missing)


def request_buttons(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"jr:a:{chat_id}:{user_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"jr:d:{chat_id}:{user_id}"),
    ]])


async def decide_request(context, chat_id: int, user_id: int, approve: bool, decided_by: str) -> tuple:
    """Approve/decline on Telegram and record it. Returns (ok, message)."""
    store = get_membership_store(context)
    try:
        if approve:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        else:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
    except TelegramError as e:
        text = str(e)
        if "hide_requester_missing" in text.lower() or "user_already_participant" in text.lower():
            store.set_request_status(chat_id, user_id, "handled_elsewhere", decided_by)
            return False, "Already handled (approved/declined by someone else, or request withdrawn)."
        return False, f"Telegram error: {text}"
    store.set_request_status(chat_id, user_id, "approved" if approve else "declined", decided_by)
    return True, "Approved" if approve else "Declined"


async def alert_admins_about_request(context, req: dict, reason: str):
    who = describe_member(req["display_name"], req["username"], req["user_id"])
    text = (
        "🙋 <b>Join request needs review</b>\n\n"
        f"👥 Group: <b>{html.escape(req['chat_title'] or str(req['chat_id']))}</b>\n"
        f"👤 {who}\n"
        f"❓ {html.escape(reason)}"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML",
                reply_markup=request_buttons(req["chat_id"], req["user_id"]),
            )
        except TelegramError as e:
            print(f"Join-request alert to admin {admin_id} failed: {e}")


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req or req.chat.id not in APPROVAL_CHAT_IDS:
        return

    user = req.from_user
    store = get_membership_store(context)
    store.save_join_request(
        chat_id=req.chat.id, chat_title=req.chat.title or str(req.chat.id),
        user_id=user.id, username=user.username, display_name=user.full_name,
        requested_at=req.date,
    )

    approve, reason = await check_join_request(context, req.chat.id, user.id, user.username)
    if approve:
        ok, msg = await decide_request(context, req.chat.id, user.id, True, "auto")
        if ok:
            stats = context.application.bot_data.setdefault("auto_approved", {"count": 0})
            stats["count"] += 1
            print(f"Auto-approved {user.id} in {req.chat.id}")
            return
        reason = f"Auto-approve failed: {msg}"

    await alert_admins_about_request(context, store.get_join_request(req.chat.id, user.id), reason)


async def recheck_pending(context, *, usernames=None, user_id=None) -> int:
    """Re-check pending requests (after the list grows or someone joins a landing group)."""
    approved = 0
    for req in get_membership_store(context).pending_requests(usernames=usernames, user_id=user_id):
        if req["chat_id"] not in APPROVAL_CHAT_IDS:
            continue
        ok_to_approve, _ = await check_join_request(context, req["chat_id"], req["user_id"], req["username"])
        if ok_to_approve:
            ok, _ = await decide_request(context, req["chat_id"], req["user_id"], True, "auto")
            approved += ok
    return approved


async def join_request_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.from_user or query.from_user.id not in ADMIN_USER_IDS:
        await query.answer("Only authorized admins can do this.", show_alert=True)
        return

    _, action, chat_id, user_id = query.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    req = get_membership_store(context).get_join_request(chat_id, user_id)
    if req and req["status"] != "pending":
        await query.answer(f"Already {req['status'].replace('_', ' ')}.", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    ok, msg = await decide_request(context, chat_id, user_id, action == "a", query.from_user.full_name)
    await query.answer(msg, show_alert=not ok)
    mark = ("✅ Approved" if action == "a" else "❌ Declined") if ok else f"⚠️ {msg}"
    try:
        await query.edit_message_text(
            f"{query.message.text_html}\n\n<b>{html.escape(mark)}</b> by {html.escape(query.from_user.full_name)}",
            parse_mode="HTML",
        )
    except TelegramError:
        pass


def usernames_from_text(text: str) -> tuple[list, list]:
    """Split free text into (valid normalized usernames, invalid tokens)."""
    good, bad = [], []
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        name = normalize_username(token)
        (good if name else bad).append(name or token)
    return good, bad


def usernames_from_csv(raw: str) -> tuple[list, list, str]:
    """Pick the username column from a Google Sheets CSV export.

    Returns (usernames, invalid values, column name used).
    """
    import csv, io
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return [], [], ""
    header = [h.strip().lower() for h in rows[0]]
    col = next((i for i, h in enumerate(header) if "telegram" in h and "user" in h), None)
    if col is None:
        col = next((i for i, h in enumerate(header) if "username" in h or "telegram" in h), None)
    if col is None:
        if len(header) == 1:  # single column without a clear header
            good, bad = usernames_from_text("\n".join(r[0] for r in rows if r))
            return good, bad, "(single column)"
        return [], [], ""
    good, bad = [], []
    for r in rows[1:]:
        value = r[col].strip() if col < len(r) else ""
        if not value:
            continue
        name = normalize_username(value)
        (good if name else bad).append(name or value)
    return good, bad, rows[0][col]


async def report_added(update, context, good, bad, source):
    store = get_membership_store(context)
    added, existing = store.add_usernames(good, source=source)
    approved_now = await recheck_pending(context, usernames=good)
    lines = [
        "📋 <b>Scholar list updated</b>",
        "",
        f"➕ Added: {added}",
        f"✔️ Already in list: {existing}",
        f"📊 Total in list: {len(store.list_usernames())}",
    ]
    if approved_now:
        lines.append(f"🚪 Pending requests approved now: {approved_now}")
    if bad:
        sample = ", ".join(html.escape(b) for b in bad[:10])
        more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
        lines += ["", f"⚠️ Skipped {len(bad)} invalid value(s): {sample}{more}"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return
    good, bad = usernames_from_text(" ".join(context.args))
    if not good and not bad:
        await update.message.reply_text(
            "Usage: /allow @username1 @username2 ...\n\n"
            "For the full Google Form list: download the sheet as CSV "
            "(File → Download → .csv) and send the file here."
        )
        return
    await report_added(update, context, good, bad, "manual")


async def disallow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return
    good, _ = usernames_from_text(" ".join(context.args))
    if not good:
        await update.message.reply_text("Usage: /disallow @username")
        return
    store = get_membership_store(context)
    removed = [n for n in good if store.remove_username(n)]
    not_found = [n for n in good if n not in removed]
    text = f"➖ Removed: {', '.join('@' + n for n in removed) or 'none'}"
    if not_found:
        text += f"\nNot in list: {', '.join('@' + n for n in not_found)}"
    await update.message.reply_text(text)


async def allowlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return
    names = get_membership_store(context).list_usernames()
    if not names:
        await update.message.reply_text("📋 The scholar list is empty. Send the CSV file or use /allow.")
        return
    text = f"📋 Scholar list ({len(names)})\n\n" + "\n".join("@" + n for n in names)
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_private_admin(update, context):
        await update.message.reply_text("⛔ This command is available to authorized admins only.")
        return
    rows = get_membership_store(context).pending_requests(limit=20)
    if not rows:
        await update.message.reply_text("🙋 No pending join requests.")
        return
    await update.message.reply_text(f"🙋 Pending join requests ({len(rows)} shown):")
    for req in rows:
        _, reason = await check_join_request(context, req["chat_id"], req["user_id"], req["username"])
        await update.message.reply_text(
            f"👥 <b>{html.escape(req['chat_title'] or str(req['chat_id']))}</b>\n"
            f"👤 {describe_member(req['display_name'], req['username'], req['user_id'])}\n"
            f"❓ {html.escape(reason or 'Meets the rules now')}",
            parse_mode="HTML",
            reply_markup=request_buttons(req["chat_id"], req["user_id"]),
        )


async def import_list_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends the Google Form sheet as CSV (or a .txt list) in private chat."""
    if not await is_private_admin(update, context):
        return
    doc = update.message.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".csv", ".txt")):
        await update.message.reply_text("Please send a .csv (Google Sheets → File → Download → .csv) or .txt file.")
        return
    if doc.file_size and doc.file_size > 5_000_000:
        await update.message.reply_text("File is too large (max 5 MB).")
        return
    raw = (await (await doc.get_file()).download_as_bytearray()).decode("utf-8-sig", errors="replace")
    if name.endswith(".csv"):
        good, bad, column = usernames_from_csv(raw)
        if not column:
            await update.message.reply_text(
                "⚠️ Couldn't find the username column. The column header should contain "
                "'Telegram' or 'Username'."
            )
            return
        await update.message.reply_text(f"📄 Reading column: {column}")
    else:
        good, bad = usernames_from_text(raw)
    await report_added(update, context, good, bad, f"file:{doc.file_name}")


GROUP_COMMANDS = [
    BotCommand("next", "Send the next wellbeing poll"),
    BotCommand("setup", "Connect this group to the bot"),
]


async def set_group_command_menu(application: Application, chat_id: int):
    """Show the group commands only to admins of the Wellbeing Support group."""
    try:
        await application.bot.set_my_commands(
            GROUP_COMMANDS,
            scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
        )
    except Exception as e:
        print(f"Could not set group commands for the Wellbeing group: {e}")


async def post_init(application: Application):
    """Set separate command menus for admin private chats and the Wellbeing group."""
    private_commands = [
        BotCommand("start", "Open admin control panel"),
        BotCommand("status", "View bot status"),
        BotCommand("results", "View latest poll results"),
        BotCommand("test", "Run a private bot test"),
        BotCommand("trackstatus", "Membership tracking status"),
        BotCommand("memberstats", "Joins and leaves per group"),
        BotCommand("recentleaves", "Latest members who left"),
        BotCommand("recentjoins", "Latest members who joined"),
        BotCommand("pending", "Join requests waiting for review"),
        BotCommand("allow", "Add usernames to scholar list"),
        BotCommand("disallow", "Remove a username from the list"),
        BotCommand("allowlist", "Show the scholar list"),
        BotCommand("help", "Show admin commands"),
    ]

    for admin_id in ADMIN_USER_IDS:
        try:
            await application.bot.set_my_commands(
                private_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            print(f"Could not set private commands for admin {admin_id}: {e}")

    # Older test runs set the group menu for admins of EVERY group; remove that.
    try:
        await application.bot.delete_my_commands(scope=BotCommandScopeAllChatAdministrators())
    except Exception as e:
        print(f"Could not clear old group-wide commands: {e}")

    chat_id = load_data().get("chat_id")
    if chat_id:
        await set_group_command_menu(application, chat_id)


def main():

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()



    application.add_handler(CommandHandler("start", start))

    application.add_handler(CommandHandler("setup", setup))

    application.add_handler(CommandHandler("test", test_poll))

    application.add_handler(CommandHandler("next", next_poll))

    application.add_handler(CommandHandler("status", status))

    application.add_handler(CommandHandler("results", results))

    application.add_handler(CommandHandler("help", help_command))

    # Membership tracking
    application.bot_data["membership_store"] = MembershipStore(MEMBERSHIP_DB_PATH)

    application.add_handler(ChatMemberHandler(track_membership, ChatMemberHandler.CHAT_MEMBER))

    application.add_handler(CommandHandler("trackstatus", track_status))

    application.add_handler(CommandHandler("memberstats", member_stats))

    application.add_handler(CommandHandler("recentleaves", recent_leaves))

    application.add_handler(CommandHandler("recentjoins", recent_joins))

    # Join request auto-approval
    application.add_handler(ChatJoinRequestHandler(handle_join_request))

    application.add_handler(CallbackQueryHandler(join_request_button, pattern=r"^jr:[ad]:-?\d+:\d+$"))

    application.add_handler(CommandHandler("allow", allow_command))

    application.add_handler(CommandHandler("disallow", disallow_command))

    application.add_handler(CommandHandler("allowlist", allowlist_command))

    application.add_handler(CommandHandler("pending", pending_command))

    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, import_list_file))

    application.add_handler(

        CallbackQueryHandler(

            handle_poll_response,

            pattern=r"^wb:\d+:\d+$",

        )

    )



    data = load_data()



    print("======================================")

    print("Buddy4Study Wellbeing Poll Bot")

    print("======================================")

    print(f"Polls in pool: {len(POLLS)}")

    print("Schedule: Every 2 days at 6:30 PM IST")

    print("Poll duration: 24 hours")

    print("Random selection: ON")

    print("No-repeat cycle: ON")

    print("Persistent schedule: ON")

    print("Custom non-anonymous polls: ON")

    print("Admin-only results: ON")

    print(f"Membership tracking: {len(TRACKED_CHAT_IDS)} group(s)" if TRACKED_CHAT_IDS else "Membership tracking: OFF (TRACKED_CHAT_IDS empty)")

    print(f"Membership alerts: {', '.join(sorted(MEMBERSHIP_ALERTS)) or 'off'}")

    print(f"Join-request auto-approval: {len(APPROVAL_CHAT_IDS)} group(s), {len(MANDATORY_CHAT_IDS)} landing group(s)" if APPROVAL_CHAT_IDS else "Join-request auto-approval: OFF (APPROVAL_CHAT_IDS empty)")

    print("======================================")



    # Restore expiry jobs for polls that were active before a restart.

    schedule_existing_poll_expiries(application, data)



    # Restore the next automatic poll.

    setup_automatic_schedule(application, data)



    print("Bot is running...")

    print("======================================")



    # chat_member updates are only delivered when requested explicitly.

    application.run_polling(allowed_updates=Update.ALL_TYPES)





if __name__ == "__main__":

    main()
