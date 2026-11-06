from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from drives import store, telegram
from drives.store import Drive

import config
import database
import logging
import re
import sys
import threading
import time

# Telegram bot for logging real drives. Tap a button when you set off and when you've parked
# (or got home); Telegram stamps each message to the second, and that's the time logged. After
# an arrival it asks which route you took. It runs as a thread of the collector (main.py calls
# start) that asks Telegram for new messages, so nothing on the server is reachable from
# outside, and a failure is logged and never stops sampling.
#
# Only one copy can poll a bot at a time; to run it on its own (to test
# it, or before the collector is updated), from backend/app:
#   python -m drives.bot

# Seconds Telegram holds a poll open waiting for a message, so replies are near-instant
poll_s: int = 50

# Seconds to wait after a failed poll (Wi-Fi or Telegram down), doubling up to the max
retry_s: int = 5
max_retry_s: int = 300

# Reached ends the drive started last if it set off within this long before, and /adddrive
# takes a drive from the same place within this long of another for the same one. Anything
# longer is a forgotten tap, not a drive
max_drive: timedelta = timedelta(hours=3)

# /undo only takes back a drive logged this recently, so repeating it can't wipe the log
undo_within: timedelta = timedelta(hours=24)

# Button text -> where a drive sets off from; None (Reached) ends the drive in progress,
# wherever it was going. The keyboard stays under the chat
buttons = {"Home -> UNI": "HOME", "UNI -> Home": "UNI", "Reached": None}
keyboard = {"keyboard": [["Home -> UNI", "UNI -> Home"], ["Reached"]],
            "is_persistent": True, "resize_keyboard": True}

# /adddrive FROM LEFT ARRIVED [DAY]. Times are 24-hour H:MM, or 12-hour with am/pm attached
# (4:40pm), so nothing is guessed
add_format = "/adddrive FROM LEFT ARRIVED [DAY]"
add_usage = (
    f"Add a drive you didn't tap, or fix a logged drive's times:\n{add_format}\n\n"
    "FROM: home or uni, where you set off\n"
    "LEFT, ARRIVED: 24-hour times, e.g. 07:31 or 16:40 (4:40pm works too)\n"
    "DAY: today if left out, or yesterday, a weekday (mon = the latest Monday), or 2026-11-04\n\n"
    "e.g. /adddrive home 07:31 08:19\n"
    "or /adddrive uni 15:30 16:40 yesterday"
)
clock_pattern = re.compile(r"(\d{1,2}):(\d{2})(am|pm)?", re.IGNORECASE)
weekday_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Opens the reply box under /adddrive's message, so the fields can be sent without the command
add_reply = {"force_reply": True, "input_field_placeholder": "home 07:31 08:19"}

place_names = {"HOME": "home", "UNI": "uni"}
toward = {"HOME": "home", "UNI": "to uni"}
other = {"HOME": "UNI", "UNI": "HOME"}

# Route question after an arrival: callback answer -> (allow_tolls, had_stop, reply)
route_answers = {
    "free": (0, False, "Toll-free."),
    "tolls": (1, False, "Toll roads."),
    "stop": (None, True, "Stopped on the way, so it's left out of the style check."),
}

# The menu behind "/" in the chat
commands = [
    {"command": "adddrive", "description": "Log a drive you didn't tap, or fix one: /adddrive home 07:31 08:19"},
    {"command": "drives", "description": "The latest drives"},
    {"command": "undo", "description": "Take back the last tap, or the last added drive"},
    {"command": "help", "description": "How logging works"},
]

help_text = (
    "Tap Home -> UNI or UNI -> Home as you set off, and Reached once you've parked: it ends the drive"
    " you started. The moment you tap is the time logged, and tapping the same start again replaces it.\n\n"
    "Forgot to tap, or a time is wrong? /adddrive logs a drive with its times on a 24-hour clock, e.g."
    " /adddrive home 07:31 08:19, or /adddrive uni 15:30 16:40 yesterday.\n\n"
    "/undo takes back the last tap, or the last added drive. /drives shows the latest drives."
)

logger = logging.getLogger("commuteiq")


# TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID as a number (None if unset or not a number)
def settings() -> tuple[str | None, int | None]:
    token, chat = config.env("TELEGRAM_BOT_TOKEN"), config.env("TELEGRAM_CHAT_ID")
    if chat is not None and not re.fullmatch(r"-?\d+", chat):
        logger.error(f"TELEGRAM_CHAT_ID must be the number the bot replies with (got {chat!r}): logging nothing until then")
        chat = None
    return token, int(chat) if chat else None


