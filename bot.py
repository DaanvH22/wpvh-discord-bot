import discord
from discord.ext import commands, tasks
from discord import ui
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
import sqlite3
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ================= DATABASE PATH =================

RAILWAY_VOLUME_MOUNT_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
if RAILWAY_VOLUME_MOUNT_PATH:
    DB_PATH = os.path.join(RAILWAY_VOLUME_MOUNT_PATH, "standbot.db")
else:
    DB_PATH = "standbot.db"

# ================= TIMEZONE =================

APP_TIMEZONE = ZoneInfo("Europe/Amsterdam")


def local_now() -> datetime:
    # Store local Amsterdam time as naive datetime to stay compatible with existing DB rows
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def local_today() -> date:
    return local_now().date()


# ================= DISCORD =================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Prevent startup logic from running multiple times on reconnect
startup_complete = False

# Prevent overlapping challenge processing in the same bot process
challenge_lock = asyncio.Lock()

# ================= DATABASE =================

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    total_standing REAL,
    total_seated REAL,
    prev_timestamp TEXT,
    status TEXT,
    daily_goal_sec INTEGER,
    daily_goal_reached INTEGER,
    last_reset TEXT
)
""")
conn.commit()


def _add_column_if_missing(col_name: str, col_type: str):
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


# Sitting reminder
_add_column_if_missing("reminder_sec", "INTEGER")
_add_column_if_missing("reminder_enabled", "INTEGER")
_add_column_if_missing("last_reminder_session_start", "TEXT")

# Standing reminder
_add_column_if_missing("reminder_stand_sec", "INTEGER")
_add_column_if_missing("reminder_stand_enabled", "INTEGER")
_add_column_if_missing("last_stand_reminder_session_start", "TEXT")

# Streak system
_add_column_if_missing("goal_set_today", "INTEGER")
_add_column_if_missing("current_streak", "INTEGER")
_add_column_if_missing("missed_goal_count", "INTEGER")
_add_column_if_missing("streak_day_processed", "TEXT")
_add_column_if_missing("streak_awarded_today", "INTEGER")

# Daily extra metrics
_add_column_if_missing("total_switches_today", "INTEGER")
_add_column_if_missing("active_today", "INTEGER")

cursor.execute("""
CREATE TABLE IF NOT EXISTS notes (
    user_id INTEGER PRIMARY KEY,
    note TEXT
)
""")
conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_metrics (
    user_id INTEGER,
    metric_date TEXT,
    standing_sec REAL,
    goal_reached INTEGER,
    switches INTEGER,
    active INTEGER,
    PRIMARY KEY (user_id, metric_date)
)
""")
conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start_date TEXT,
    week_end_date TEXT,
    challenge_type TEXT,
    target_value REAL,
    current_progress REAL,
    final_progress REAL,
    channel_id INTEGER,
    message_id INTEGER,
    milestone_posted INTEGER,
    completed INTEGER,
    completion_message_sent INTEGER
)
""")
conn.commit()

# Make sure only ONE challenge row can exist per week per channel
cursor.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_group_challenge_week_channel
ON group_challenges (week_start_date, week_end_date, channel_id)
""")
conn.commit()

# ================= CONFIG =================

GOAL_PRESETS_MIN = {"easy": 30, "medium": 90, "hard": 180}

RECOMMENDED_SIT_REMINDER_MIN = 30
RECOMMENDED_STAND_REMINDER_MIN = 30

MENU_TIMEOUT_SECONDS = 7200

STREAK_FREEZE_MESSAGE = (
    "You missed your goal today, but your streak is protected this time. 🔥\n"
    "Reach your goal next time to keep it going."
)

STREAK_RESET_MESSAGE = (
    "Your streak has reset this time, but that also means a fresh start. 🌱\n"
    "Set a goal and reach it again to start a new streak."
)

CHALLENGE_CHANNEL_ID = 1481603472867721318
CHALLENGE_START_WEEKDAY = 2  # Wednesday (Mon=0)
CHALLENGE_START_TIME = time(9, 30)
CHALLENGE_MILESTONE_STEP = 10
CHALLENGE_GROWTH_FACTOR = 1.10
CHALLENGE_HISTORY_WEEKS = 3

CHALLENGE_ROTATION = [
    "standing_time",
    "daily_goals",
    "posture_switches",
    "active_days"
]

CHALLENGE_CONFIG = {
    "standing_time": {
        "label": "Standing time",
        "default": 8 * 3600,
        "min": 4 * 3600,
        "max": 24 * 3600,
        "active_threshold": 1 * 3600,
        "round_to": 1800
    },
    "daily_goals": {
        "label": "Daily goals",
        "default": 8,
        "min": 4,
        "max": 30,
        "active_threshold": 2,
        "round_to": 1
    },
    "posture_switches": {
        "label": "Posture switches",
        "default": 40,
        "min": 15,
        "max": 250,
        "active_threshold": 10,
        "round_to": 5
    },
    "active_days": {
        "label": "Active days",
        "default": 8,
        "min": 4,
        "max": 30,
        "active_threshold": 2,
        "round_to": 1
    }
}

# ================= HELPERS =================

