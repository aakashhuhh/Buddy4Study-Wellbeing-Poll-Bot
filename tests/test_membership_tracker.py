import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import sys

# Storage code now lives inside app.py (single-file deploy). Importing app.py
# needs a token, so use a fake one; nothing connects to Telegram.
os.environ.setdefault("BOT_TOKEN", "123:TEST-TOKEN-NOT-REAL")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import MembershipStore, classify_membership_change

class MembershipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test_membership.sqlite3"
        self.store = MembershipStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, **overrides):
        args = dict(chat_id=-100123, chat_title="Test Group", user_id=42,
                    username="scholar", display_name="Test Scholar", event_type="join",
                    occurred_at=datetime(2026, 10, 13, 10, tzinfo=timezone(timedelta(hours=5, minutes=30))),
                    update_key="update-1")
        args.update(overrides)
        return args

    def test_records_event_and_normalizes_timestamp_to_utc(self):
        self.assertTrue(self.store.record_event(**self.event()))
        rows = self.store.recent_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurred_at_utc"], "2026-10-13T04:30:00+00:00")

    def test_duplicate_update_key_is_ignored(self):
        self.assertTrue(self.store.record_event(**self.event()))
        self.assertFalse(self.store.record_event(**self.event(user_id=99)))
        self.assertEqual(self.store.count_events(), 1)

    def test_filters_by_group_and_event_type(self):
        self.store.record_event(**self.event())
        self.store.record_event(**self.event(user_id=43, event_type="leave", update_key="update-2"))
        self.assertEqual(self.store.count_events(chat_id=-100123, event_type="join"), 1)
        self.assertEqual(self.store.count_events(chat_id=-100999), 0)

    def test_rejects_invalid_event_type(self):
        with self.assertRaises(ValueError):
            self.store.record_event(**self.event(event_type="ban"))

    def test_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):
            self.store.record_event(**self.event(occurred_at=datetime(2026, 10, 13)))

    def test_limit_validation(self):
        with self.assertRaises(ValueError):
            self.store.recent_events(limit=0)

    def test_recent_events_filter_by_type(self):
        self.store.record_event(**self.event())
        self.store.record_event(**self.event(user_id=43, event_type="leave", update_key="update-2"))
        leaves = self.store.recent_events(event_type="leave")
        self.assertEqual([r["user_id"] for r in leaves], [43])
        with self.assertRaises(ValueError):
            self.store.recent_events(event_type="ban")

    def test_stats_by_chat_and_since(self):
        old = datetime(2026, 9, 1, tzinfo=timezone.utc)
        new = datetime(2026, 10, 13, tzinfo=timezone.utc)
        self.store.record_event(**self.event(occurred_at=old, update_key="a"))
        self.store.record_event(**self.event(occurred_at=new, event_type="leave", update_key="b"))
        self.store.record_event(**self.event(chat_id=-100555, chat_title="Other", occurred_at=new, update_key="c"))
        all_time = {r["chat_id"]: r for r in self.store.stats_by_chat()}
        self.assertEqual((all_time[-100123]["joins"], all_time[-100123]["leaves"]), (1, 1))
        recent = {r["chat_id"]: r for r in self.store.stats_by_chat(since=datetime(2026, 10, 1, tzinfo=timezone.utc))}
        self.assertEqual((recent[-100123]["joins"], recent[-100123]["leaves"]), (0, 1))
        self.assertEqual(self.store.first_event_time(), old.isoformat())

    def test_data_survives_reopen(self):
        self.store.record_event(**self.event())
        reopened = MembershipStore(self.db)
        self.assertEqual(reopened.count_events(), 1)


class ClassifyMembershipChangeTests(unittest.TestCase):
    def test_transitions(self):
        cases = [
            (("left", None, "member", None), "join"),
            (("kicked", None, "member", None), "join"),
            (("member", None, "left", None), "leave"),
            (("member", None, "kicked", None), "leave"),
            (("administrator", None, "left", None), "leave"),
            (("member", None, "administrator", None), "status_change"),
            (("left", None, "kicked", None), "status_change"),        # banned after leaving: not a 2nd leave
            (("member", None, "restricted", True), "status_change"),  # muted but still inside
            (("restricted", True, "restricted", False), "leave"),     # restricted user left
            (("left", None, "restricted", True), "join"),
            (("member", None, "member", None), None),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(classify_membership_change(*args), expected)


if __name__ == "__main__":
    unittest.main()
