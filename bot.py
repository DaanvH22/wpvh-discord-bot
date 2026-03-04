import discord
from discord.ext import commands, tasks
from discord import ui
from datetime import datetime, date
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DATABASE =================

conn = sqlite3.connect("standbot.db")
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

# --- Lightweight "migration" for reminder fields (safe if already exists) ---
def _add_column_if_missing(col_name: str, col_type: str):
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

# Sitting reminder
_add_column_if_missing("reminder_sec", "INTEGER")                 # after how many seconds sitting
_add_column_if_missing("reminder_enabled", "INTEGER")             # 0/1
_add_column_if_missing("last_reminder_session_start", "TEXT")     # seated session start already reminded

# Standing reminder (inverse)
_add_column_if_missing("reminder_stand_sec", "INTEGER")           # after how many seconds standing
_add_column_if_missing("reminder_stand_enabled", "INTEGER")       # 0/1
_add_column_if_missing("last_stand_reminder_session_start", "TEXT")  # standing session start already reminded

# ================= CONFIG =================

GOAL_PRESETS_MIN = {"easy": 30, "medium": 60, "hard": 120}

# Recommended reminder times (you can tweak these)
RECOMMENDED_SIT_REMINDER_MIN = 30     # remind to stand after sitting
RECOMMENDED_STAND_REMINDER_MIN = 30   # remind to sit after standing

# ================= HELPERS =================

def ensure_today(user_id: int):
    today = str(date.today())
    cursor.execute("SELECT last_reset FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if row is None:
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO users (
                user_id,total_standing,total_seated,prev_timestamp,status,
                daily_goal_sec,daily_goal_reached,last_reset,
                reminder_sec,reminder_enabled,last_reminder_session_start,
                reminder_stand_sec,reminder_stand_enabled,last_stand_reminder_session_start
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            user_id, 0, 0, now, "inactive",
            None, 0, today,
            None, 0, None,
            None, 0, None
        ))
        conn.commit()
    elif row[0] != today:
        cursor.execute("""
            UPDATE users SET
                total_standing=0,
                total_seated=0,
                daily_goal_reached=0,
                last_reset=?
            WHERE user_id=?
        """, (today, user_id))
        conn.commit()

def get_user(user_id: int):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return None

    cursor.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cursor.fetchall()]
    data = dict(zip(cols, row))

    # Ensure keys exist (in case migration order differs)
    data.setdefault("reminder_sec", None)
    data.setdefault("reminder_enabled", 0)
    data.setdefault("last_reminder_session_start", None)
    data.setdefault("reminder_stand_sec", None)
    data.setdefault("reminder_stand_enabled", 0)
    data.setdefault("last_stand_reminder_session_start", None)
    return data