def ensure_today(user_id: int):
    """
    Only ensures the user exists.
    Does NOT reset anything, so a deploy/push will not reset users.
    """
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if row is None:
        today = str(local_today())
        now = local_now().isoformat()
        cursor.execute("""
            INSERT INTO users (
                user_id,total_standing,total_seated,prev_timestamp,status,
                daily_goal_sec,daily_goal_reached,last_reset,
                reminder_sec,reminder_enabled,last_reminder_session_start,
                reminder_stand_sec,reminder_stand_enabled,last_stand_reminder_session_start,
                goal_set_today,current_streak,missed_goal_count,streak_day_processed,
                streak_awarded_today,total_switches_today,active_today
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            user_id, 0, 0, now, "inactive",
            None, 0, today,
            None, 0, None,
            None, 0, None,
            0, 0, 0, None,
            0, 0, 0
        ))
        conn.commit()


def get_user(user_id: int):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return None

    cursor.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cursor.fetchall()]
    data = dict(zip(cols, row))

    data.setdefault("reminder_sec", None)
    data.setdefault("reminder_enabled", 0)
    data.setdefault("last_reminder_session_start", None)
    data.setdefault("reminder_stand_sec", None)
    data.setdefault("reminder_stand_enabled", 0)
    data.setdefault("last_stand_reminder_session_start", None)

    data.setdefault("goal_set_today", 0)
    data.setdefault("current_streak", 0)
    data.setdefault("missed_goal_count", 0)
    data.setdefault("streak_day_processed", None)
    data.setdefault("streak_awarded_today", 0)

    data.setdefault("total_switches_today", 0)
    data.setdefault("active_today", 0)

    return data


def upsert_user(user_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {fields} WHERE user_id=?", values)
    conn.commit()


def mark_user_active(user_id: int):
    ensure_today(user_id)
    upsert_user(user_id, active_today=1)


def format_time(seconds: float):
    seconds = max(0, int(seconds))
    minutes = int(seconds // 60)
    hours = int(minutes // 60)
    minutes = minutes % 60
    if hours > 0:
        return f"{hours} hour(s) {minutes} min"
    return f"{minutes} min"


def add_elapsed_to_totals(row: dict):
    return add_elapsed_to_totals_until(row, local_now())


def add_elapsed_to_totals_until(row: dict, until_dt: datetime):
    prev = datetime.fromisoformat(row["prev_timestamp"])
    elapsed = max(0, (until_dt - prev).total_seconds())

    standing = float(row["total_standing"] or 0)
    seated = float(row["total_seated"] or 0)

    if row["status"] == "standing":
        standing += elapsed
    elif row["status"] == "seated":
        seated += elapsed

    return standing, seated, elapsed


def set_daily_goal(user_id: int, minutes: int):
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("minutes must be > 0")

    ensure_today(user_id)
    mark_user_active(user_id)

    row = get_user(user_id)
    streak_already_awarded = int(row.get("streak_awarded_today") or 0) == 1

    update_data = {
        "daily_goal_sec": minutes * 60,
        "goal_set_today": 1
    }

    # If streak already awarded today, do NOT set daily_goal_reached back to 0,
    # otherwise the streak can be awarded twice.
    if not streak_already_awarded:
        update_data["daily_goal_reached"] = 0

    upsert_user(user_id, **update_data)


def set_sit_reminder(user_id: int, minutes: int):
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    ensure_today(user_id)
    mark_user_active(user_id)
    upsert_user(
        user_id,
        reminder_sec=minutes * 60,
        reminder_enabled=1,
        last_reminder_session_start=None
    )


def set_stand_reminder(user_id: int, minutes: int):
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    ensure_today(user_id)
    mark_user_active(user_id)
    upsert_user(
        user_id,
        reminder_stand_sec=minutes * 60,
        reminder_stand_enabled=1,
        last_stand_reminder_session_start=None
    )


def disable_sit_reminder(user_id: int):
    ensure_today(user_id)
    mark_user_active(user_id)
    upsert_user(user_id, reminder_enabled=0, last_reminder_session_start=None)


def disable_stand_reminder(user_id: int):
    ensure_today(user_id)
    mark_user_active(user_id)
    upsert_user(user_id, reminder_stand_enabled=0, last_stand_reminder_session_start=None)


def get_streak_text(row: dict) -> str:
    streak = int(row.get("current_streak") or 0)
    return f"🔥 **Streak:** {streak}"


def get_note(user_id: int) -> str | None:
    cursor.execute("SELECT note FROM notes WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_note(user_id: int, note: str):
    cursor.execute("""
    INSERT INTO notes (user_id, note) VALUES (?, ?)
    ON CONFLICT(user_id) DO UPDATE SET note=excluded.note
    """, (user_id, note))
    conn.commit()


def save_daily_metrics(user_id: int, metric_date: str, standing_sec: float, goal_reached: int, switches: int, active: int):
    cursor.execute("""
    INSERT INTO daily_metrics (user_id, metric_date, standing_sec, goal_reached, switches, active)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id, metric_date) DO UPDATE SET
        standing_sec=excluded.standing_sec,
        goal_reached=excluded.goal_reached,
        switches=excluded.switches,
        active=excluded.active
    """, (user_id, metric_date, standing_sec, goal_reached, switches, active))
    conn.commit()


def get_current_challenge_window(now: datetime | None = None):
    """
    Returns (start_dt, end_dt_exclusive) for the current challenge window.
    Challenge starts every Wednesday at 09:30 local time.
    """
    if now is None:
        now = local_now()

    days_since_wed = (now.weekday() - CHALLENGE_START_WEEKDAY) % 7
    this_wed_date = now.date() - timedelta(days=days_since_wed)
    this_wed_start = datetime.combine(this_wed_date, CHALLENGE_START_TIME)

    if now < this_wed_start:
        start_dt = this_wed_start - timedelta(days=7)
    else:
        start_dt = this_wed_start

    end_dt = start_dt + timedelta(days=7)
    return start_dt, end_dt


def get_period_dates_from_window(start_dt: datetime, end_dt: datetime):
    """
    For metric aggregation we use date-based storage.
    End date is inclusive Tuesday.
    """
    return start_dt.date(), (end_dt - timedelta(days=1)).date()


def get_challenge_display_period(start_dt: datetime, end_dt: datetime) -> str:
    end_display = end_dt - timedelta(minutes=1)
    return f"{start_dt.strftime('%a %d %b %H:%M')} – {end_display.strftime('%a %d %b %H:%M')}"


def get_metric_value_text(challenge_type: str, value: float) -> str:
    if challenge_type == "standing_time":
        return format_time(value)
    if challenge_type == "daily_goals":
        return f"{int(round(value))} goals"
    if challenge_type == "posture_switches":
        return f"{int(round(value))} switches"
    if challenge_type == "active_days":
        return f"{int(round(value))} active days"
    return str(value)


def get_milestone_message(percent: int) -> str:
    if percent >= 100:
        return "Challenge completed! 🎉"
    if percent >= 90:
        return "Final push! 🚀"
    if percent >= 70:
        return "Almost there! 👀"
    if percent >= 50:
        return "You're halfway there! 🔥"
    if percent >= 30:
        return "Good progress so far! 🔥"
    if percent >= 10:
        return "Nice start! 💪"
    return "Let's do this! 💪"


def get_next_challenge_type() -> str:
    cursor.execute("""
    SELECT challenge_type
    FROM group_challenges
    ORDER BY week_start_date DESC, id DESC
    LIMIT 1
    """)
    row = cursor.fetchone()

    if not row:
        return CHALLENGE_ROTATION[0]

    last_type = row[0]
    try:
        idx = CHALLENGE_ROTATION.index(last_type)
        return CHALLENGE_ROTATION[(idx + 1) % len(CHALLENGE_ROTATION)]
    except ValueError:
        return CHALLENGE_ROTATION[0]


def round_metric_target(challenge_type: str, raw_value: float) -> float:
    cfg = CHALLENGE_CONFIG[challenge_type]
    step = cfg["round_to"]
    if step <= 1:
        return int(round(raw_value))
    return int(round(raw_value / step) * step)


def clamp_metric_target(challenge_type: str, value: float) -> float:
    cfg = CHALLENGE_CONFIG[challenge_type]
    return max(cfg["min"], min(cfg["max"], value))


def compute_challenge_progress(challenge_type: str, week_start_date: date, week_end_date: date, include_live_current: bool = True) -> float:
    start_str = str(week_start_date)
    end_str = str(week_end_date)

    column_map = {
        "standing_time": "standing_sec",
        "daily_goals": "goal_reached",
        "posture_switches": "switches",
        "active_days": "active"
    }

    col = column_map[challenge_type]
    cursor.execute(f"""
    SELECT COALESCE(SUM({col}), 0)
    FROM daily_metrics
    WHERE metric_date >= ? AND metric_date <= ?
    """, (start_str, end_str))
    db_total = float(cursor.fetchone()[0] or 0)

    if not include_live_current:
        return db_total

    today = local_today()
    if not (week_start_date <= today <= week_end_date):
        return db_total

    live_total = 0.0
    user_ids = [r[0] for r in cursor.execute("SELECT user_id FROM users").fetchall()]
    for user_id in user_ids:
        data = get_user(user_id)
        if not data:
            continue
        if data.get("last_reset") != str(today):
            continue

        if challenge_type == "standing_time":
            standing, _, _ = add_elapsed_to_totals(data)
            live_total += float(standing or 0)
        elif challenge_type == "daily_goals":
            live_total += int(data.get("daily_goal_reached") or 0)
        elif challenge_type == "posture_switches":
            live_total += int(data.get("total_switches_today") or 0)
        elif challenge_type == "active_days":
            live_total += int(data.get("active_today") or 0)

    return db_total + live_total


def get_recent_active_week_values(challenge_type: str, limit: int = CHALLENGE_HISTORY_WEEKS) -> list[float]:
    threshold = CHALLENGE_CONFIG[challenge_type]["active_threshold"]

    cursor.execute("""
    SELECT week_start_date, week_end_date, final_progress
    FROM group_challenges
    WHERE challenge_type=?
    ORDER BY week_start_date DESC, id DESC
    """, (challenge_type,))
    rows = cursor.fetchall()

    values = []
    for week_start_str, week_end_str, final_progress in rows:
        if final_progress is None:
            value = compute_challenge_progress(
                challenge_type,
                date.fromisoformat(week_start_str),
                date.fromisoformat(week_end_str),
                include_live_current=False
            )
        else:
            value = float(final_progress)

        if value >= threshold:
            values.append(value)

        if len(values) >= limit:
            break

    return values


def calculate_new_challenge_target(challenge_type: str) -> float:
    cfg = CHALLENGE_CONFIG[challenge_type]
    values = get_recent_active_week_values(challenge_type)

    if values:
        avg = sum(values) / len(values)
        raw_target = avg * CHALLENGE_GROWTH_FACTOR
    else:
        raw_target = cfg["default"]

    rounded = round_metric_target(challenge_type, raw_target)
    return clamp_metric_target(challenge_type, rounded)


async def get_channel_async(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


def challenge_row_to_dict(row):
    if row is None:
        return None
    cols = [
        "id", "week_start_date", "week_end_date", "challenge_type", "target_value",
        "current_progress", "final_progress", "channel_id", "message_id",
        "milestone_posted", "completed", "completion_message_sent"
    ]
    return dict(zip(cols, row))


def get_current_challenge_row():
    start_dt, end_dt = get_current_challenge_window()
    week_start_date, week_end_date = get_period_dates_from_window(start_dt, end_dt)

    cursor.execute("""
    SELECT id, week_start_date, week_end_date, challenge_type, target_value,
           current_progress, final_progress, channel_id, message_id,
           milestone_posted, completed, completion_message_sent
    FROM group_challenges
    WHERE week_start_date=? AND week_end_date=? AND channel_id=?
    ORDER BY id DESC
    LIMIT 1
    """, (str(week_start_date), str(week_end_date), CHALLENGE_CHANNEL_ID))

    return challenge_row_to_dict(cursor.fetchone())


def get_group_challenge_row_by_id(challenge_id: int):
    cursor.execute("""
    SELECT id, week_start_date, week_end_date, challenge_type, target_value,
           current_progress, final_progress, channel_id, message_id,
           milestone_posted, completed, completion_message_sent
    FROM group_challenges
    WHERE id=?
    """, (challenge_id,))
    return challenge_row_to_dict(cursor.fetchone())


def update_group_challenge_row(challenge_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [challenge_id]
    cursor.execute(f"UPDATE group_challenges SET {fields} WHERE id=?", values)
    conn.commit()


def create_group_challenge_row(week_start_date: str, week_end_date: str, challenge_type: str, target_value: float, channel_id: int):
    """
    Uses INSERT OR IGNORE so duplicate rows for the same week/channel are prevented
    by the UNIQUE index.
    """
    cursor.execute("""
    INSERT OR IGNORE INTO group_challenges (
        week_start_date, week_end_date, challenge_type, target_value,
        current_progress, final_progress, channel_id, message_id,
        milestone_posted, completed, completion_message_sent
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        week_start_date, week_end_date, challenge_type, target_value,
        0, None, channel_id, None,
        0, 0, 0
    ))
    conn.commit()

    cursor.execute("""
    SELECT id
    FROM group_challenges
    WHERE week_start_date=? AND week_end_date=? AND channel_id=?
    ORDER BY id DESC
    LIMIT 1
    """, (week_start_date, week_end_date, channel_id))
    row = cursor.fetchone()
    return row[0] if row else None


