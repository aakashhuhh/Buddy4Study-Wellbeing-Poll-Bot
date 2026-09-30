import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found in .env file")

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "poll_data.json"

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
            data["next_poll_at"] = next_poll_at.timestamp()

        save_data(data)

        # Schedule the 24-hour closing.
        schedule_poll_expiry(context.application, instance)

        # Schedule the next automatic poll only for automatic polls.
        if update_schedule:
            schedule_next_automatic_poll(
                context.application,
                scheduled_for + POLL_INTERVAL,
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
    """Send the latest poll's results privately to the requesting admin."""
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "Please use /results inside the Wellbeing Support group."
        )
        return

    data = load_data()
    instances = data.get("poll_instances", [])

    if not instances:
        await update.message.reply_text(
            "No custom wellbeing poll has been sent yet."
        )
        return

    admin_user = update.effective_user

    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await context.bot.send_message(
            chat_id=admin_user.id,
            text=(
                "🔐 <b>Admin-only results</b>\n\n"
                "The latest wellbeing poll results are below."
            ),
            parse_mode="HTML",
        )

        latest = instances[-1]

        for chunk in split_message(format_poll_results(latest)):
            await context.bot.send_message(
                chat_id=admin_user.id,
                text=chunk,
                parse_mode="HTML",
            )

    except Exception:
        # Never expose poll results in the group.
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "⚠️ I couldn't send the results privately. "
                "Please open the Buddy4Study Wellbeing Poll Bot in a private chat, "
                "press <b>Start</b>, and then run /results again in this group."
            ),
            parse_mode="HTML",
        )


# --------------------------------------------------
# ADMIN CHECK
# --------------------------------------------------

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return False

    if not update.effective_user:
        return False

    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id,
    )

    return member.status in ["administrator", "creator"]


# --------------------------------------------------
# /START
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌿 Buddy4Study Wellbeing Poll Bot is active!\n\n"
        "Add me to your Wellbeing Support group and use /setup there."
    )


# --------------------------------------------------
# /SETUP
# --------------------------------------------------

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "Please use /setup inside the Wellbeing Support group."
        )
        return

    data = load_data()
    data["chat_id"] = update.effective_chat.id

    # If this is a fresh setup, the first automatic slot is today at 6:30 PM IST.
    if data.get("next_poll_at") is None:
        data["next_poll_at"] = FIRST_POLL_AT.timestamp()

    save_data(data)

    await update.message.reply_text(
        "✅ Wellbeing Support group connected!\n\n"
        f"📊 Polls available: {len(POLLS)}\n"
        "⏱️ Schedule: Every 2 days at 6:30 PM IST\n"
        "⏳ Each poll: Open for 24 hours\n"
        "🎲 Selection: Random\n"
        "🚫 Repeats: Only after the full poll pool is used\n\n"
        "Use /test to send a test poll now."
    )


# --------------------------------------------------
# /TEST
# --------------------------------------------------

async def test_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "Please use /test inside the Wellbeing Support group."
        )
        return

    data = load_data()

    if data.get("chat_id") != update.effective_chat.id:
        data["chat_id"] = update.effective_chat.id
        save_data(data)

    await send_random_poll(context, update_schedule=False)


# --------------------------------------------------
# /NEXT
# --------------------------------------------------

async def next_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "Please use /next inside the Wellbeing Support group."
        )
        return

    data = load_data()

    if data.get("chat_id") != update.effective_chat.id:
        data["chat_id"] = update.effective_chat.id
        save_data(data)

    await send_random_poll(context, update_schedule=False)


# --------------------------------------------------
# /HELP
# --------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    await update.message.reply_text(
        "🌿 Buddy4Study Wellbeing Poll Bot\n\n"
        "Admin commands:\n\n"
        "/test — Send a random poll for testing\n"
        "/next — Send the next random unused poll\n"
        "/status — View poll and schedule status\n"
        "/results — Send latest poll results privately (admins only)\n"
        "/help — Show available commands\n\n"
        "⏱️ Automatic polls: Every 2 days at 6:30 PM IST\n"
        "⏳ Poll duration: 24 hours\n"
        "🎲 Random selection: ON\n"
        "🚫 No-repeat cycle: ON"
    )


# --------------------------------------------------
# /STATUS
# --------------------------------------------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ This command is available to group admins only."
        )
        return

    data = load_data()

    used = len(data.get("used_polls", []))
    remaining = len(POLLS) - used
    total_sent = data.get("total_sent", 0)
    next_poll_at = data.get("next_poll_at")

    next_poll_text = format_ist(next_poll_at)

    await update.message.reply_text(
        "🌿 Wellbeing Poll Bot Status\n\n"
        f"📊 Total polls: {len(POLLS)}\n"
        f"🎲 Used in current cycle: {used}\n"
        f"🔄 Remaining before reset: {remaining}\n"
        f"📨 Total polls sent: {total_sent}\n"
        "⏱️ Schedule: Every 2 days at 6:30 PM IST\n"
        "⏳ Poll duration: 24 hours\n"
        f"🗓️ Next automatic poll: {next_poll_text}"
    )


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
            when=0.1,
            data={"scheduled_for": scheduled_for},
            name=f"missed_automatic_poll_{int(scheduled_for.timestamp())}",
        )
    else:
        schedule_next_automatic_poll(application, scheduled_for)


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setup", setup))
    application.add_handler(CommandHandler("test", test_poll))
    application.add_handler(CommandHandler("next", next_poll))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("results", results))
    application.add_handler(CommandHandler("help", help_command))
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
    print("======================================")

    # Restore expiry jobs for polls that were active before a restart.
    schedule_existing_poll_expiries(application, data)

    # Restore the next automatic poll.
    setup_automatic_schedule(application, data)

    print("Bot is running...")
    print("======================================")

    application.run_polling()


if __name__ == "__main__":
    main()