def hhmm(at: datetime) -> str:
    return f"{at:%H:%M}"


def minutes(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds() / 60)


# "15:30–16:40 (70 min)"
def times(drive: Drive) -> str:
    if drive.arrival is None:
        return f"left {hhmm(drive.departure)}, no arrival yet"
    return f"{hhmm(drive.departure)}–{hhmm(drive.arrival)} ({minutes(drive.departure, drive.arrival)} min)"


# "Wed Nov 4, uni → home, 15:30–16:40 (70 min)"
def describe(drive: Drive) -> str:
    return (f"{drive.departure:%a %b} {drive.departure.day}, {place_names[drive.origin]} →"
            f" {place_names[drive.destination]}, {times(drive)}")


# The route question under a finished drive's reply, unless it's been answered
def asking_route(text: str, drive: Drive) -> tuple[str, dict | None]:
    if drive.allow_tolls is not None or drive.had_stop:
        return text, None
    return f"{text}\nWhich way did you go?", {"inline_keyboard": [
        [{"text": "Toll-free", "callback_data": f"route {drive.id} free"},
         {"text": "Toll roads", "callback_data": f"route {drive.id} tolls"}],
        [{"text": "I stopped on the way", "callback_data": f"route {drive.id} stop"}],
    ]}


# A start button: a new drive from place, or a new time for the one just started there
def set_off(conn, place: str, at: datetime) -> str:
    last = next(iter(store.latest(conn)), None)
    in_progress = last if last and last.arrival is None and abs(at - last.departure) < max_drive else None

    if in_progress and in_progress.origin == place:
        store.set_departure(conn, in_progress.id, at)
        return f"Changed: left {place_names[place]} at {hhmm(at)} (was {hhmm(in_progress.departure)})."

    store.add_drive(conn, database.get_location(conn, place), database.get_location(conn, other[place]), at)
    text = f"Left {place_names[place]} at {hhmm(at)}. Tap Reached when you get {toward[other[place]]}."
    if in_progress:
        text += (f"\nThe drive from {place_names[in_progress.origin]} at {hhmm(in_progress.departure)} never got"
                 " Reached, so it's left out; /adddrive can fill in its times.")
    return text


# Reached: ends the drive in progress, wherever it was going
def reach(conn, at: datetime) -> tuple[str, dict | None]:
    last = next(iter(store.latest(conn)), None)
    if last is None or last.arrival is not None or not timedelta(0) < at - last.departure < max_drive:
        if last and last.arrival is not None:
            hint = f" You reached {place_names[last.destination]} at {hhmm(last.arrival)}." \
                if timedelta(0) < at - last.arrival < max_drive else ""
        elif last:
            hint = (f" The one from {place_names[last.origin]} at {hhmm(last.departure)} set off over"
                    f" {max_drive.total_seconds() / 3600:g} h ago.")
        else:
            hint = ""
        return (f"No drive in progress.{hint} Tap Home -> UNI or UNI -> Home as you set off, or log one you"
                " didn't tap with /adddrive."), None

    store.set_arrival(conn, last, at)
    return asking_route(f"Reached {place_names[last.destination]} at {hhmm(at)}: {minutes(last.departure, at)} min"
                        f" from {place_names[last.origin]}, left {hhmm(last.departure)}.", last)


# "07:31", "16:40", "4:40pm" -> minutes after midnight, or None. Without am/pm the clock is
# 24-hour, so 4:40 is 04:40
def clock_minutes(text: str) -> int | None:
    if not (match := clock_pattern.fullmatch(text)):
        return None
    hour, minute, half = int(match[1]), int(match[2]), (match[3] or "").lower()
    if minute > 59:
        return None
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute


# "today", "yesterday", "mon" or "monday" (the latest one, today included), "2026-11-04"
def day_named(text: str, today: date) -> date | None:
    text = text.lower()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    if len(text) >= 3 and (index := next((i for i, name in enumerate(weekday_names) if name.startswith(text)), None)) is not None:
        return today - timedelta(days=(today.weekday() - index) % 7)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def retry(problem: str) -> tuple[str, dict]:
    return (f"{problem}\n\nFormat: {add_format}, e.g. /adddrive home 07:31 08:19. Reply to this message to"
            " try again."), add_reply