def build_challenge_message_content(challenge_type: str, target_value: float, progress_value: float, start_dt: datetime, end_dt: datetime) -> str:
    percent = int((progress_value / target_value) * 100) if target_value > 0 else 0
    percent = min(100, max(0, percent))
    label = CHALLENGE_CONFIG[challenge_type]["label"]
    progress_text = get_metric_value_text(challenge_type, progress_value)
    target_text = get_metric_value_text(challenge_type, target_value)
    period_text = get_challenge_display_period(start_dt, end_dt)
    motivation = get_milestone_message(percent)

    return (
        f"🏆 **Weekly Group Challenge**\n\n"
        f"**Type:** {label}\n"
        f"**Goal:** {target_text} together this week\n"
        f"**Period:** {period_text}\n\n"
        f"**Progress:** {progress_text} / {target_text}\n"
        f"**Completed:** {percent}%\n\n"
        f"{motivation}"
    )


async def ensure_challenge_message(row: dict):
    channel = await get_channel_async(int(row["channel_id"]))
    if channel is None:
        print(f"Could not find challenge channel {row['channel_id']}.")
        return row

    start_dt, end_dt = get_current_challenge_window()
    progress = compute_challenge_progress(
        row["challenge_type"],
        date.fromisoformat(row["week_start_date"]),
        date.fromisoformat(row["week_end_date"]),
        include_live_current=True
    )

    content = build_challenge_message_content(
        row["challenge_type"],
        float(row["target_value"]),
        float(progress),
        start_dt,
        end_dt
    )

    message_id = row.get("message_id")
    if message_id:
        try:
            await channel.fetch_message(int(message_id))
            return row
        except discord.NotFound:
            # Message really does not exist anymore -> safe to recreate
            pass
        except discord.Forbidden as e:
            print(f"Cannot access existing challenge message {message_id}: {e}")
            return row
        except discord.HTTPException as e:
            print(f"Temporary HTTP error while checking challenge message {message_id}: {e}")
            return row
        except Exception as e:
            print(f"Unexpected error while checking challenge message {message_id}: {e}")
            return row

    try:
        message = await channel.send(content)
        update_group_challenge_row(
            row["id"],
            message_id=message.id,
            current_progress=progress
        )
        row["message_id"] = message.id
        row["current_progress"] = progress
    except Exception as e:
        print(f"Could not send challenge message: {e}")

    return row


