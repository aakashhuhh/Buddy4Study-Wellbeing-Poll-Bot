import json
import os
import random
from datetime import datetime, timezone
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

# Poll will be posted every 2 days
POLL_INTERVAL_DAYS = 2

# --------------------------------------------------
# WELLBEING POLL POOL
# --------------------------------------------------

POLLS = [
    {
        "question": "How are you feeling today? 💙",
        "options": ["😊 Great", "🙂 Good", "😐 Okay", "😔 Not great", "🫂 Need some support"],
    },
    {
        "question": "What helps you relax after a busy day? 🌿",
        "options": ["🎵 Music", "🚶 Walking", "📱 Entertainment", "😴 Rest", "💬 Talking to someone"],
    },
    {
        "question": "How would you rate your energy level today? ⚡",
        "options": ["⚡ Very high", "🙂 Good", "😐 Average", "😴 Low"],
    },
    {
        "question": "What usually helps you deal with stress? 🧘",
        "options": ["🎵 Music", "🏃 Exercise", "😴 Rest", "💬 Talking", "🧘 Quiet time"],
    },
    {
        "question": "How supported do you feel this week? 💙",
        "options": ["🫶 Very supported", "🙂 Supported", "😐 Neutral", "😔 Not much", "🫂 Need more support"],
    },
    {
        "question": "What is one thing that improves your mood? ☀️",
        "options": ["🎵 Music", "👥 Friends", "🏃 Exercise", "🎬 Movies/Shows", "🌿 Time alone"],
    },
    {
        "question": "How well have you been balancing studies/work and personal time? ⚖️",
        "options": ["😊 Very well", "🙂 Well", "😐 It's okay", "😕 Not very well"],
    },
    {
        "question": "What helps you stay motivated? 🚀",
        "options": ["🎯 Goals", "👨‍👩‍👧 Family", "👥 Friends", "💪 Self-discipline", "🌟 Inspiration"],
    },
    {
        "question": "How has your week been so far? 🌱",
        "options": ["🌟 Excellent", "😊 Good", "😐 Average", "😕 Difficult", "🫂 Need a break"],
    },
    {
        "question": "Which activity helps you recharge the most? 🔋",
        "options": ["😴 Sleeping", "🎵 Music", "🏃 Exercise", "🎮 Entertainment", "👥 Socialising"],
    },
    {
        "question": "How often do you take time for yourself? 🌿",
        "options": ["🌟 Every day", "🙂 Often", "😐 Sometimes", "😕 Rarely"],
    },
    {
        "question": "What would help make this week better? 💙",
        "options": ["😴 More rest", "📚 Less workload", "👥 More social time", "🏃 More activity", "🌿 More personal time"],
    },
    {
        "question": "How are you managing your current workload? 📚",
        "options": ["💪 Very well", "🙂 Well", "😐 Manageable", "😓 Difficult"],
    },
    {
        "question": "What do you prefer when you need a mental break? 🧠",
        "options": ["🎵 Music", "🚶 Walk", "😴 Sleep", "🎬 Watch something", "🌿 Quiet time"],
    },
    {
        "question": "How positive do you feel about the coming week? ☀️",
        "options": ["🌟 Very positive", "😊 Positive", "😐 Neutral", "😕 A little worried"],
    },
    {
        "question": "Which helps you feel more refreshed? 🌱",
        "options": ["😴 Good sleep", "💧 Staying hydrated", "🏃 Physical activity", "🎵 Music", "🌿 Time outdoors"],
    },
    {
        "question": "How often do you connect with friends or people you trust? 💬",
        "options": ["😊 Very often", "🙂 Often", "😐 Sometimes", "😕 Rarely"],
    },
    {
        "question": "What is your preferred way to unwind? 🌙",
        "options": ["🎵 Music", "📖 Reading", "🎬 Movies/Shows", "🎮 Games", "😴 Sleep"],
    },
    {
        "question": "How confident are you about handling challenges this week? 💪",
        "options": ["💪 Very confident", "🙂 Confident", "😐 Not sure", "😕 A little worried"],
    },
    {
        "question": "What's one small thing that made you happy recently? 😊",
        "options": ["👥 Someone's support", "🎉 Good news", "🏆 Small achievement", "🌿 A peaceful moment", "☀️ Something unexpected"],
    },
]

# --------------------------------------------------
# DATA STORAGE
# --------------------------------------------------

def load_data():
    if not DATA_FILE.exists():
        return {
            "chat_id": None,
            "used_polls": [],
            "total_sent": 0,
            "next_poll_at": None,
            "poll_instances": [],
        }

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {
            "chat_id": None,
            "used_polls": [],
            "total_sent": 0,
            "next_poll_at": None,
            "poll_instances": [],
        }


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


# --------------------------------------------------
# GET NEXT RANDOM POLL
# --------------------------------------------------

