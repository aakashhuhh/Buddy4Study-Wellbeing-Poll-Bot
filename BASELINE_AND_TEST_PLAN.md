# Buddy4Study Wellbeing Poll Bot — Development Baseline

## Scope and safety
- `baseline/production_bot.py` is an unchanged reference copy of the old production bot (previously deployed as `bot.py`, before 21 Oct 2026).
- Root `app.py` is the single canonical development entrypoint, based on the complete saved `bot_newVersion.py` (the newer version includes the existing poll functionality plus the earlier admin/UI changes).
- The duplicate root `bot_newVersion.py` was removed to avoid competing entrypoints. The source version is preserved by the hash below and can be recreated from the uploaded original ZIP.
- This is a development consolidation, not a production deployment; the live host has not been accessed.
- This workspace intentionally excludes `.env`, `.git`, `poll_data.json`, bytecode, and live credentials/data.
- `membership_tracker.py` is a standalone storage prototype and is NOT integrated into either bot. No production service or Telegram API was contacted.

## Snapshot from the supplied ZIP
- `baseline/production_bot.py`: 907 lines; SHA-256 `3de30b552b6d996c26529f7862e96f5b394a3296033afb3de562602a8d35877e`
- root `app.py` (consolidated development entrypoint): 1598 lines; SHA-256 `e597e408a631fe506966a53301a32efab90fec06834cabe38fffc48d45810f7f`
- Both register `/start`, `/setup`, `/test`, `/next`, `/status`, `/results`, and `/help`.
- Newer version has explicit `ADMIN_USER_IDS`, admin/private checks for status/results, private test behavior, and a startup command-registration hook. These require behavioral review before deployment.
- Newer version defines a 20-question pool, a 2-day interval, a 24-hour response window, and a first-poll timestamp of 30 Sep 2026 at 18:30 IST. Confirm actual production settings against the running service before using any schedule from this development snapshot.
- `requirements.txt`: `python-telegram-bot[job-queue]==22.8`, `python-dotenv==1.2.3`, `apscheduler==3.11.3`.

## Membership tracking design (phase 1 only)
- Independent SQLite database (`membership_events.sqlite3`), never `poll_data.json`.
- Record group ID/title, user ID, username/display name, event type, old/new status, and UTC event timestamp.
- Support optional idempotency key to avoid duplicate processing.
- Convert UTC to Asia/Kolkata only when presenting reports.
- Telegram integration must later handle `chat_member` updates and require the bot to be an administrator with suitable permissions in each test group. Historical departures cannot be recovered retroactively.
- Admin alerts/reports must be private and restricted to an allowlist; avoid collecting unnecessary personal information.

## Test plan before integration
1. Unit-test SQLite writes, UTC normalization, duplicate suppression, filtering, and invalid input.
2. Test Telegram membership transitions in a separate test group with a separate test bot token and test database.
3. Verify bot admin permissions and update delivery (`chat_member` updates).
4. Regression-test all existing poll commands and the 24-hour poll close / two-day schedule in an isolated copy using a disposable poll data file.
5. Restart the test process and verify SQLite events persist; verify poll state remains independent.
6. Check logs for leaked tokens, IDs, or personal data; test error handling and admin authorization.
7. Produce a backup and rollback plan. Deploy only after explicit operator approval.

## Current status (14 Oct 2026)
- Membership tracking is now integrated into `app.py` (`chat_member` handler, private admin alerts, `/trackstatus`, `/memberstats`, `/recentleaves`, `/recentjoins`).
- Fixes: `/setup` limited to `ADMIN_USER_IDS` and needs `/setup confirm` to move polls to a different group; `/next` and `/setup` menu shown only in the Wellbeing group (old all-groups menu is cleared on startup).
- Schedule fix (approved change to `send_random_poll`): if the bot was offline more than 2 days, the next slot used to land in the past; the job queue silently skips past-dated jobs, so automatic polls stopped. Now the bot sends the one missed poll and moves to the next future 6:30 PM IST slot (no burst). Normal case unchanged: exactly 2 days later.
- Poll engine (other 14 functions, 20 questions, schedule settings) verified identical to `baseline/production_bot.py` by `tests/test_poll_regression.py`.
- Offline tests: 32 passing (tracker storage + classification, poll regression, bot integration with real python-telegram-bot 22.8 objects and a fake bot). Startup smoke test passes with a fake token.
- NOT yet done: live test in a separate Telegram test group with a test bot token; production deployment (needs explicit approval).
- Known limits: history starts when tracking is switched on; alert counters reset on restart; an alert that fails is logged and counted, not retried.

## Live test in test group (14 Oct 2026, test bot @Buddy4Study_Test_Bot)
Passed: /start, /help, /trackstatus (group ✅ admin), join stored without alert, leave alert, removed alert with "By", /setup lock behaviour, /next poll, /results in private chat, /status in private chat.
Found: past next-poll date after /setup → fixed in this version (see above). Pending: /recentleaves, /recentjoins, /memberstats, restart persistence check.

## Single-file layout (14 Oct 2026)
- `membership_tracker.py` merged into `app.py` because VisiHost accepts one .py file. No new libraries (sqlite3 is built into Python).
- Tests updated to import from `app.py`; 32 passing. Standalone startup of `app.py` alone verified.
- Live test status: all checks passed in the test group, including restart persistence and the schedule fix.

## Join request auto-approval (14 Oct 2026)
- Landing groups (MANDATORY_CHAT_IDS): approve if the Telegram username is in the scholar list (imported from the Google Form sheet CSV, or `/allow`).
- Other groups in APPROVAL_CHAT_IDS: approve if the person is already in ALL landing groups (checked by user ID; no username needed).
- Otherwise: request stays pending; admins get Approve/Decline buttons. `/pending` lists open requests.
- Re-checks: adding usernames approves matching pending requests; joining a landing group approves that person's pending requests in other groups.
- Limits: Telegram doesn't let bots list requests made before the bot was running (approve those by hand once). People without a username always go to manual review in landing groups.
- Tests: 51 offline tests passing (19 new for this feature). Live test in test group: pending.