async def edit_challenge_message(row: dict, progress_value: float):
    channel = await get_channel_async(int(row["channel_id"]))
    if channel is None:
        print(f"Could not find challenge channel {row['channel_id']}.")
        return

    if not row.get("message_id"):
        return

    try:
        message = await channel.fetch_message(int(row["message_id"]))
    except discord.NotFound:
        row = await ensure_challenge_message(row)
        if not row.get("message_id"):
            return
        try:
            message = await channel.fetch_message(int(row["message_id"]))
        except Exception:
            return
    except discord.Forbidden as e:
        print(f"Cannot fetch challenge message for editing: {e}")
        return
    except discord.HTTPException as e:
        print(f"HTTP error while fetching challenge message for editing: {e}")
        return
    except Exception as e:
        print(f"Unexpected error while fetching challenge message for editing: {e}")
        return

    start_dt, end_dt = get_current_challenge_window()
    content = build_challenge_message_content(
        row["challenge_type"],
        float(row["target_value"]),
        float(progress_value),
        start_dt,
        end_dt
    )

    try:
        await message.edit(content=content)
    except Exception as e:
        print(f"Could not edit challenge message: {e}")


async def post_challenge_completion_message(row: dict, progress_value: float):
    channel = await get_channel_async(int(row["channel_id"]))
    if channel is None:
        print(f"Could not find challenge channel {row['channel_id']}.")
        return

    label = CHALLENGE_CONFIG[row["challenge_type"]]["label"]
    progress_text = get_metric_value_text(row["challenge_type"], progress_value)

    try:
        await channel.send(
            f"🎉 **Weekly Group Challenge completed!**\n\n"
            f"You completed this week's **{label.lower()}** challenge.\n"
            f"Final progress: **{progress_text}**\n\n"
            f"Amazing job everyone! 👏"
        )
    except Exception as e:
        print(f"Could not send challenge completion message: {e}")


async def finalize_old_challenges():
    current = get_current_challenge_row()
    current_start = None
    current_channel = None
    if current:
        current_start = current["week_start_date"]
        current_channel = current["channel_id"]

    cursor.execute("""
    SELECT id, week_start_date, week_end_date, challenge_type, target_value,
           current_progress, final_progress, channel_id, message_id,
           milestone_posted, completed, completion_message_sent
    FROM group_challenges
    ORDER BY week_start_date DESC, id DESC
    """)
    rows = cursor.fetchall()

    for raw in rows:
        row = challenge_row_to_dict(raw)
        if (
            current_start is not None
            and current_channel is not None
            and row["week_start_date"] == current_start
            and row["channel_id"] == current_channel
        ):
            continue
        if row["final_progress"] is not None:
            continue

        final_progress = compute_challenge_progress(
            row["challenge_type"],
            date.fromisoformat(row["week_start_date"]),
            date.fromisoformat(row["week_end_date"]),
            include_live_current=False
        )
        completed = 1 if final_progress >= float(row["target_value"]) else 0
        update_group_challenge_row(
            row["id"],
            final_progress=final_progress,
            current_progress=final_progress,
            completed=completed
        )


