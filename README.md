# Buddy4Study Wellbeing Poll Bot + Membership Tracker (development)

- `app.py` — the ONLY file needed to run/deploy. Wellbeing polls + admin commands + membership tracking (SQLite storage built in, separate from `poll_data.json`). Single file because the host (VisiHost) accepts one .py file.
- `baseline/production_bot.py` — exact copy of the bot running live today. Do not edit. Used for rollback and regression tests.
- `tests/` — offline tests. Run: `python -m unittest discover -s tests -v`

## Admin commands (send in the bot's private chat)
`/status` `/results` `/test` `/help` — polls
`/trackstatus` `/memberstats` `/recentleaves` `/recentjoins` — membership
`/pending` `/allow` `/disallow` `/allowlist` — join requests (send the Google Form sheet as .csv to import usernames)

## Group commands (Wellbeing Support group only)
`/next` — send a poll now · `/setup` — connect the group (authorised admins only; moving to another group needs `/setup confirm`)

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in (use a TEST bot token while testing).
3. Make the bot an admin in every group listed in `TRACKED_CHAT_IDS`.
4. Admins must open the bot in private chat and press Start once, or alerts can't reach them.
5. `python app.py`

Never copy the production `.env` or `poll_data.json` into this folder for testing.