# /adddrive's fields: a new drive, or new times for the same drive logged before (from the same
# place, setting off within max_drive): an open one gets completed, a finished one corrected
def add(conn, fields: list[str], sent: datetime) -> tuple[str, dict | None]:
    if not fields:
        return f"{add_usage}\n\nReply to this message with FROM LEFT ARRIVED [DAY].", add_reply
    if len(fields) not in (3, 4) or fields[0].lower() not in ("home", "uni"):
        return retry("That isn't FROM LEFT ARRIVED [DAY]: FROM is home or uni, then two times.")

    place = fields[0].upper()
    left, arrived = clock_minutes(fields[1]), clock_minutes(fields[2])
    if left is None or arrived is None:
        return retry(f"Times are 24-hour, like 07:31 or 16:40 (or 4:40pm); got {fields[1]} and {fields[2]}.")
    if (day := day_named(fields[3], sent.date()) if len(fields) == 4 else sent.date()) is None:
        return retry(f"DAY is today, yesterday, a weekday like mon, or a date like 2026-11-04; got {fields[3]}.")

    midnight = datetime.combine(day, datetime.min.time())
    departure, arrival = midnight + timedelta(minutes=left), midnight + timedelta(minutes=arrived)
    if arrival <= departure:
        return retry(f"Arriving at {hhmm(arrival)} isn't after leaving at {hhmm(departure)}. The clock is 24-hour:"
                     " 4:40 in the afternoon is 16:40, or 4:40pm.")
    if arrival - departure >= max_drive:
        return retry(f"That's {minutes(departure, arrival)} min, and a drive is logged only under"
                     f" {max_drive.total_seconds() / 3600:g} h.")
    if arrival > sent:
        return retry(f"That drive ends after now ({sent:%a} {hhmm(sent)}).")

    nearby = store.on_dates(conn, [f"{day + timedelta(days=shift)}" for shift in (-1, 0, 1)])
    same = min((drive for drive in nearby if drive.origin == place and abs(drive.departure - departure) < max_drive),
               key=lambda drive: abs(drive.departure - departure), default=None)

    if same is None:
        drive_id = store.add_drive(conn, database.get_location(conn, place), database.get_location(conn, other[place]),
                                   departure, arrival)
        drive = store.get(conn, drive_id)
        return asking_route(f"Added: {describe(drive)}.", drive)

    store.set_times(conn, same.id, departure, arrival)
    drive = store.get(conn, same.id)
    return asking_route(f"Updated: {describe(drive)}. Before: {times(same)}.", drive)


# A tap on the route question, e.g. "route 12 tolls"
def answer_route(conn, data: str) -> str:
    kind, drive_id, answer = (data.split() + ["", "", ""])[:3]
    if kind != "route" or not drive_id.isdigit() or answer not in route_answers:
        return "That button is from an older version; nothing was saved."
    if store.get(conn, int(drive_id)) is None:
        return "That drive was taken back with /undo; nothing was saved."

    allow_tolls, had_stop, reply = route_answers[answer]
    store.set_route(conn, int(drive_id), allow_tolls, had_stop)
    return reply


# Takes back the last drive logged: its Reached tap, or the whole drive if it was added with
# /adddrive or hasn't arrived
def undo(conn, now: datetime) -> str:
    last = next(iter(store.latest(conn)), None)
    if last is None or now - last.created > undo_within:
        return f"Nothing logged in the last {undo_within.total_seconds() / 3600:g} h to undo."

    if last.arrival and not last.typed:
        store.set_arrival(conn, last, None)
        return (f"Took back reaching {place_names[last.destination]} at {hhmm(last.arrival)}. The drive from"
                f" {place_names[last.origin]} at {hhmm(last.departure)} is open again.")

    store.delete(conn, last.id)
    return f"Took back {describe(last)}."


def recent(conn, count: int = 5) -> str:
    if not (drives := store.latest(conn, count)):
        return "No drives logged yet."

    lines = []
    for drive in sorted(drives, key=lambda drive: drive.departure):
        route = "stopped on the way" if drive.had_stop else {0: "toll-free", 1: "toll roads"}.get(drive.allow_tolls, "route?")
        lines.append(f"{describe(drive)}{'' if drive.arrival is None else ', ' + route}{', typed' if drive.typed else ''}")
    return "\n".join(lines)