async def ensure_current_group_challenge():
    await finalize_old_challenges()

    start_dt, end_dt = get_current_challenge_window()
    week_start_date, week_end_date = get_period_dates_from_window(start_dt, end_dt)

    row = get_current_challenge_row()
    if not row:
        challenge_type = get_next_challenge_type()
        target_value = calculate_new_challenge_target(challenge_type)

        challenge_id = create_group_challenge_row(
            str(week_start_date),
            str(week_end_date),
            challenge_type,
            target_value,
            CHALLENGE_CHANNEL_ID
        )

        if challenge_id is None:
            return None

        row = get_group_challenge_row_by_id(challenge_id)

    if not row:
        return None

    return await ensure_challenge_message(row)


async def process_group_challenge():
    async with challenge_lock:
        row = await ensure_current_group_challenge()
        if not row:
            return

        progress = compute_challenge_progress(
            row["challenge_type"],
            date.fromisoformat(row["week_start_date"]),
            date.fromisoformat(row["week_end_date"]),
            include_live_current=True
        )

        target = float(row["target_value"] or 0)
        percent = int((progress / target) * 100) if target > 0 else 0
        percent = min(100, max(0, percent))
        milestone = (percent // CHALLENGE_MILESTONE_STEP) * CHALLENGE_MILESTONE_STEP

        updates = {"current_progress": progress}

        if int(row.get("completed") or 0) == 0 and progress >= target:
            updates["completed"] = 1
            updates["final_progress"] = progress
            updates["milestone_posted"] = 100
            update_group_challenge_row(row["id"], **updates)
            row.update(updates)

            await edit_challenge_message(row, progress)

            if int(row.get("completion_message_sent") or 0) == 0:
                await post_challenge_completion_message(row, progress)
                update_group_challenge_row(row["id"], completion_message_sent=1)
            return

        if milestone > int(row.get("milestone_posted") or 0):
            updates["milestone_posted"] = milestone
            update_group_challenge_row(row["id"], **updates)
            row.update(updates)
            await edit_challenge_message(row, progress)
        else:
            update_group_challenge_row(row["id"], **updates)

# ================= DAILY ROLLOVER =================

async def process_daily_rollover(send_messages: bool = True):
    """
    Handles end-of-day processing for all users:
    - saves previous day metrics to daily_metrics
    - processes streak logic for the previous day
    - resets everyone to inactive for the new day
    """
    today = str(local_today())
    now_dt = local_now()
    now_iso = now_dt.isoformat()
    today_start = datetime.combine(local_today(), time.min)

    user_ids = [r[0] for r in cursor.execute("SELECT user_id FROM users").fetchall()]

    for user_id in user_ids:
        data = get_user(user_id)
        if not data:
            continue

        last_reset = data.get("last_reset")
        if last_reset == today:
            continue

        previous_day = last_reset

        # Calculate previous day's final standing total
        standing_prev = float(data.get("total_standing") or 0)
        if data.get("status") == "standing":
            prev = datetime.fromisoformat(data["prev_timestamp"])
            standing_prev += max(0, (today_start - prev).total_seconds())

        # Save previous day's metrics
        if previous_day:
            goal_reached_prev = int(data.get("daily_goal_reached") or 0)
            switches_prev = int(data.get("total_switches_today") or 0)
            active_prev = int(data.get("active_today") or 0)

            save_daily_metrics(
                user_id=user_id,
                metric_date=previous_day,
                standing_sec=standing_prev,
                goal_reached=goal_reached_prev,
                switches=switches_prev,
                active=active_prev
            )

        # Process streak logic for previous day
        goal_set_today = int(data.get("goal_set_today") or 0)
        daily_goal_reached = int(data.get("daily_goal_reached") or 0)
        missed_goal_count = int(data.get("missed_goal_count") or 0)
        streak_day_processed = data.get("streak_day_processed")

        if previous_day and streak_day_processed != previous_day:
            if goal_set_today == 1 and daily_goal_reached == 0:
                if missed_goal_count == 0:
                    upsert_user(
                        user_id,
                        missed_goal_count=1,
                        streak_day_processed=previous_day
                    )
                    if send_messages:
                        try:
                            user = await bot.fetch_user(user_id)
                            await user.send(STREAK_FREEZE_MESSAGE)
                        except discord.Forbidden:
                            print(f"Could not DM user {user_id} (DMs disabled).")
                else:
                    upsert_user(
                        user_id,
                        current_streak=0,
                        missed_goal_count=0,
                        streak_day_processed=previous_day
                    )
                    if send_messages:
                        try:
                            user = await bot.fetch_user(user_id)
                            await user.send(STREAK_RESET_MESSAGE)
                        except discord.Forbidden:
                            print(f"Could not DM user {user_id} (DMs disabled).")

        # Start new day clean: always inactive
        upsert_user(
            user_id,
            total_standing=0,
            total_seated=0,
            daily_goal_sec=None,
            daily_goal_reached=0,
            goal_set_today=0,
            streak_day_processed=None,
            streak_awarded_today=0,
            total_switches_today=0,
            active_today=0,
            status="inactive",
            prev_timestamp=now_iso,
            last_reminder_session_start=None,
            last_stand_reminder_session_start=None,
            last_reset=today
        )

# ================= ACTIONS =================

async def action_stand(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    mark_user_active(user_id)
    row = get_user(user_id)
    now = local_now()

    if row["status"] == "standing":
        return f"{user.mention} is already **standing**."

    if row["status"] == "seated":
        prev = datetime.fromisoformat(row["prev_timestamp"])
        elapsed = (now - prev).total_seconds()
        upsert_user(
            user_id,
            total_seated=float(row["total_seated"] or 0) + elapsed,
            prev_timestamp=now.isoformat(),
            status="standing",
            total_switches_today=int(row.get("total_switches_today") or 0) + 1,
            last_reminder_session_start=None,
            last_stand_reminder_session_start=None
        )
        return f"{user.mention} is now **standing**."

    upsert_user(
        user_id,
        prev_timestamp=now.isoformat(),
        status="standing",
        last_stand_reminder_session_start=None
    )
    return f"{user.mention} is now **standing**."


async def action_sit(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    mark_user_active(user_id)
    row = get_user(user_id)
    now = local_now()

    if row["status"] == "seated":
        return f"{user.mention} is already **sitting**."

    if row["status"] == "standing":
        prev = datetime.fromisoformat(row["prev_timestamp"])
        elapsed = (now - prev).total_seconds()
        upsert_user(
            user_id,
            total_standing=float(row["total_standing"] or 0) + elapsed,
            prev_timestamp=now.isoformat(),
            status="seated",
            total_switches_today=int(row.get("total_switches_today") or 0) + 1,
            last_reminder_session_start=None,
            last_stand_reminder_session_start=None
        )
        return f"{user.mention} is now **sitting**."

    upsert_user(
        user_id,
        prev_timestamp=now.isoformat(),
        status="seated",
        last_reminder_session_start=None
    )
    return f"{user.mention} is now **sitting**."


async def action_end(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    mark_user_active(user_id)
    row = get_user(user_id)
    now = local_now()

    if row["status"] in ("standing", "seated"):
        prev = datetime.fromisoformat(row["prev_timestamp"])
        elapsed = (now - prev).total_seconds()

        if row["status"] == "standing":
            upsert_user(
                user_id,
                total_standing=float(row["total_standing"] or 0) + elapsed,
                prev_timestamp=now.isoformat(),
                status="inactive",
                last_reminder_session_start=None,
                last_stand_reminder_session_start=None
            )
        else:
            upsert_user(
                user_id,
                total_seated=float(row["total_seated"] or 0) + elapsed,
                prev_timestamp=now.isoformat(),
                status="inactive",
                last_reminder_session_start=None,
                last_stand_reminder_session_start=None
            )
    else:
        upsert_user(
            user_id,
            prev_timestamp=now.isoformat(),
            status="inactive",
            last_reminder_session_start=None,
            last_stand_reminder_session_start=None
        )

    return f"{user.mention} is now **inactive**. (Stopped tracking for now.)"


async def action_status(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    mark_user_active(user_id)
    row = get_user(user_id)
    standing, seated, elapsed = add_elapsed_to_totals(row)

    return (
        f"{user.mention} current status: **{row['status']}**\n"
        f"Time in current status: **{format_time(elapsed)}**\n"
        f"Today standing: **{format_time(standing)}** | seated: **{format_time(seated)}**"
    )


async def action_daily(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    mark_user_active(user_id)
    row = get_user(user_id)

    streak_line = get_streak_text(row)

    if row["daily_goal_sec"] is None:
        return (
            f"{user.mention} no daily goal set yet. Use **Set goal**.\n"
            f"{streak_line}"
        )

    standing, _, _ = add_elapsed_to_totals(row)
    goal = int(row["daily_goal_sec"])

    percentage = int((standing / goal) * 100) if goal > 0 else 0
    percentage = min(100, max(0, percentage))

    reached = "✅ reached" if row["daily_goal_reached"] else "⏳ not reached yet"
    goal_set_today_text = "Yes" if int(row.get("goal_set_today") or 0) == 1 else "No"

    return (
        f"{user.mention} **Daily goal** ({reached})\n"
        f"Goal: **{format_time(goal)}**\n"
        f"Progress: **{format_time(standing)}**\n"
        f"Completed: **{percentage}%**\n"
        f"Goal set today: **{goal_set_today_text}**\n"
        f"{streak_line}"
    )


async def action_overview(user: discord.User | discord.Member):
    status_text = await action_status(user)
    daily_text = await action_daily(user)
    return f"{status_text}\n\n{daily_text}"


async def action_reminder_info(user: discord.User | discord.Member):
    ensure_today(user.id)
    mark_user_active(user.id)
    row = get_user(user.id)

    sit_enabled = bool(row.get("reminder_enabled", 0)) and bool(row.get("reminder_sec"))
    stand_enabled = bool(row.get("reminder_stand_enabled", 0)) and bool(row.get("reminder_stand_sec"))

    sit_line = "OFF"
    if sit_enabled:
        sit_line = f"ON (after {format_time(row['reminder_sec'])} sitting)"

    stand_line = "OFF"
    if stand_enabled:
        stand_line = f"ON (after {format_time(row['reminder_stand_sec'])} standing)"

    return (
        f"{user.mention} **Reminders**\n"
        f"• Sitting → Stand: **{sit_line}**\n"
        f"• Standing → Sit: **{stand_line}**\n\n"
        f"Use the buttons to set or change them again."
    )

# ================= MODALS =================

class CustomGoalModal(ui.Modal, title="Set a custom daily goal"):
    minutes = ui.TextInput(
        label="How many minutes of standing today?",
        placeholder="e.g., 45",
        required=True,
        max_length=4
    )

    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This modal isn't for you 🙂. Type `!menu` in your own DM.",
                ephemeral=True
            )
            return

        raw = str(self.minutes.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Please enter a whole number (minutes), e.g. 45.",
                ephemeral=False
            )
            return

        mins = int(raw)
        if mins <= 0:
            await interaction.response.send_message("Minutes must be > 0.", ephemeral=False)
            return

        set_daily_goal(interaction.user.id, mins)
        row = get_user(interaction.user.id)
        await interaction.response.send_message(
            f"{interaction.user.mention} daily goal set to **{mins} minutes** (custom).\n"
            f"{get_streak_text(row)}",
            ephemeral=False
        )


class CustomSitReminderModal(ui.Modal, title="Custom reminder (Sitting → Stand)"):
    minutes = ui.TextInput(
        label="Remind me after how many minutes of sitting?",
        placeholder="e.g., 45",
        required=True,
        max_length=4
    )

    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This modal isn't for you 🙂. Type `!menu` in your own DM.",
                ephemeral=True
            )
            return

        raw = str(self.minutes.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Please enter a whole number (minutes), e.g. 45.",
                ephemeral=False
            )
            return

        mins = int(raw)
        if mins <= 0:
            await interaction.response.send_message("Minutes must be > 0.", ephemeral=False)
            return

        set_sit_reminder(interaction.user.id, mins)
        text = await action_reminder_info(interaction.user)
        await interaction.response.send_message(f"✅ Sitting reminder set!\n\n{text}", ephemeral=False)


class CustomStandReminderModal(ui.Modal, title="Custom reminder (Standing → Sit)"):
    minutes = ui.TextInput(
        label="Remind me after how many minutes of standing?",
        placeholder="e.g., 30",
        required=True,
        max_length=4
    )

    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This modal isn't for you 🙂. Type `!menu` in your own DM.",
                ephemeral=True
            )
            return

        raw = str(self.minutes.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "Please enter a whole number (minutes), e.g. 30.",
                ephemeral=False
            )
            return

        mins = int(raw)
        if mins <= 0:
            await interaction.response.send_message("Minutes must be > 0.", ephemeral=False)
            return

        set_stand_reminder(interaction.user.id, mins)
        text = await action_reminder_info(interaction.user)
        await interaction.response.send_message(f"✅ Standing reminder set!\n\n{text}", ephemeral=False)


class NoteEditModal(ui.Modal, title="Edit your table height note"):
    note = ui.TextInput(
        label="Your note (e.g., 'Desk at 102cm')",
        placeholder="Write something you can quickly use later",
        required=True,
        max_length=120
    )

    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This modal isn't for you 🙂. Type `!menu` in your own DM.",
                ephemeral=True
            )
            return

        text = str(self.note.value).strip()
        mark_user_active(self.owner_id)
        set_note(self.owner_id, text)
        await interaction.response.send_message("✅ Saved!", ephemeral=True)

# ================= BASE VIEW WITH TIMEOUT MESSAGE =================

class BaseOwnedView(ui.View):
    def __init__(self, owner_id: int, timeout: int = MENU_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These buttons aren't for you 🙂. Please type !menu in your DM with me.",
                ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(
                    content="This menu has timed out. Please type **!menu** to open a new one.",
                    view=None
                )
            except Exception:
                pass

# ================= VIEWS =================

class GoalView(BaseOwnedView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id=owner_id)

    @ui.button(label="Easy (30 min)", style=discord.ButtonStyle.success)
    async def easy(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["easy"])
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=next_view)

    @ui.button(label="Medium (1 hour 30 min)", style=discord.ButtonStyle.primary)
    async def medium(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["medium"])
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=next_view)

    @ui.button(label="Hard (3 hours)", style=discord.ButtonStyle.danger)
    async def hard(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["hard"])
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=next_view)

    @ui.button(label="Custom…", style=discord.ButtonStyle.secondary)
    async def custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomGoalModal(owner_id=self.owner_id))

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=next_view)


