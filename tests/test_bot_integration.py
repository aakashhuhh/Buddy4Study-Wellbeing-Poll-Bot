"""Offline tests for the Telegram side of app.py.

Uses real python-telegram-bot objects for membership updates and a fake bot for
sending messages. No Telegram API is contacted and no real token is used.
"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import (
    BotCommandScopeChatAdministrators, Chat, ChatJoinRequest, ChatMemberAdministrator, ChatMemberBanned,
    ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Update, User,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_ID = 111
TRACKED = -1001
UNTRACKED = -2002
ANNOUNCE, INTRO, CAREER = -1001, -1002, -1003


def load_bot(tmpdir):
    os.environ.update({
        "BOT_TOKEN": "123:TEST-TOKEN-NOT-REAL",
        "ADMIN_USER_IDS": str(ADMIN_ID),
        "TRACKED_CHAT_IDS": str(TRACKED),
        "MEMBERSHIP_DB_PATH": str(Path(tmpdir) / "m.sqlite3"),
        "MEMBERSHIP_ALERTS": "join,leave",
        "APPROVAL_CHAT_IDS": f"{ANNOUNCE},{INTRO},{CAREER}",
        "MANDATORY_CHAT_IDS": f"{ANNOUNCE},{INTRO}",
    })
    sys.path.insert(0, str(ROOT))
    if "app" in sys.modules:
        return importlib.reload(sys.modules["app"])
    return importlib.import_module("app")


def run(coro):
    return asyncio.run(coro)


class BotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = load_bot(self.tmp.name)
        self.bot.DATA_FILE = Path(self.tmp.name) / "poll_data.json"
        self.fake = MagicMock()
        self.fake.send_message = AsyncMock()
        self.fake.set_my_commands = AsyncMock()
        self.app = SimpleNamespace(
            bot_data={"membership_store": self.bot.MembershipStore(os.environ["MEMBERSHIP_DB_PATH"])},
            bot=self.fake,
        )
        self.ctx = SimpleNamespace(application=self.app, bot=self.fake, args=[])
        self.scholar = User(42, "Riya", False, last_name="S", username="riya_s")
        self.admin_user = User(ADMIN_ID, "Aakash", False)
        self.update_id = 0

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- helpers
    def member_update(self, old, new, chat_id=TRACKED, actor=None, ts=1_760_000_000):
        self.update_id += 1
        chat = Chat(chat_id, "supergroup", title="Announcements")
        change = ChatMemberUpdated(
            chat=chat, from_user=actor or self.scholar,
            date=datetime.fromtimestamp(ts, tz=timezone.utc),
            old_chat_member=old, new_chat_member=new,
        )
        return Update(self.update_id, chat_member=change)

    def store(self):
        return self.app.bot_data["membership_store"]

    def sent_texts(self):
        return [c.kwargs["text"] for c in self.fake.send_message.call_args_list]

    # ---------- membership events
    def test_join_is_stored_and_alerted(self):
        u = self.member_update(ChatMemberLeft(self.scholar), ChatMemberMember(self.scholar))
        run(self.bot.track_membership(u, self.ctx))
        rows = self.store().recent_events()
        self.assertEqual((rows[0]["event_type"], rows[0]["user_id"]), ("join", 42))
        self.assertIn("Member joined", self.sent_texts()[0])
        self.assertEqual(self.fake.send_message.call_args.kwargs["chat_id"], ADMIN_ID)

    def test_voluntary_leave(self):
        u = self.member_update(ChatMemberMember(self.scholar), ChatMemberLeft(self.scholar))
        run(self.bot.track_membership(u, self.ctx))
        self.assertEqual(self.store().count_events(event_type="leave"), 1)
        text = self.sent_texts()[0]
        self.assertIn("Member left", text)
        self.assertIn("IST", text)

    def test_removed_by_admin(self):
        u = self.member_update(
            ChatMemberMember(self.scholar), ChatMemberBanned(self.scholar, until_date=None),
            actor=self.admin_user,
        )
        run(self.bot.track_membership(u, self.ctx))
        text = self.sent_texts()[0]
        self.assertIn("Member removed", text)
        self.assertIn("By: Aakash", text)

    def test_duplicate_update_is_stored_once_and_alerted_once(self):
        old, new = ChatMemberMember(self.scholar), ChatMemberLeft(self.scholar)
        run(self.bot.track_membership(self.member_update(old, new), self.ctx))
        run(self.bot.track_membership(self.member_update(old, new), self.ctx))  # same event, new update_id
        self.assertEqual(self.store().count_events(), 1)
        self.assertEqual(self.fake.send_message.await_count, 1)

    def test_leave_then_rejoin_keeps_both_events(self):
        run(self.bot.track_membership(self.member_update(ChatMemberMember(self.scholar), ChatMemberLeft(self.scholar), ts=1), self.ctx))
        run(self.bot.track_membership(self.member_update(ChatMemberLeft(self.scholar), ChatMemberMember(self.scholar), ts=2), self.ctx))
        self.assertEqual([r["event_type"] for r in self.store().recent_events()], ["join", "leave"])

    def test_untracked_group_is_ignored(self):
        u = self.member_update(ChatMemberLeft(self.scholar), ChatMemberMember(self.scholar), chat_id=UNTRACKED)
        run(self.bot.track_membership(u, self.ctx))
        self.assertEqual(self.store().count_events(), 0)
        self.fake.send_message.assert_not_awaited()

    def test_promotion_is_stored_but_not_alerted(self):
        promoted = ChatMemberAdministrator(
            self.scholar, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
            can_delete_messages=False, can_manage_video_chats=False, can_restrict_members=False,
            can_promote_members=False, can_change_info=False, can_invite_users=True,
            can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
        )
        run(self.bot.track_membership(self.member_update(ChatMemberMember(self.scholar), promoted), self.ctx))
        self.assertEqual(self.store().count_events(event_type="status_change"), 1)
        self.fake.send_message.assert_not_awaited()

    def test_alert_failure_does_not_lose_event(self):
        self.fake.send_message.side_effect = Exception("Forbidden: bot was blocked by the user")
        u = self.member_update(ChatMemberMember(self.scholar), ChatMemberLeft(self.scholar))
        run(self.bot.track_membership(u, self.ctx))
        self.assertEqual(self.store().count_events(event_type="leave"), 1)
        self.assertEqual(self.app.bot_data["notify_stats"]["failed"], 1)

    def test_leave_only_alert_setting(self):
        with patch.object(self.bot, "MEMBERSHIP_ALERTS", {"leave"}):
            run(self.bot.track_membership(self.member_update(ChatMemberLeft(self.scholar), ChatMemberMember(self.scholar)), self.ctx))
        self.assertEqual(self.store().count_events(event_type="join"), 1)
        self.fake.send_message.assert_not_awaited()

    def test_html_in_names_is_escaped(self):
        evil = User(77, "<b>x</b>", False)
        run(self.bot.track_membership(self.member_update(ChatMemberLeft(evil), ChatMemberMember(evil)), self.ctx))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.sent_texts()[0])

    # ---------- admin commands
    def command_update(self, user_id, chat_type="private", chat_id=None):
        upd = MagicMock()
        upd.effective_user = SimpleNamespace(id=user_id)
        upd.effective_chat = SimpleNamespace(type=chat_type, id=chat_id or user_id)
        upd.message.reply_text = AsyncMock()
        return upd

    def test_reports_blocked_for_non_admin(self):
        for handler in (self.bot.recent_leaves, self.bot.recent_joins, self.bot.member_stats, self.bot.track_status):
            with self.subTest(handler=handler.__name__):
                upd = self.command_update(999)
                run(handler(upd, self.ctx))
                self.assertIn("authorized admins only", upd.message.reply_text.call_args.args[0])

    def test_reports_blocked_in_group_even_for_admin(self):
        upd = self.command_update(ADMIN_ID, chat_type="supergroup", chat_id=TRACKED)
        run(self.bot.recent_leaves(upd, self.ctx))
        self.assertIn("authorized admins only", upd.message.reply_text.call_args.args[0])

    def test_recent_leaves_and_stats_for_admin(self):
        run(self.bot.track_membership(self.member_update(ChatMemberMember(self.scholar), ChatMemberLeft(self.scholar)), self.ctx))
        upd = self.command_update(ADMIN_ID)
        run(self.bot.recent_leaves(upd, self.ctx))
        self.assertIn("riya_s", upd.message.reply_text.call_args.args[0])
        upd = self.command_update(ADMIN_ID)
        run(self.bot.member_stats(upd, self.ctx))
        self.assertIn("Announcements", upd.message.reply_text.call_args.args[0])

    # ---------- /setup lock
    def setup_update(self, user_id, chat_id, is_group_admin=True):
        upd = self.command_update(user_id, chat_type="supergroup", chat_id=chat_id)
        self.fake.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator" if is_group_admin else "member"))
        return upd

    def test_setup_rejects_group_admin_not_in_admin_list(self):
        upd = self.setup_update(555, -3003)
        run(self.bot.setup(upd, self.ctx))
        self.assertIn("authorized bot admins only", upd.message.reply_text.call_args.args[0])
        self.assertFalse(self.bot.DATA_FILE.exists())

    def test_setup_needs_confirm_to_move_polls(self):
        self.bot.DATA_FILE.write_text(json.dumps({"chat_id": -3003, "next_poll_at": 1.0}))
        self.ctx.args = []
        upd = self.setup_update(ADMIN_ID, TRACKED)
        run(self.bot.setup(upd, self.ctx))
        self.assertIn("/setup confirm", upd.message.reply_text.call_args.args[0])
        self.assertEqual(json.loads(self.bot.DATA_FILE.read_text())["chat_id"], -3003)

        self.ctx.args = ["confirm"]
        upd = self.setup_update(ADMIN_ID, TRACKED)
        run(self.bot.setup(upd, self.ctx))
        saved = json.loads(self.bot.DATA_FILE.read_text())
        self.assertEqual(saved["chat_id"], TRACKED)
        self.assertEqual(saved["next_poll_at"], 1.0)  # schedule untouched
        scope = self.fake.set_my_commands.call_args.kwargs["scope"]
        self.assertIsInstance(scope, BotCommandScopeChatAdministrators)
        self.assertEqual(scope.chat_id, TRACKED)


    # ---------- automatic schedule (send_random_poll)
    def run_auto_poll(self, scheduled_for):
        self.bot.DATA_FILE.write_text(json.dumps({"chat_id": TRACKED, "used_polls": []}))
        self.fake.send_message = AsyncMock(return_value=SimpleNamespace(message_id=5))
        self.app.job_queue = MagicMock()
        ctx = SimpleNamespace(application=self.app, bot=self.fake, job=None)
        run(self.bot.send_random_poll(ctx, update_schedule=True, scheduled_for=scheduled_for))
        saved = json.loads(self.bot.DATA_FILE.read_text())
        next_at = datetime.fromtimestamp(saved["next_poll_at"], tz=timezone.utc)
        job_times = [c.kwargs.get("when") for c in self.app.job_queue.run_once.call_args_list]
        return saved, next_at, job_times

    def test_schedule_normal_case_is_exactly_two_days_later(self):
        from datetime import timedelta
        slot = datetime.now(timezone.utc) - timedelta(minutes=1)
        saved, next_at, jobs = self.run_auto_poll(slot)
        self.assertEqual(next_at, slot + timedelta(days=2))  # same as production
        self.assertIn(next_at, jobs)
        self.assertEqual(len(saved["used_polls"]), 1)

    def test_schedule_skips_past_slots_after_long_downtime(self):
        from datetime import timedelta
        slot = self.bot.FIRST_POLL_AT  # 30 Sep 2026 18:30 IST, long past
        _, next_at, jobs = self.run_auto_poll(slot)
        now = datetime.now(timezone.utc)
        self.assertGreater(next_at, now)
        self.assertLessEqual(next_at, now + timedelta(days=2))
        self.assertEqual(next_at.astimezone(self.bot.IST).strftime("%H:%M"), "18:30")
        self.assertEqual((next_at - slot) % timedelta(days=2), timedelta(0))
        self.assertIn(next_at, jobs)
        self.assertEqual(self.fake.send_message.await_count, 1)  # one poll, no burst



class JoinRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = load_bot(self.tmp.name)
        self.store = self.bot.MembershipStore(os.environ["MEMBERSHIP_DB_PATH"])
        self.fake = MagicMock()
        for m in ("send_message", "approve_chat_join_request", "decline_chat_join_request"):
            setattr(self.fake, m, AsyncMock())
        self.members = {}  # (chat_id, user_id) -> status
        self.fake.get_chat_member = AsyncMock(side_effect=lambda c, u: SimpleNamespace(
            status=self.members.get((c, u), "left")))
        titles = {ANNOUNCE: "Announcements", INTRO: "Scholar Introduction", CAREER: "Career Guidance"}
        self.fake.get_chat = AsyncMock(side_effect=lambda c: SimpleNamespace(title=titles[c]))
        self.app = SimpleNamespace(bot_data={"membership_store": self.store}, bot=self.fake)
        self.ctx = SimpleNamespace(application=self.app, bot=self.fake, args=[])
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, chat_id, user_id=42, username="Riya_S"):
        self.n += 1
        user = User(user_id, "Riya", False, username=username)
        req = ChatJoinRequest(
            chat=Chat(chat_id, "supergroup", title={ANNOUNCE: "Announcements", INTRO: "Scholar Introduction",
                                                    CAREER: "Career Guidance"}.get(chat_id, "Other")),
            from_user=user, date=datetime(2026, 10, 14, 14, tzinfo=timezone.utc), user_chat_id=user_id,
        )
        run(self.bot.handle_join_request(Update(self.n, chat_join_request=req), self.ctx))

    def alerts(self):
        return [c for c in self.fake.send_message.call_args_list if "Join request" in c.kwargs["text"]]

    def admin_msg(self, args=None):
        upd = MagicMock()
        upd.effective_user = SimpleNamespace(id=ADMIN_ID)
        upd.effective_chat = SimpleNamespace(type="private", id=ADMIN_ID)
        upd.message.reply_text = AsyncMock()
        self.ctx.args = args or []
        return upd

    # ---- normalize
    def test_normalize_username_formats(self):
        n = self.bot.normalize_username
        for raw in ("@Riya_S", "riya_s", " https://t.me/Riya_S ", "t.me/riya_s?start=1", "http://telegram.me/Riya_S/"):
            self.assertEqual(n(raw), "riya_s", raw)
        for raw in ("", None, "riya s", "@ab", "+919876543210", "riya-s"):
            self.assertIsNone(n(raw), raw)

    # ---- landing groups
    def test_landing_group_listed_username_auto_approved(self):
        self.store.add_usernames(["riya_s"])
        self.request(ANNOUNCE, username="@RIYA_S" .lstrip("@"))
        self.fake.approve_chat_join_request.assert_awaited_once_with(chat_id=ANNOUNCE, user_id=42)
        self.assertEqual(self.store.get_join_request(ANNOUNCE, 42)["status"], "approved")
        self.assertEqual(self.alerts(), [])

    def test_landing_group_unknown_username_goes_to_admin(self):
        self.request(INTRO, username="stranger1")
        self.fake.approve_chat_join_request.assert_not_awaited()
        alert = self.alerts()[0]
        self.assertIn("not in the scholar list", alert.kwargs["text"])
        buttons = alert.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual(buttons[0].callback_data, f"jr:a:{INTRO}:42")
        self.assertEqual(self.store.get_join_request(INTRO, 42)["status"], "pending")

    def test_landing_group_no_username_goes_to_admin(self):
        self.request(ANNOUNCE, username=None)
        self.assertIn("No Telegram username", self.alerts()[0].kwargs["text"])

    # ---- other groups
    def test_other_group_approved_when_in_both_landing_groups(self):
        self.members[(ANNOUNCE, 42)] = "member"
        self.members[(INTRO, 42)] = "member"
        self.request(CAREER, username=None)  # no username needed here
        self.fake.approve_chat_join_request.assert_awaited_once_with(chat_id=CAREER, user_id=42)

    def test_other_group_pending_when_missing_a_landing_group(self):
        self.members[(ANNOUNCE, 42)] = "member"
        self.request(CAREER)
        self.fake.approve_chat_join_request.assert_not_awaited()
        self.assertIn("Not in: Scholar Introduction", self.alerts()[0].kwargs["text"])

    def test_listed_username_does_not_bypass_landing_rule(self):
        self.store.add_usernames(["riya_s"])
        self.request(CAREER)
        self.fake.approve_chat_join_request.assert_not_awaited()

    def test_group_not_in_approval_list_is_ignored(self):
        self.request(-9999)
        self.fake.approve_chat_join_request.assert_not_awaited()
        self.fake.send_message.assert_not_awaited()

    # ---- rechecks
    def test_allow_approves_existing_pending_request(self):
        self.request(ANNOUNCE)
        upd = self.admin_msg(["@Riya_S", "bad name!"])
        run(self.bot.allow_command(upd, self.ctx))
        self.fake.approve_chat_join_request.assert_awaited_once_with(chat_id=ANNOUNCE, user_id=42)
        reply = upd.message.reply_text.call_args.args[0]
        self.assertIn("Added: 1", reply)
        self.assertIn("approved now: 1", reply)
        self.assertIn("invalid", reply)

    def test_joining_landing_group_unlocks_other_group_request(self):
        self.request(CAREER)  # pending: not in landing groups
        self.members[(ANNOUNCE, 42)] = "member"
        self.members[(INTRO, 42)] = "member"
        user = User(42, "Riya", False, username="riya_s")
        change = ChatMemberUpdated(
            chat=Chat(INTRO, "supergroup", title="Scholar Introduction"), from_user=user,
            date=datetime(2026, 10, 14, 15, tzinfo=timezone.utc),
            old_chat_member=ChatMemberLeft(user), new_chat_member=ChatMemberMember(user),
        )
        run(self.bot.track_membership(Update(99, chat_member=change), self.ctx))
        self.fake.approve_chat_join_request.assert_awaited_once_with(chat_id=CAREER, user_id=42)

    # ---- buttons
    def press(self, data, user_id=ADMIN_ID):
        q = MagicMock()
        q.from_user = SimpleNamespace(id=user_id, full_name="Aakash")
        q.data = data
        q.message.text_html = "🙋 <b>Join request needs review</b>"
        for m in ("answer", "edit_message_text", "edit_message_reply_markup"):
            setattr(q, m, AsyncMock())
        upd = SimpleNamespace(callback_query=q)
        run(self.bot.join_request_button(upd, self.ctx))
        return q

    def test_admin_approve_button(self):
        self.request(INTRO, username="stranger1")
        q = self.press(f"jr:a:{INTRO}:42")
        self.fake.approve_chat_join_request.assert_awaited_once_with(chat_id=INTRO, user_id=42)
        self.assertIn("Approved", q.edit_message_text.call_args.args[0])
        self.assertEqual(self.store.get_join_request(INTRO, 42)["decided_by"], "Aakash")

    def test_admin_decline_button(self):
        self.request(INTRO, username="stranger1")
        self.press(f"jr:d:{INTRO}:42")
        self.fake.decline_chat_join_request.assert_awaited_once_with(chat_id=INTRO, user_id=42)
        self.assertEqual(self.store.get_join_request(INTRO, 42)["status"], "declined")

    def test_non_admin_cannot_press(self):
        self.request(INTRO, username="stranger1")
        q = self.press(f"jr:a:{INTRO}:42", user_id=555)
        self.fake.approve_chat_join_request.assert_not_awaited()
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_second_press_after_decision_does_nothing(self):
        self.request(INTRO, username="stranger1")
        self.press(f"jr:a:{INTRO}:42")
        q = self.press(f"jr:d:{INTRO}:42")  # another admin's old message
        self.fake.decline_chat_join_request.assert_not_awaited()
        self.assertIn("Already approved", q.answer.call_args.args[0])

    def test_request_already_handled_by_human(self):
        from telegram.error import BadRequest
        self.request(INTRO, username="stranger1")
        self.fake.approve_chat_join_request.side_effect = BadRequest("Hide_requester_missing")
        q = self.press(f"jr:a:{INTRO}:42")
        self.assertIn("Already handled", q.answer.call_args.args[0])
        self.assertEqual(self.store.get_join_request(INTRO, 42)["status"], "handled_elsewhere")

    # ---- CSV import
    def send_file(self, name, content):
        upd = self.admin_msg()
        f = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(content.encode("utf-8-sig"))))
        upd.message.document = SimpleNamespace(file_name=name, file_size=len(content), get_file=AsyncMock(return_value=f))
        run(self.bot.import_list_file(upd, self.ctx))
        return " | ".join(c.args[0] for c in upd.message.reply_text.call_args_list)

    def test_google_form_csv_import(self):
        csv_text = (
            "Timestamp,Full Name,Scholarship,Telegram Username / Profile Link\n"
            "14/10/2026,Riya,Kotak,@Riya_S\n"
            "14/10/2026,Aman,HDFC,https://t.me/aman_k\n"
            "14/10/2026,Neha,UGO,neha.k\n"
            "14/10/2026,Riya again,Kotak,riya_s\n"
            "14/10/2026,Blank,Kotak,\n"
        )
        reply = self.send_file("Scholar Telegram Form (Responses).csv", csv_text)
        self.assertIn("Telegram Username / Profile Link", reply)
        self.assertEqual(self.store.list_usernames(), ["aman_k", "riya_s"])
        self.assertIn("Added: 2", reply)
        self.assertIn("neha.k", reply)  # reported as invalid

    def test_csv_without_username_column(self):
        reply = self.send_file("x.csv", "Name,City\nRiya,Delhi\n")
        self.assertIn("Couldn't find the username column", reply)
        self.assertEqual(self.store.list_usernames(), [])

    def test_disallow(self):
        self.store.add_usernames(["riya_s"])
        upd = self.admin_msg(["@riya_s"])
        run(self.bot.disallow_command(upd, self.ctx))
        self.assertFalse(self.store.is_allowed("riya_s"))

    def test_list_commands_blocked_for_non_admin(self):
        for handler in (self.bot.allow_command, self.bot.disallow_command,
                        self.bot.allowlist_command, self.bot.pending_command):
            upd = self.admin_msg(["@x_user"])
            upd.effective_user = SimpleNamespace(id=999)
            run(handler(upd, self.ctx))
            self.assertIn("authorized admins only", upd.message.reply_text.call_args.args[0])
        self.assertEqual(self.store.list_usernames(), [])


if __name__ == "__main__":
    unittest.main()