# The reply to one message, with its buttons (None = keep the keyboard). replying_to is the
# text of the message it answers, if any
def respond(conn, text: str, sent: datetime, replying_to: str = "") -> tuple[str, dict | None]:
    if text.strip() in buttons:
        place = buttons[text.strip()]
        return (set_off(conn, place, sent), None) if place else reach(conn, sent)

    words = text.split()
    command = words[0].split("@")[0].lower() if words else ""
    if command in ("/start", "/help"):
        return help_text, None
    if command == "/undo":
        return undo(conn, sent), None
    if command == "/drives":
        return recent(conn), None
    if command == "/adddrive":
        return add(conn, words[1:], sent)

    # An answer to /adddrive's question: the fields without the command
    if add_format in replying_to:
        return add(conn, words, sent)

    return f"Not sure what that was.\n\n{help_text}", None


def handle(conn, token: str, chat_id: int | None, update: dict):
    if query := update.get("callback_query"):
        message = query.get("message") or {}
        if chat_id is None or message.get("chat", {}).get("id") != chat_id:
            return
        reply = answer_route(conn, query.get("data") or "")
        telegram.call(token, "answerCallbackQuery", callback_query_id=query["id"])

        # The answer takes the buttons' place under the question
        telegram.call(token, "editMessageText", chat_id=chat_id, message_id=message["message_id"],
                      text=f"{message.get('text', '')}\n{reply}")
        return

    if not (message := update.get("message")):
        return
    chat = message["chat"]["id"]

    if chat_id is None:
        logger.info(f"Telegram bot: message from chat {chat}; if that's you, set TELEGRAM_CHAT_ID={chat} in .env and restart")
        telegram.call(token, "sendMessage", chat_id=chat, text=f"This chat's id is {chat}. Put TELEGRAM_CHAT_ID={chat}"
                      " in .env and restart the collector. Until then, nothing is logged.")
        return
    if chat != chat_id:
        logger.warning(f"Telegram bot: ignored a message from chat {chat}, not TELEGRAM_CHAT_ID")
        return

    # Saved before replying, so a failed reply loses nothing
    replying_to = (message.get("reply_to_message") or {}).get("text") or ""
    text, markup = respond(conn, message.get("text") or "", datetime.fromtimestamp(message["date"]), replying_to)
    logger.info(f"Telegram bot: {text.splitlines()[0][:120]}")
    telegram.call(token, "sendMessage", chat_id=chat, text=text, reply_markup=markup or keyboard)


# Polls Telegram for messages forever; a failure is logged and retried, never raised
def run(token: str, chat_id: int | None):
    conn = database.connect()
    store.create_tables(conn)
    offset, wait_s = None, retry_s

    # Only the "/" menu is lost if this fails
    try:
        telegram.call(token, "setMyCommands", commands=commands)
    except Exception as err:
        logger.warning(f"Telegram bot: couldn't set the command menu: {err}")

    while True:
        try:
            updates = telegram.call(token, "getUpdates", wait_s=poll_s + 15, offset=offset, timeout=poll_s,
                                    allowed_updates=["message", "callback_query"])
        except Exception as err:
            hint = " (another copy is polling this bot: python -m drives.bot, or a second collector?)" \
                if getattr(err, "code", None) == 409 else ""
            logger.error(f"Telegram bot: {err}{hint}; retrying in {wait_s} s")
            time.sleep(wait_s)
            wait_s = min(wait_s * 2, max_retry_s)
            continue

        wait_s = retry_s
        for update in updates:
            # Asking from the next id confirms this one to Telegram, so it's handled once
            offset = update["update_id"] + 1
            try:
                handle(conn, token, chat_id, update)
            except Exception as err:
                logger.error(f"Telegram bot: a message failed: {err}")


def log_plan(chat_id: int | None):
    if chat_id is None:
        logger.info("Telegram bot: no TELEGRAM_CHAT_ID in .env yet; send the bot a message and it replies with yours")
    else:
        logger.info(f"Telegram bot: logging drives from chat {chat_id}")


# Starts the bot in a thread of its own, if TELEGRAM_BOT_TOKEN is set
def start():
    token, chat_id = settings()
    if token is None:
        logger.info("Telegram bot: off (no TELEGRAM_BOT_TOKEN in .env)")
        return

    log_plan(chat_id)
    threading.Thread(target=run, args=(token, chat_id), name="telegram-bot", daemon=True).start()


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token, chat_id = settings()
    if token is None:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first: make a bot with @BotFather (see .env.example)")

    conn = database.connect()
    if database.get_location(conn, "HOME") is None or database.get_location(conn, "UNI") is None:
        sys.exit("Home/university locations are not set. Run: python setup_locations.py")
    conn.close()

    log_plan(chat_id)
    try:
        run(token, chat_id)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