def upsert_user(user_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {fields} WHERE user_id=?", values)
    conn.commit()

def format_time(seconds: float):
    seconds = max(0, int(seconds))
    minutes = int(seconds // 60)
    hours = int(minutes // 60)
    minutes = minutes % 60
    if hours > 0:
        return f"{hours} uur {minutes} min"
    return f"{minutes} min"

def add_elapsed_to_totals(row: dict):
    now = datetime.now()
    prev = datetime.fromisoformat(row["prev_timestamp"])
    elapsed = (now - prev).total_seconds()

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
    upsert_user(user_id, daily_goal_sec=minutes * 60, daily_goal_reached=0)

def set_sit_reminder(user_id: int, minutes: int):
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    ensure_today(user_id)
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
    upsert_user(
        user_id,
        reminder_stand_sec=minutes * 60,
        reminder_stand_enabled=1,
        last_stand_reminder_session_start=None
    )

def disable_sit_reminder(user_id: int):
    ensure_today(user_id)
    upsert_user(user_id, reminder_enabled=0, last_reminder_session_start=None)

def disable_stand_reminder(user_id: int):
    ensure_today(user_id)
    upsert_user(user_id, reminder_stand_enabled=0, last_stand_reminder_session_start=None)

# ================= ACTIONS =================

async def action_stand(user: discord.User | discord.Member):
    user_id = user.id
    ensure_today(user_id)
    row = get_user(user_id)
    now = datetime.now()

    if row["status"] == "seated":
        prev = datetime.fromisoformat(row["prev_timestamp"])
        elapsed = (now - prev).total_seconds()
        upsert_user(
            user_id,
            total_seated=float(row["total_seated"] or 0) + elapsed,
            prev_timestamp=now.isoformat(),
            status="standing",
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
    row = get_user(user_id)
    now = datetime.now()

    if row["status"] == "standing":
        prev = datetime.fromisoformat(row["prev_timestamp"])
        elapsed = (now - prev).total_seconds()
        upsert_user(
            user_id,
            total_standing=float(row["total_standing"] or 0) + elapsed,
            prev_timestamp=now.isoformat(),
            status="seated",
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
    row = get_user(user_id)
    now = datetime.now()

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
    row = get_user(user_id)

    if row["daily_goal_sec"] is None:
        return f"{user.mention} no daily goal set yet. Use **Set goal**."

    standing, _, _ = add_elapsed_to_totals(row)
    goal = int(row["daily_goal_sec"])

    percentage = int((standing / goal) * 100) if goal > 0 else 0
    percentage = min(100, max(0, percentage))

    reached = "✅ reached" if row["daily_goal_reached"] else "⏳ not reached yet"

    return (
        f"{user.mention} **Daily goal** ({reached})\n"
        f"Goal: **{format_time(goal)}**\n"
        f"Progress: **{format_time(standing)}**\n"
        f"Completed: **{percentage}%**"
    )

async def action_overview(user: discord.User | discord.Member):
    status_text = await action_status(user)
    daily_text = await action_daily(user)
    return f"{status_text}\n\n{daily_text}"

async def action_reminder_info(user: discord.User | discord.Member):
    ensure_today(user.id)
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
        f"Use the buttons below to set or change them."
    )

# ================= MODALS =================

class CustomGoalModal(ui.Modal, title="Set a custom daily goal"):
    minutes = ui.TextInput(
        label="How many minutes standing today?",
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
        await interaction.response.send_message(
            f"{interaction.user.mention} daily goal set to **{mins} minutes** (custom).",
            ephemeral=False
        )

class CustomSitReminderModal(ui.Modal, title="Custom reminder (Sitting → Stand)"):
    minutes = ui.TextInput(
        label="Remind me after how many minutes sitting?",
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
        label="Remind me after how many minutes standing?",
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

# ================= NOTES (Table height note) =================

cursor.execute("""
CREATE TABLE IF NOT EXISTS notes (
    user_id INTEGER PRIMARY KEY,
    note TEXT
)
""")
conn.commit()

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
        set_note(self.owner_id, text)
        await interaction.response.send_message("✅ Saved!", ephemeral=True)

# ================= VIEWS =================

class GoalView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Deze knoppen zijn niet voor jou 🙂. Typ `!menu` in je eigen DM met mij.",
                ephemeral=True
            )
            return False
        return True

    @ui.button(label="Easy (30 min)", style=discord.ButtonStyle.success)
    async def easy(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["easy"])
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=MenuView(self.owner_id))

    @ui.button(label="Medium (60 min)", style=discord.ButtonStyle.primary)
    async def medium(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["medium"])
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=MenuView(self.owner_id))

    @ui.button(label="Hard (120 min)", style=discord.ButtonStyle.danger)
    async def hard(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_daily_goal(interaction.user.id, GOAL_PRESETS_MIN["hard"])
        text = await action_daily(interaction.user)
        await interaction.message.edit(content=f"✅ Goal set!\n\n{text}", view=MenuView(self.owner_id))

    @ui.button(label="Custom…", style=discord.ButtonStyle.secondary)
    async def custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomGoalModal(owner_id=self.owner_id))

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=MenuView(self.owner_id))

class ReminderView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These buttons aren't for you 🙂. Please type !menu in your DM with me.",
                ephemeral=True
            )
            return False
        return True

    # ---- Sitting reminder ----
    @ui.button(label="Set sitting reminder to 30 min", style=discord.ButtonStyle.primary)
    async def sit_recommended(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_sit_reminder(interaction.user.id, RECOMMENDED_SIT_REMINDER_MIN)
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Custom sitting reminder…", style=discord.ButtonStyle.secondary)
    async def sit_custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomSitReminderModal(owner_id=self.owner_id))

    @ui.button(label="Turn OFF sitting reminder", style=discord.ButtonStyle.danger, row=1)
    async def sit_off(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        disable_sit_reminder(interaction.user.id)
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    # ---- Standing reminder ----
    @ui.button(label="Set standing reminder to 30 min", style=discord.ButtonStyle.primary, row=2)
    async def stand_recommended(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        set_stand_reminder(interaction.user.id, RECOMMENDED_STAND_REMINDER_MIN)
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Custom standing reminder…", style=discord.ButtonStyle.secondary, row=2)
    async def stand_custom(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CustomStandReminderModal(owner_id=self.owner_id))

    @ui.button(label="Turn OFF standing reminder", style=discord.ButtonStyle.danger, row=3)
    async def stand_off(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        disable_stand_reminder(interaction.user.id)
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=f"✅ Updated!\n\n{text}", view=self)

    @ui.button(label="Back", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=MenuView(self.owner_id))

class NoteView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These buttons aren't for you 🙂. Please type !menu in your own DM with me.",
                ephemeral=True
            )
            return False
        return True

    @ui.button(label="Edit note", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(NoteEditModal(owner_id=self.owner_id))

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await interaction.message.edit(content=f"Hi {interaction.user.mention}", view=MenuView(self.owner_id))

class MenuView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "These buttons aren't for you 🙂. Please type !menu in your DM with me.",
                ephemeral=True
            )
            return False
        return True

    @ui.button(label="I'm standing", style=discord.ButtonStyle.success)
    async def standing(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        text = await action_stand(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="I'm sitting", style=discord.ButtonStyle.success)
    async def sitting(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        text = await action_sit(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="Overview", style=discord.ButtonStyle.primary)
    async def overview(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        text = await action_overview(interaction.user)
        await interaction.message.edit(content=text, view=self)

    @ui.button(label="Set goal", style=discord.ButtonStyle.primary)
    async def set_goal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await interaction.message.edit(content="Choose a daily goal:", view=GoalView(self.owner_id))

    @ui.button(label="Reminders", style=discord.ButtonStyle.primary)
    async def reminders(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        text = await action_reminder_info(interaction.user)
        await interaction.message.edit(content=text, view=ReminderView(self.owner_id))

    @ui.button(label="Table notes", style=discord.ButtonStyle.secondary)
    async def table_note(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        note = get_note(interaction.user.id)
        if note:
            content = f"{interaction.user.mention} your saved note:\n**{note}**"
        else:
            content = f"{interaction.user.mention} you don't have a note yet. Click **Edit note** to add one."
        await interaction.message.edit(content=content, view=NoteView(self.owner_id))

    @ui.button(label="End", style=discord.ButtonStyle.danger)
    async def end(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        text = await action_end(interaction.user)
        await interaction.message.edit(content=text, view=self)

# ================= COMMANDS (DM ONLY) =================

@bot.command()
async def menu(ctx):
    if ctx.guild is not None:
        await ctx.send("Please use DM to talk to me 🙂")
        return
    ensure_today(ctx.author.id)
    await ctx.send(f"Hi {ctx.author.mention}", view=MenuView(ctx.author.id))

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
    await ctx.send(f"{ctx.author.mention} goal set to {minutes} minutes")

@bot.command()
async def end(ctx):
    if ctx.guild:
        return await ctx.send("Please use DM to talk to me 🙂")
    await ctx.send(await action_end(ctx.author))

# ================= GOAL + REMINDER CHECKERS =================

@tasks.loop(minutes=1)
async def goal_checker():
    for row in cursor.execute("SELECT user_id FROM users"):
        user_id = row[0]
        data = get_user(user_id)
        if not data:
            continue

        if not data.get("daily_goal_sec") or data.get("daily_goal_reached"):
            continue

        standing, _, _ = add_elapsed_to_totals(data)

        if standing >= int(data["daily_goal_sec"]):
            upsert_user(user_id, daily_goal_reached=1)
            try:
                user = await bot.fetch_user(user_id)
                await user.send("🎉 You reached your daily goal!")
            except discord.Forbidden:
                print(f"Could not DM user {user_id} (DMs disabled).")

@tasks.loop(minutes=1)
async def reminder_checker():
    for row in cursor.execute("SELECT user_id FROM users"):
        user_id = row[0]
        data = get_user(user_id)
        if not data:
            continue

        now = datetime.now()

        # -------- Sitting -> Stand reminder --------
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

        # -------- Standing -> Sit reminder --------
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

@bot.event
async def on_ready():
    print("Bot ready")
    if not goal_checker.is_running():
        goal_checker.start()
    if not reminder_checker.is_running():
        reminder_checker.start()

# ================= RUN =================

while True:
    try:
        bot.run(TOKEN)
    except Exception as e:
        print("Bot crashed, restarting...", e)