def get_next_poll(data):
    used = set(data["used_polls"])

    available = [
        i for i in range(len(POLLS))
        if i not in used
    ]

    # If every poll has been used, start a new shuffled cycle
    if not available:
        data["used_polls"] = []
        available = list(range(len(POLLS)))

    poll_index = random.choice(available)

    data["used_polls"].append(poll_index)

    return poll_index


# --------------------------------------------------
# SEND POLL
# --------------------------------------------------

async def send_random_poll(
    context: ContextTypes.DEFAULT_TYPE,
    update_schedule: bool = True,
):
    data = load_data()
    chat_id = data.get("chat_id")

    if not chat_id:
        print("No chat ID configured yet.")
        return

    poll_index = get_next_poll(data)
    poll = POLLS[poll_index]

    try:
        # IMPORTANT:
        # This is a custom, non-anonymous poll using inline buttons.
        # Telegram's native send_poll() is not used.
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
        )

        message = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🌿 <b>Weekly Wellbeing Check-in</b>\n\n"
                f"{poll['question']}\n\n"
                f"{warning}"
                "<b>Choose one option:</b>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        # Store this specific poll instance so admins can see
        # exactly who responded to each posted poll.
        data.setdefault("poll_instances", []).append({
            "message_id": message.message_id,
            "poll_index": poll_index,
            "question": poll["question"],
            "options": poll["options"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "responses": {},
        })

        # Keep the file reasonably sized.
        if len(data["poll_instances"]) > 100:
            data["poll_instances"] = data["poll_instances"][-100:]

        data["total_sent"] += 1

        # Only automatic polls move the 2-day schedule.
        # Manual /test and /next do not change the next automatic poll time.
        if update_schedule:
            data["next_poll_at"] = (
                datetime.now(timezone.utc).timestamp()
                + POLL_INTERVAL_DAYS * 24 * 60 * 60
            )

        save_data(data)
        print(f"Poll sent: #{poll_index + 1}")

    except Exception as e:
        print(f"Error sending poll: {e}")


async def handle_poll_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Record a scholar's non-anonymous response to a custom poll."""
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


def format_poll_results(instance):
    """Create an admin-only summary for one poll instance."""
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
    """Split a long Telegram message without breaking lines."""
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
            # A single unusually long line is split safely.
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

    # Delete the /results command from the group so it does not remain visible.
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await context.bot.send_message(
            chat_id=admin_user.id,
            text="🔐 <b>Admin-only results</b>\n\n"
                 "The latest wellbeing poll results are below.",
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
        # The bot cannot DM a user until that user has started the bot.
        # Do not expose any poll data in the group.
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
    save_data(data)

    await update.message.reply_text(
        "✅ Wellbeing Support group connected!\n\n"
        f"📊 Polls available: {len(POLLS)}\n"
        "⏱️ Schedule: Every 2 days\n"
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
        "⏱️ Automatic polls: Every 2 days\n"
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

    if next_poll_at:
        next_poll_text = datetime.fromtimestamp(
            next_poll_at, tz=timezone.utc
        ).astimezone().strftime("%d %b %Y, %I:%M %p")
    else:
        next_poll_text = "Not scheduled yet"

    await update.message.reply_text(
        "🌿 Wellbeing Poll Bot Status\n\n"
        f"📊 Total polls: {len(POLLS)}\n"
        f"🎲 Used in current cycle: {used}\n"
        f"🔄 Remaining before reset: {remaining}\n"
        f"📨 Total polls sent: {total_sent}\n"
        f"⏱️ Schedule: Every 2 days\n"
        f"🗓️ Next automatic poll: {next_poll_text}"
    )


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
    application.add_handler(CallbackQueryHandler(handle_poll_response, pattern=r"^wb:\d+:\d+$"))

    # Persistent 2-day schedule. If the bot restarts, it keeps the
    # previously saved next poll time instead of resetting the timer.
    data = load_data()
    now = datetime.now(timezone.utc).timestamp()
    next_poll_at = data.get("next_poll_at")

    if next_poll_at is None:
        # First startup: create and persist the next automatic poll time.
        next_poll_at = now + POLL_INTERVAL_DAYS * 24 * 60 * 60
        data["next_poll_at"] = next_poll_at
        save_data(data)
        first_delay = POLL_INTERVAL_DAYS * 24 * 60 * 60
    else:
        # If the bot was offline past the scheduled time, send the missed
        # poll as soon as the bot starts. Otherwise wait until the saved time.
        first_delay = max(0, next_poll_at - now)

    application.job_queue.run_repeating(
        send_random_poll,
        interval=POLL_INTERVAL_DAYS * 24 * 60 * 60,
        first=first_delay,
    )

    print("======================================")
    print("Buddy4Study Wellbeing Poll Bot")
    print("======================================")
    print(f"Polls in pool: {len(POLLS)}")
    print("Schedule: Every 2 days")
    print("Random selection: ON")
    print("No-repeat cycle: ON")
    print("Persistent schedule: ON")
    print("Custom non-anonymous polls: ON")
    print("Admin-only results: ON")
    print("Bot is running...")
    print("======================================")

    application.run_polling()


if __name__ == "__main__":
    main()
