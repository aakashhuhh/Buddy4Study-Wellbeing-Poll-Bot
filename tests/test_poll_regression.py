"""Guards the wellbeing poll engine against accidental changes.

Compares the poll logic in app.py with baseline/production_bot.py (the bot that
is live today). Comments and docstrings are ignored; any real code change to
these functions, or to the 20 questions / schedule, makes a test fail.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Poll engine: must stay identical to production.
POLL_ENGINE = [
    "default_data", "load_data", "save_data", "get_next_poll", "format_ist",
    "close_poll", "schedule_poll_expiry", "automatic_poll_job",
    "schedule_next_automatic_poll", "handle_poll_response",
    "format_poll_results", "split_message", "schedule_existing_poll_expiries",
    "setup_automatic_schedule",
]
# Approved, deliberate changes (each has behaviour tests in test_bot_integration.py):
#   send_random_poll — 14 Oct 2026: skip past slots so automatic polls never stop
#                      after long downtime (see test_schedule_* tests).
APPROVED_CHANGES = {"send_random_poll"}

SETTINGS = ["POLL_INTERVAL", "POLL_DURATION", "IST", "FIRST_POLL_AT", "DATA_FILE"]


def parse(path):
    tree = ast.parse(path.read_text(encoding="utf-8").replace("\r", ""))
    funcs, settings = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                node.body = body[1:] or [ast.Pass()]
            funcs[node.name] = ast.unparse(node)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            settings[node.targets[0].id] = ast.unparse(node.value)
    return funcs, settings


class PollRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prod_funcs, cls.prod_settings = parse(ROOT / "baseline" / "production_bot.py")
        cls.dev_funcs, cls.dev_settings = parse(ROOT / "app.py")

    def test_poll_engine_identical_to_production(self):
        for name in POLL_ENGINE:
            with self.subTest(function=name):
                self.assertIn(name, self.dev_funcs)
                self.assertEqual(self.dev_funcs[name], self.prod_funcs[name])

    def test_only_approved_poll_functions_changed(self):
        changed = {
            name for name, code in self.prod_funcs.items()
            if name in self.dev_funcs and self.dev_funcs[name] != code
        }
        command_layer = {"results", "is_admin", "start", "setup", "test_poll",
                         "next_poll", "help_command", "status", "main"}
        self.assertEqual(changed - command_layer, APPROVED_CHANGES)

    def test_poll_settings_identical_to_production(self):
        for name in SETTINGS:
            with self.subTest(setting=name):
                self.assertEqual(self.dev_settings[name], self.prod_settings[name])

    def test_original_20_questions_unchanged_and_50_total(self):
        # 21 Oct 2026: 30 polls added at the END, so old poll numbers in
        # poll_data.json (used_polls, results) still point to the same questions.
        prod = ast.literal_eval(self.prod_settings["POLLS"])
        dev = ast.literal_eval(self.dev_settings["POLLS"])
        self.assertEqual(len(prod), 20)
        self.assertEqual(dev[:20], prod)
        self.assertEqual(len(dev), 50)
        self.assertEqual(len({p["question"] for p in dev}), 50)
        for poll in dev:
            self.assertTrue(2 <= len(poll["options"]) <= 10)
            self.assertEqual(len(set(poll["options"])), len(poll["options"]))

    def test_membership_code_does_not_touch_poll_data(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        names = {"MembershipStore", "is_present", "classify_membership_change", "track_membership"}
        code = [ast.unparse(n) for n in tree.body if getattr(n, "name", None) in names]
        self.assertEqual(len(code), len(names))
        for chunk in code:
            # strip docstrings/comments are already gone after unparse; check real code
            self.assertNotIn("DATA_FILE", chunk)
            self.assertNotIn("load_data", chunk)
            self.assertNotIn("save_data", chunk)

if __name__ == "__main__":
    unittest.main()