class ReminderView(BaseOwnedView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id=owner_id)

    @ui.button(label="Set sitting reminder to 30 min", style=discord.ButtonStyle.primary)
    async def sit_recommended(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_sit_reminder(interaction.user.id, RECOMMENDED_SIT_REMINDER_MIN)
        self.message = interaction.message
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Custom sitting reminder…", style=discord.ButtonStyle.secondary)
    async def sit_custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomSitReminderModal(owner_id=self.owner_id))

    @ui.button(label="Turn OFF sitting reminder", style=discord.ButtonStyle.danger, row=1)
    async def sit_off(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        disable_sit_reminder(interaction.user.id)
        self.message = interaction.message
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Set standing reminder to 30 min", style=discord.ButtonStyle.primary, row=2)
    async def stand_recommended(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_stand_reminder(interaction.user.id, RECOMMENDED_STAND_REMINDER_MIN)
        self.message = interaction.message
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Custom standing reminder…", style=discord.ButtonStyle.secondary, row=2)
    async def stand_custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomStandReminderModal(owner_id=self.owner_id))

    @ui.button(label="Turn OFF standing reminder", style=discord.ButtonStyle.danger, row=3)
    async def stand_off(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        disable_stand_reminder(interaction.user.id)
        self.message = interaction.message
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=next_view)


class NoteView(BaseOwnedView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id=owner_id)

    @ui.button(label="Edit note", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(NoteEditModal(owner_id=self.owner_id))

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        next_view = MenuView(self.owner_id)
        next_view.message = interaction.message
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=next_view)


class MenuView(BaseOwnedView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id=owner_id)

    @ui.button(label="I'm standing", style=discord.ButtonStyle.success)
    async def standing(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        self.message = interaction.message
        text = await action_stand(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="I'm sitting", style=discord.ButtonStyle.success)
    async def sitting(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        self.message = interaction.message
        text = await action_sit(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="Overview", style=discord.ButtonStyle.primary)
    async def overview(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        self.message = interaction.message
        text = await action_overview(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="Set goal", style=discord.ButtonStyle.primary)
    async def set_goal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        next_view = GoalView(self.owner_id)
        next_view.message = interaction.message
        await interaction.message.edit(content="Choose a daily goal:", view=next_view)

    @ui.button(label="Reminders", style=discord.ButtonStyle.primary)
    async def reminders(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        next_view = ReminderView(self.owner_id)
        next_view.message = interaction.message
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=text, view=next_view)

    @ui.button(label="Table notes", style=discord.ButtonStyle.secondary)
    async def table_note(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        mark_user_active(interaction.user.id)
        next_view = NoteView(self.owner_id)
        next_view.message = interaction.message
        note = get_note(interaction.user.id)
        if note:
            content = f"{interaction.user.mention} your saved note:\n**{note}**"
        else:
            content = f"{interaction.user.mention} you don't have a note yet. Click **Edit note** to add one."
        await interaction.message.edit(content=content, view=next_view)

    @ui.button(label="Pause/End", style=discord.ButtonStyle.danger)
    async def end(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        self.message = interaction.message
        text = await action_end(interaction.user)
        await interaction.message.edit(content=text, view=self)

# ================= COMMANDS (DM ONLY) =================

@bot.command()
async def menu(ctx):
    if ctx.guild is not None:
        await ctx.send("Please use DM to talk to me 🙂")
        return
    ensure_today(ctx.author.id)
    mark_user_active(ctx.author.id)
    view = MenuView(ctx.author.id)
    message = await ctx.send(f"Hi {ctx.author.mention}", view=view)
    view.message = message


@bot.command()
async def stand(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_stand(ctx.author))


@bot.command()
async def sit(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_sit(ctx.author))


@bot.command()
async def status(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_status(ctx.author))


@bot.command()
async def daily(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_daily(ctx.author))


@bot.command()
async def overview(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_overview(ctx.author))


@bot.command()
async def setdaily(ctx, minutes: int):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    try:
        set_daily_goal(ctx.author.id, minutes)
    except ValueError:
        return await ctx.send(f"{ctx.author.mention} minutes must be > 0.")
    row = get_user(ctx.author.id)
    await ctx.send(
        f"{ctx.author.mention} goal set to {minutes} minutes\n"
        f"{get_streak_text(row)}"
    )


@bot.command()
async def end(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_end(ctx.author))

# ================= CHECKERS =================

@tasks.loop(minutes=1)
async def goal_checker():
    user_ids = [r[0] for r in cursor.execute("SELECT user_id FROM users").fetchall()]
    today = str(local_today())

    for user_id in user_ids:
        data = get_user(user_id)
        if not data:
            continue

        if not data.get("daily_goal_sec"):
            continue

        if int(data.get("goal_set_today") or 0) != 1:
            continue

        if int(data.get("daily_goal_reached") or 0) == 1:
            continue

        standing, _, _ = add_elapsed_to_totals(data)

        if standing >= int(data["daily_goal_sec"]):
            updates = {
                "daily_goal_reached": 1
            }

            # Only award streak once per day
            if int(data.get("streak_awarded_today") or 0) == 0:
                new_streak = int(data.get("current_streak") or 0) + 1
                updates["current_streak"] = new_streak
                updates["missed_goal_count"] = 0
                updates["streak_day_processed"] = today
                updates["streak_awarded_today"] = 1
                streak_message = f"🔥 Your streak is now **{new_streak}**."
            else:
                streak_message = "🔥 Your streak has already been counted for today."

            upsert_user(user_id, **updates)

            try:
                user = await bot.fetch_user(user_id)
                await user.send(
                    "🎉 You reached your daily goal!\n"
                    f"{streak_message}"
                )
            except discord.Forbidden:
                print(f"Could not DM user {user_id} (DMs disabled).")


@tasks.loop(minutes=1)
async def reminder_checker():
    user_ids = [r[0] for r in cursor.execute("SELECT user_id FROM users").fetchall()]

    for user_id in user_ids:
        data = get_user(user_id)
        if not data:
            continue

        now = local_now()

        if data.get("reminder_enabled") and data.get("reminder_sec") and data.get("status") == "seated":
            session_start = data.get("prev_timestamp")
            if session_start and data.get("last_reminder_session_start") != session_start:
                prev = datetime.fromisoformat(session_start)
                elapsed = (now - prev).total_seconds()

                if elapsed >= int(data["reminder_sec"]):
                    upsert_user(user_id, last_reminder_session_start=session_start)
                    try:
                        user = await bot.fetch_user(user_id)
                        await user.send(
                            f"👋 Reminder: you've been **sitting for {format_time(elapsed)}**. "
                            f"Maybe it's a good moment to stand up? 🙂"
                        )
                    except discord.Forbidden:
                        print(f"Could not DM user {user_id} (DMs disabled).")

        if data.get("reminder_stand_enabled") and data.get("reminder_stand_sec") and data.get("status") == "standing":
            session_start = data.get("prev_timestamp")
            if session_start and data.get("last_stand_reminder_session_start") != session_start:
                prev = datetime.fromisoformat(session_start)
                elapsed = (now - prev).total_seconds()

                if elapsed >= int(data["reminder_stand_sec"]):
                    upsert_user(user_id, last_stand_reminder_session_start=session_start)
                    try:
                        user = await bot.fetch_user(user_id)
                        await user.send(
                            f"👋 Reminder: you've been **standing for {format_time(elapsed)}**. "
                            f"Maybe it's a good moment to sit down? 🙂"
                        )
                    except discord.Forbidden:
                        print(f"Could not DM user {user_id} (DMs disabled).")


@tasks.loop(minutes=1)
async def daily_rollover_checker():
    await process_daily_rollover(send_messages=True)


@tasks.loop(minutes=1)
async def group_challenge_checker():
    await process_group_challenge()


@goal_checker.before_loop
async def before_goal_checker():
    await bot.wait_until_ready()


@reminder_checker.before_loop
async def before_reminder_checker():
    await bot.wait_until_ready()


@daily_rollover_checker.before_loop
async def before_daily_rollover_checker():
    await bot.wait_until_ready()


@group_challenge_checker.before_loop
async def before_group_challenge_checker():
    await bot.wait_until_ready()

# ================= EVENTS =================

@bot.event
async def on_ready():
    global startup_complete

    print("Bot ready")
    print(f"Using database at: {DB_PATH}")

    if startup_complete:
        print("Reconnect detected - startup logic skipped.")
        return

    startup_complete = True

    await process_daily_rollover(send_messages=True)
    await process_group_challenge()

    if not goal_checker.is_running():
        goal_checker.start()

    if not reminder_checker.is_running():
        reminder_checker.start()

    if not daily_rollover_checker.is_running():
        daily_rollover_checker.start()

    if not group_challenge_checker.is_running():
        group_challenge_checker.start()

# ================= RUN AND RESTART =================

while True:
    try:
        bot.run(TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", e)