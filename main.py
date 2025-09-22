import discord 
from discord.ext
import commands tasks 
import asyncio
import datetime
import json 
import os 
import traceback

# ------------------ CONFIG & DATA ------------------

TOKEN = os.environ.get('DISCORD_BOT_TOKEN') DATA_FILE = 'bot_data.json'

# ------------------ HELPER FUNCTIONS ------------------

def safe_print(*args): try: print(*args) except: pass

def init_data_structure(): return { "users": {}, "logs": {"deletions": []}, # Add other default structures here }

def load_data(): if not os.path.exists(DATA_FILE): return init_data_structure() with open(DATA_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data): with open(DATA_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, default=str)

# ------------------ BOT INIT ------------------

intents = discord.Intents.all() bot = commands.Bot(command_prefix='!', intents=intents)

# ------------------ ADMIN: rpurge check ------------------

@bot.command(name="rpurge", help="(Admin) Check recent cached bulk deletions and possible actors.") @commands.has_permissions(manage_messages=True) async def cmd_rpurge(ctx: commands.Context): data = load_data() deletions = data.get("logs", {}).get("deletions", [])[-70:] embed = discord.Embed(title="🧾 Recent Cached Deletions", color=discord.Color.dark_red()) if not deletions: embed.description = "No cached deletions stored." await ctx.send(embed=embed) return for d in deletions[-15:]: content = (d.get("content") or "")[:200] embed.add_field(name=f"Msg {d.get('message_id')} by {d.get('author')}", value=f"{content}\nTime: {d.get('time')}", inline=False) await ctx.send(embed=embed)

# ------------------ DEBUG: rdump ------------------

@bot.command(name="rdump", help="(Admin) Dump JSON data for debugging") @commands.has_permissions(administrator=True) async def cmd_rdump(ctx: commands.Context): d = load_data() path = "rdump.json" with open(path, "w", encoding="utf-8") as f: json.dump(d, f, indent=2, default=str) await ctx.send("📦 Data dump:", file=discord.File(path)) try: os.remove(path) except: pass

# ------------------ DAILY ARCHIVE / CLEANUP ------------------

@tasks.loop(hours=24) async def daily_maintenance_task(): try: data = load_data() # prune daily entries older than 120 days cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=120) for uid, u in data.get("users", {}).items(): daily = u.get("daily_seconds", {}) keys_to_remove = [] for k in list(daily.keys()): try: ddt = datetime.datetime.strptime(k, "%Y-%m-%d") if ddt < cutoff: keys_to_remove.append(k) except: pass for k in keys_to_remove: daily.pop(k, None) save_data(data) except Exception as e: safe_print("⚠️ daily maintenance error:", e) traceback.print_exc()

# ------------------ EVENT: ON READY ------------------

@bot.event async def on_ready(): safe_print(f"✅ Logged in as {bot.user} ({bot.user.id})") if not daily_maintenance_task.is_running(): daily_maintenance_task.start()

# ------------------ YOUR ORIGINAL 49KB SCRIPT ------------------

 # mega_timetrack_with_audit_reconciliation.py
# Monolithic mega bot — updated to attribute role/channel permission changes, detect external mutes/unmutes,
# improved purge detection with audit log attribution, DM a fancier mute embed, and !rping toggle.
#
# Requirements:
# - Python 3.9+
# - discord.py 2.x
# - pytz
#
# Set environment variable DISCORD_TOKEN before running.

import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import pytz
import json
import os
import threading
import re
import traceback
from typing import Optional, List, Dict, Any

# ------------------ CONFIG ------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = 1416036219309002905
TRACK_CHANNEL_ID = 1416036222450794694

TRACK_ROLES = [
    1416036219909046304,
    1410420126003630122,
    1410423594579918860,
    1410421466666631279,
    1410421647265108038,
    1410419345234067568,
    1410422029236047975,
    1410458084874260592
]

RMUTE_ROLE_ID = 1410423854563721287
RCACHE_ROLES = [1410422029236047975, 1410422762895577088, 1406326282429403306]

OFFLINE_DELAY = 53                     # seconds for offline threshold
PRESENCE_CHECK_INTERVAL = 5            # how often presence tracker runs
AUTO_SAVE_INTERVAL = 120               # autosave interval (seconds)
DATA_FILE = "mega_bot_data.json"
BACKUP_DIR = "mega_bot_backups"
MAX_BACKUPS = 20
COMMAND_COOLDOWN = 4

# Audit log reconciliation lookback seconds (startup)
AUDIT_LOOKBACK_SECONDS = 3600  # 1 hour by default; increase if you want more reconciliation

# ------------------ INTENTS & BOT ------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
data_lock = threading.Lock()
console_lock = threading.Lock()
command_cooldowns: Dict[int, float] = {}

# ------------------ UTIL: SAFE PRINT ------------------
def safe_print(*args, **kwargs):
    with console_lock:
        print(*args, **kwargs)

# ------------------ PERSISTENCE ------------------
def init_data_structure() -> Dict[str, Any]:
    return {
        "users": {},
        "mutes": {},                 # keyed by mute_id
        "images": {},                # cached deleted attachments/messages
        "logs": {},                  # various logs
        "rmute_usage": {},           # moderator usage counts
        "last_audit_check": None,    # ISO timestamp of last audit reconciliation
        "rping_disabled_users": {}   # mapping user_id -> bool (True means disabled)
    }

def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            safe_print("⚠️ Failed to load data file:", e)
            try:
                os.rename(DATA_FILE, DATA_FILE + ".corrupt")
            except:
                pass
            return init_data_structure()
    else:
        return init_data_structure()

def rotate_backups():
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        files = sorted(os.listdir(BACKUP_DIR))
        while len(files) > MAX_BACKUPS:
            os.remove(os.path.join(BACKUP_DIR, files.pop(0)))
    except Exception as e:
        safe_print("⚠️ backup rotation error:", e)

def save_data(data: Dict[str, Any]):
    with data_lock:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            backup = os.path.join(BACKUP_DIR, f"backup_{ts}.json")
            with open(backup, "w", encoding="utf-8") as bf:
                json.dump(data, bf, indent=2, default=str)
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            rotate_backups()
        except Exception as e:
            safe_print("❌ Error saving data:", e)
            traceback.print_exc()

# ------------------ USER DATA HELPERS ------------------
def ensure_user_data(uid: str, data: Dict[str, Any]) -> None:
    if uid not in data["users"]:
        data["users"][uid] = {
            "status": "offline",
            "online_time": None,
            "offline_time": None,
            "last_message": None,
            "last_message_time": None,
            "last_edit": None,
            "last_edit_time": None,
            "last_delete": None,
            "last_online_times": {},
            "offline_timer": 0,
            "total_online_seconds": 0,
            "daily_seconds": {},
            "weekly_seconds": {},
            "monthly_seconds": {},
            "average_online": 0.0,
            "notify": True
        }

def add_seconds_to_user(uid: str, seconds: int, data: Dict[str, Any]) -> None:
    ensure_user_data(uid, data)
    u = data["users"][uid]
    u["total_online_seconds"] = u.get("total_online_seconds", 0) + seconds
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    week = datetime.datetime.utcnow().strftime("%Y-W%U")
    month = datetime.datetime.utcnow().strftime("%Y-%m")
    u["daily_seconds"][today] = u["daily_seconds"].get(today, 0) + seconds
    u["weekly_seconds"][week] = u["weekly_seconds"].get(week, 0) + seconds
    u["monthly_seconds"][month] = u["monthly_seconds"].get(month, 0) + seconds
    total_time = u["total_online_seconds"]
    total_days = max(len(u["daily_seconds"]), 1)
    u["average_online"] = total_time / total_days

# ------------------ TIMEZONE / FORMAT HELPERS ------------------
def tz_now_strings() -> Dict[str, str]:
    tzs = {
        "UTC": pytz.utc,
        "EST": pytz.timezone("US/Eastern"),
        "PST": pytz.timezone("US/Pacific"),
        "CET": pytz.timezone("Europe/Paris")
    }
    out = {}
    for k, v in tzs.items():
        out[k] = datetime.datetime.now(v).strftime("%Y-%m-%d %H:%M:%S")
    return out

def format_time(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def format_duration_seconds(sec: int) -> str:
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def parse_duration(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.match(r"^(\d+)([smhd])$", s.strip(), re.I)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "s": return val
    if unit == "m": return val * 60
    if unit == "h": return val * 3600
    if unit == "d": return val * 86400
    return None

def ascii_progress_bar(current: int, total: int, length: int = 20) -> str:
    try:
        ratio = min(max(float(current) / float(total), 0.0), 1.0)
        filled = int(length * ratio)
        return "█" * filled + "░" * (length - filled)
    except:
        return "░" * length

# ------------------ EMBED BUILDERS ------------------
def build_timetrack_embed(member: discord.Member, user_data: Dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=f"📊 Timetrack — {member.display_name}", color=discord.Color.blue())
    embed.add_field(name="Status", value=f"**{user_data.get('status','offline')}**", inline=True)
    embed.add_field(name="Online Since", value=user_data.get("online_time") or "N/A", inline=True)
    embed.add_field(name="Offline Since", value=user_data.get("offline_time") or "N/A", inline=True)
    embed.add_field(name="Last Message", value=user_data.get("last_message") or "N/A", inline=False)
    embed.add_field(name="Last Edit", value=user_data.get("last_edit") or "N/A", inline=False)
    embed.add_field(name="Last Delete", value=user_data.get("last_delete") or "N/A", inline=False)

    tz_map = user_data.get("last_online_times", {})
    tz_lines = [f"{tz}: {tz_map.get(tz,'N/A')}" for tz in ("UTC", "EST", "PST", "CET")]
    embed.add_field(name="Last Online (4 TZ)", value="\n".join(tz_lines), inline=False)

    total = user_data.get("total_online_seconds", 0)
    avg = int(user_data.get("average_online", 0))
    embed.add_field(name="Total Online (forever)", value=format_duration_seconds(total), inline=True)
    embed.add_field(name="Average Daily Online", value=format_duration_seconds(avg), inline=True)

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    todays = user_data.get("daily_seconds", {}).get(today, 0)
    embed.add_field(name="Today's Activity", value=f"{ascii_progress_bar(todays,3600)} ({todays}s)", inline=False)

    embed.set_footer(text="Timetrack • offline-delay 53s")
    return embed

def build_mute_dm_embed(target: discord.Member, moderator: discord.Member, duration_str: Optional[str], reason: str, auto: bool = False) -> discord.Embed:
    # fancier DM embed for muted user
    title = "🔇 You've been muted" if not auto else "🔇 You were auto-muted"
    embed = discord.Embed(title=title, color=discord.Color.dark_theme())
    embed.add_field(name="Server", value=f"{target.guild.name}", inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} ({moderator.id})", inline=False)
    if duration_str:
        embed.add_field(name="Duration", value=duration_str, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Appeal", value="If you believe this is incorrect, contact the moderation team.", inline=False)
    embed.set_footer(text="You may not receive DMs if you have DMs disabled.")
    return embed

def build_mute_log_embed(target: discord.Member, moderator: Optional[discord.Member], duration_str: Optional[str], reason: str, unmute_at: Optional[str] = None, source: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title="🔇 Mute Log", color=discord.Color.orange())
    embed.add_field(name="User", value=f"{target} ({target.id})", inline=False)
    embed.add_field(name="Moderator/Source", value=f"{moderator if moderator else source}", inline=False)
    if duration_str:
        embed.add_field(name="Duration", value=duration_str, inline=True)
    if unmute_at:
        embed.add_field(name="Unmute At", value=unmute_at, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text="Mute event logged")
    return embed

def build_unmute_log_embed(target: discord.Member, moderator: Optional[discord.Member], reason: Optional[str], auto: bool = False, source: Optional[str] = None) -> discord.Embed:
    title = "✅ Auto Unmute" if auto else "🔈 Unmute Log"
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="User", value=f"{target} ({target.id})", inline=False)
    if moderator:
        embed.add_field(name="Moderator", value=f"{moderator} ({moderator.id})", inline=False)
    if source and not moderator:
        embed.add_field(name="Source", value=source, inline=False)
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text="Unmute event")
    return embed

def build_purge_embed(actor: Optional[discord.Member], channel: discord.TextChannel, count: int, preview: List[str], when: str) -> discord.Embed:
    embed = discord.Embed(title="🗑️ Purge Detected", color=discord.Color.dark_red())
    embed.add_field(name="Channel", value=f"{channel.mention} ({channel.id})", inline=False)
    if actor:
        embed.add_field(name="Purged by", value=f"{actor} ({actor.id})", inline=True)
    else:
        embed.add_field(name="Purged by", value="Unknown / could be bot", inline=True)
    embed.add_field(name="Message count", value=str(count), inline=True)
    if preview:
        embed.add_field(name="Preview", value="\n".join(preview[:10]), inline=False)
    embed.set_footer(text=f"Purge at {when}")
    return embed

# ------------------ COMMAND COOLDOWN HELPER ------------------
def can_execute_command(user_id: int) -> bool:
    last = command_cooldowns.get(user_id, 0.0)
    now = datetime.datetime.utcnow().timestamp()
    if now - last >= COMMAND_COOLDOWN:
        command_cooldowns[user_id] = now
        return True
    return False

# ------------------ PRESENCE TRACKER TASK ------------------
@tasks.loop(seconds=PRESENCE_CHECK_INTERVAL)
async def presence_tracker_task():
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return
        channel = bot.get_channel(TRACK_CHANNEL_ID)
        if not channel:
            return
        data = load_data()
        now_utc = datetime.datetime.utcnow()
        for member in guild.members:
            # only track if they have a tracked role
            if not any(r.id in TRACK_ROLES for r in member.roles):
                continue
            uid = str(member.id)
            ensure_user_data(uid, data)
            u = data["users"][uid]
            if member.status != discord.Status.offline:
                # became online
                if u.get("status") == "offline":
                    u["status"] = "online"
                    u["online_time"] = format_time(now_utc)
                    u["offline_timer"] = 0
                    u["last_online_times"] = tz_now_strings()
                    # notify if allowed
                    if u.get("notify", True):
                        # respect rping settings for the owner
                        # if user has disabled ping, replace mention with name
                        recipient_id = member.id
                        rping_disabled = data.get("rping_disabled_users", {}).get(str(recipient_id), False)
                        mention = member.mention if not rping_disabled else member.display_name
                        try:
                            await channel.send(f"✅ {mention} is online")
                        except:
                            pass
                # credit online seconds
                add_seconds_to_user(uid, PRESENCE_CHECK_INTERVAL, data)
            else:
                # offline presence
                if u.get("status") == "online":
                    u["offline_timer"] = u.get("offline_timer", 0) + PRESENCE_CHECK_INTERVAL
                    if u["offline_timer"] >= OFFLINE_DELAY:
                        u["status"] = "offline"
                        u["offline_time"] = format_time(now_utc)
                        u["offline_timer"] = 0
                        u["last_online_times"] = tz_now_strings()
                        if u.get("notify", True):
                            recipient_id = member.id
                            rping_disabled = data.get("rping_disabled_users", {}).get(str(recipient_id), False)
                            mention = member.mention if not rping_disabled else member.display_name
                            try:
                                await channel.send(f"❌ {mention} is offline")
                            except:
                                pass
        save_data(data)
    except Exception as e:
        safe_print("❌ presence_tracker_task error:", e)
        traceback.print_exc()

# ------------------ AUTO SAVE ------------------
@tasks.loop(seconds=AUTO_SAVE_INTERVAL)
async def auto_save_task():
    try:
        save_data(load_data())
        safe_print("💾 Auto-saved data.")
    except Exception as e:
        safe_print("❌ auto_save_task error:", e)
        traceback.print_exc()

# ------------------ STARTUP: RECONCILE AUDIT LOGS ------------------
async def reconcile_audit_logs_on_start():
    """
    Called after bot ready. Looks at audit logs since last_audit_check timestamp and posts missed events.
    This attempts to catch up role/channel changes, member role updates and bulk deletes that happened
    while the bot was offline.
    """
    try:
        data = load_data()
        guild = bot.get_guild(GUILD_ID)
        channel = bot.get_channel(TRACK_CHANNEL_ID)
        if not guild or not channel:
            return
        last_check_iso = data.get("last_audit_check")
        now = datetime.datetime.utcnow()
        lookback_since = now - datetime.timedelta(seconds=AUDIT_LOOKBACK_SECONDS)
        # If last_check exists, use that; otherwise use lookback window
        if last_check_iso:
            try:
                last_check_dt = datetime.datetime.fromisoformat(last_check_iso)
            except:
                last_check_dt = lookback_since
        else:
            last_check_dt = lookback_since

        # We will check several audit log action types:
        actions_to_check = [
            discord.AuditLogAction.role_create,
            discord.AuditLogAction.role_delete,
            discord.AuditLogAction.role_update,
            discord.AuditLogAction.channel_create,
            discord.AuditLogAction.channel_delete,
            discord.AuditLogAction.channel_update,
            discord.AuditLogAction.message_bulk_delete,
            discord.AuditLogAction.member_role_update
        ]

        for action in actions_to_check:
            try:
                async for entry in guild.audit_logs(limit=50, action=action):
                    # audit entries are newest-first. We only want entries after last_check_dt
                    if entry.created_at.replace(tzinfo=None) < last_check_dt:
                        break
                    # process entries depending on action
                    if entry.action == discord.AuditLogAction.message_bulk_delete:
                        # message purge — attribute
                        target_channel = None
                        # message_bulk_delete entries don't include target in API consistently; we'll mention actor
                        actor = entry.user
                        when = entry.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        emb = discord.Embed(title="🗑️ Missed Bulk Delete (while offline)", color=discord.Color.dark_red())
                        emb.add_field(name="Possible actor", value=f"{actor} ({actor.id})", inline=False)
                        emb.add_field(name="When", value=when, inline=False)
                        emb.set_footer(text="Audit logs suggest a bulk delete occurred while bot was offline.")
                        try:
                            await channel.send(embed=emb)
                        except:
                            pass
                    elif entry.action == discord.AuditLogAction.member_role_update:
                        # member role added/removed while offline — try to attribute
                        target = entry.target
                        actor = entry.user
                        changes = entry.changes
                        change_desc = str(changes)
                        emb = discord.Embed(title="🛡️ Missed Member Role Update", color=discord.Color.orange())
                        emb.add_field(name="Member", value=f"{target} ({getattr(target,'id',str(target))})", inline=False)
                        emb.add_field(name="Actor", value=f"{actor} ({actor.id})", inline=False)
                        emb.add_field(name="Changes", value=change_desc, inline=False)
                        emb.set_footer(text=f"At {entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
                        try:
                            await channel.send(embed=emb)
                        except:
                            pass
                    elif entry.action in (discord.AuditLogAction.role_update, discord.AuditLogAction.role_create, discord.AuditLogAction.role_delete):
                        actor = entry.user
                        target = entry.target
                        emb = discord.Embed(title="⚙️ Missed Role Audit", color=discord.Color.orange())
                        emb.add_field(name="Action", value=str(entry.action), inline=True)
                        emb.add_field(name="Role", value=f"{target} ({getattr(target,'id',str(target))})", inline=True)
                        emb.add_field(name="Actor", value=f"{actor} ({actor.id})", inline=False)
                        emb.add_field(name="Changes", value=str(entry.changes), inline=False)
                        emb.set_footer(text=f"At {entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
                        try:
                            await channel.send(embed=emb)
                        except:
                            pass
                    elif entry.action in (discord.AuditLogAction.channel_update, discord.AuditLogAction.channel_create, discord.AuditLogAction.channel_delete):
                        actor = entry.user
                        target = entry.target
                        emb = discord.Embed(title="📢 Missed Channel Audit", color=discord.Color.blurple())
                        emb.add_field(name="Action", value=str(entry.action), inline=True)
                        emb.add_field(name="Channel", value=f"{target} ({getattr(target,'id',str(target))})", inline=True)
                        emb.add_field(name="Actor", value=f"{actor} ({actor.id})", inline=False)
                        emb.add_field(name="Changes", value=str(entry.changes), inline=False)
                        emb.set_footer(text=f"At {entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
                        try:
                            await channel.send(embed=emb)
                        except:
                            pass
            except Exception as e:
                safe_print("⚠️ audit log scanning error for action", action, e)
        # record last audit check time
        data["last_audit_check"] = datetime.datetime.utcnow().isoformat()
        save_data(data)
    except Exception as e:
        safe_print("❌ reconcile_audit_logs_on_start error:", e)
        traceback.print_exc()

# ------------------ EVENT: READY (start reconcile) ------------------
@bot.event
async def on_ready():
    safe_print(f"✅ Logged in as: {bot.user} ({bot.user.id})")
    # start tasks
    presence_tracker_task.start()
    auto_save_task.start()
    # reconcile audit logs (catch up)
    try:
        await reconcile_audit_logs_on_start()
    except Exception as e:
        safe_print("⚠️ reconcile audit on start failed:", e)
    safe_print("📡 Presence tracker & auto-save started.")

# ------------------ MESSAGE EVENTS (edits, deletes, bulk) ------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author and message.author.bot:
        return
    data = load_data()
    uid = str(message.author.id)
    ensure_user_data(uid, data)
    data["users"][uid]["last_message"] = (message.content or "")[:1900]
    data["users"][uid]["last_message_time"] = format_time(datetime.datetime.utcnow())
    # cache attachments if any
    if message.attachments:
        attachments = [a.url for a in message.attachments]
        data["images"][str(message.id)] = {
            "author": message.author.id,
            "time": format_time(datetime.datetime.utcnow()),
            "attachments": attachments,
            "content": (message.content or "")[:1900],
            "deleted_by": None
        }
    save_data(data)
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author and after.author.bot:
        return
    data = load_data()
    uid = str(after.author.id)
    ensure_user_data(uid, data)
    data["users"][uid]["last_edit"] = (after.content or "")[:1900]
    data["users"][uid]["last_edit_time"] = format_time(datetime.datetime.utcnow())
    # log edit
    data["logs"].setdefault("edits", []).append({
        "message_id": after.id,
        "author": after.author.id,
        "before": (before.content or "")[:1900],
        "after": (after.content or "")[:1900],
        "time": format_time(datetime.datetime.utcnow())
    })
    save_data(data)

@bot.event
async def on_message_delete(message: discord.Message):
    # single message deletion — we cache and try to attribute later
    if message.author and message.author.bot:
        return
    data = load_data()
    try:
        attachments = [a.url for a in message.attachments] if message.attachments else []
        data["images"][str(message.id)] = {
            "author": message.author.id if message.author else None,
            "time": format_time(datetime.datetime.utcnow()),
            "attachments": attachments,
            "content": (message.content or "")[:1900],
            "deleted_by": None
        }
        data["logs"].setdefault("deletions", []).append({
            "message_id": message.id,
            "author": message.author.id if message.author else None,
            "content": (message.content or "")[:1900],
            "attachments": attachments,
            "time": format_time(datetime.datetime.utcnow())
        })
    except Exception as e:
        safe_print("⚠️ on_message_delete error:", e)
    save_data(data)

@bot.event
async def on_bulk_message_delete(messages: List[discord.Message]):
    data = load_data()
    guild = None
    try:
        if messages:
            guild = messages[0].guild
    except:
        guild = None
    # cache and create a preview
    preview = []
    for m in messages[:15]:
        author_name = (m.author.display_name if m.author else "Unknown")
        preview.append(f"{author_name}: {(m.content or '')[:120]}")
        data["images"][str(m.id)] = {
            "author": m.author.id if m.author else None,
            "time": format_time(datetime.datetime.utcnow()),
            "attachments": [a.url for a in m.attachments] if m.attachments else [],
            "content": (m.content or "")[:1900],
            "deleted_by": None,
            "bulk_deleted": True
        }
        data["logs"].setdefault("deletions", []).append({
            "message_id": m.id,
            "author": m.author.id if m.author else None,
            "content": (m.content or "")[:1900],
            "attachments": [a.url for a in m.attachments] if m.attachments else [],
            "bulk": True,
            "time": format_time(datetime.datetime.utcnow())
        })
    # try to attribute via audit logs (message_bulk_delete)
    actor = None
    probable_actor = None
    try:
        if guild:
            async for entry in guild.audit_logs(limit=12, action=discord.AuditLogAction.message_bulk_delete):
                # find the most recent bulk delete entry
                # use created_at to pick likely one
                probable_actor = entry.user
                break
    except Exception as e:
        safe_print("⚠️ audit log check bulk delete failed:", e)
    # send embed to track channel
    channel = bot.get_channel(TRACK_CHANNEL_ID)
    when = format_time(datetime.datetime.utcnow())
    emb = build_purge_embed(probable_actor, messages[0].channel if messages else channel, len(messages), preview, when)
    if channel:
        try:
            await channel.send(embed=emb)
        except:
            pass
    save_data(data)

# ------------------ MEMBER UPDATE (role adds/removes) ATTRIBUTION ------------------
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    Detect role adds/removes and attribute them via audit logs:
    - If RMUTE role is added or removed, log who did it (even if by another bot).
    - For any role add/remove, log who changed the member roles.
    """
    try:
        before_roles = {r.id for r in before.roles}
        after_roles = {r.id for r in after.roles}
        added = after_roles - before_roles
        removed = before_roles - after_roles
        data = load_data()
        guild = after.guild
        channel = bot.get_channel(TRACK_CHANNEL_ID)
        # handle additions
        if added:
            for rid in added:
                # log member role add
                who = None
                # look at audit logs member_role_update to attribute actor
                try:
                    async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.member_role_update):
                        # entry.target is a Member
                        if getattr(entry.target, "id", None) == after.id:
                            # changes might include 'roles'
                            who = entry.user
                            break
                except Exception as e:
                    safe_print("⚠️ audit lookup for member role add failed:", e)
                # write log entry
                data["logs"].setdefault("member_role_changes", []).append({
                    "member": after.id,
                    "role_added": rid,
                    "by": who.id if who else None,
                    "time": format_time(datetime.datetime.utcnow()),
                    "type": "add"
                })
                # If it is RMUTE role -> handle mute event
                if rid == RMUTE_ROLE_ID:
                    # determine duration if known
                    # search data["mutes"] for a matching record with 'user' == after.id and not yet removed
                    found_mute_entry = None
                    for mid, m in data.get("mutes", {}).items():
                        if m.get("user") == after.id:
                            found_mute_entry = (mid, m)
                            break
                    moderator_member = who
                    source = None
                    if moderator_member:
                        source = f"{moderator_member} ({moderator_member.id})"
                    else:
                        source = "Unknown (possibly bot)"
                    # build nicer DM embed
                    try:
                        # duration if present
                        dur = None
                        unmute_at = None
                        if found_mute_entry:
                            dur = format_duration_seconds(found_mute_entry[1].get("duration_seconds", 0))
                            unmute_at = found_mute_entry[1].get("unmute_utc")
                        # DM the user with fancy embed
                        dm_embed = build_mute_dm_embed(after, moderator_member if moderator_member else bot.user, dur, found_mute_entry[1].get("reason","No reason provided") if found_mute_entry else "Muted (by role add)", auto=False)
                        try:
                            await after.send(embed=dm_embed)
                        except:
                            # user DMs blocked
                            pass
                        # log to channel
                        if channel:
                            log_embed = build_mute_log_embed(after, moderator_member, dur, found_mute_entry[1].get("reason","No reason provided") if found_mute_entry else "Muted (role added)", unmute_at, source=source)
                            await channel.send(embed=log_embed)
                    except Exception as e:
                        safe_print("⚠️ member role add RMUTE handling error:", e)
        # handle removals (unmute or role removed)
        if removed:
            for rid in removed:
                who = None
                try:
                    async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.member_role_update):
                        if getattr(entry.target, "id", None) == after.id:
                            who = entry.user
                            break
                except Exception as e:
                    safe_print("⚠️ audit lookup for member role remove failed:", e)
                data["logs"].setdefault("member_role_changes", []).append({
                    "member": after.id,
                    "role_removed": rid,
                    "by": who.id if who else None,
                    "time": format_time(datetime.datetime.utcnow()),
                    "type": "remove"
                })
                # If RMUTE role removed => unmute event
                if rid == RMUTE_ROLE_ID:
                    moderator_member = who
                    source = None
                    if moderator_member:
                        source = f"{moderator_member} ({moderator_member.id})"
                    else:
                        source = "Unknown (possibly bot)"
                    # find mute record and remove it
                    removed_record_id = None
                    for mid, m in data.get("mutes", {}).items():
                        if m.get("user") == after.id:
                            removed_record_id = mid
                            break
                    if removed_record_id:
                        # remove mute record
                        data["mutes"].pop(removed_record_id, None)
                    save_data(data)
                    # log to channel
                    ch = bot.get_channel(TRACK_CHANNEL_ID)
                    try:
                        if ch:
                            await ch.send(embed=build_unmute_log_embed(after, moderator_member, reason=None, auto=False, source=source))
                    except:
                        pass
    except Exception as e:
        safe_print("⚠️ on_member_update error:", e)
        traceback.print_exc()

# ------------------ ROLE/CHANNEL UPDATE EVENTS (with audit attribution) ------------------
@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    # attempt to attribute who updated the role
    try:
        guild = after.guild
        actor = None
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.role_update):
                if getattr(entry.target, "id", None) == after.id:
                    actor = entry.user
                    changes = entry.changes
                    break
        except Exception as e:
            safe_print("⚠️ role_update audit lookup failed:", e)
            changes = None
        # compose embed
        embed = discord.Embed(title="⚙️ Role Updated", color=discord.Color.orange())
        embed.add_field(name="Role", value=f"{after.name} ({after.id})", inline=False)
        embed.add_field(name="Edited by", value=f"{actor} ({actor.id})" if actor else "Unknown", inline=False)
        embed.add_field(name="Before (name/perms)", value=f"{before.name} / {str(before.permissions)}", inline=False)
        embed.add_field(name="After (name/perms)", value=f"{after.name} / {str(after.permissions)}", inline=False)
        embed.add_field(name="Changes (raw)", value=str(getattr(locals().get('changes', {}), '__str__', lambda: '{}')()) or "N/A", inline=False)
        ch = bot.get_channel(TRACK_CHANNEL_ID)
        if ch:
            await ch.send(embed=embed)
        # log
        data = load_data()
        data["logs"].setdefault("role_update", []).append({
            "role_id": after.id,
            "before_name": before.name,
            "after_name": after.name,
            "before_perms": str(before.permissions),
            "after_perms": str(after.permissions),
            "editor": actor.id if actor else None,
            "time": format_time(datetime.datetime.utcnow())
        })
        save_data(data)
    except Exception as e:
        safe_print("⚠️ on_guild_role_update error:", e)
        traceback.print_exc()

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    try:
        guild = after.guild
        actor = None
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.channel_update):
                # attribute for channel update
                if getattr(entry.target, "id", None) == after.id:
                    actor = entry.user
                    changes = entry.changes
                    break
        except Exception as e:
            safe_print("⚠️ channel_update audit lookup failed:", e)
            changes = None
        embed = discord.Embed(title="🔧 Channel Updated", color=discord.Color.blurple())
        embed.add_field(name="Channel", value=f"{after.name} ({after.id})", inline=False)
        embed.add_field(name="Edited by", value=f"{actor} ({actor.id})" if actor else "Unknown", inline=False)
        embed.add_field(name="Before", value=f"{before.name}", inline=True)
        embed.add_field(name="After", value=f"{after.name}", inline=True)
        embed.add_field(name="Raw changes", value=str(getattr(locals().get('changes', {}), '__str__', lambda: '{}')()) or "N/A", inline=False)
        ch = bot.get_channel(TRACK_CHANNEL_ID)
        if ch:
            await ch.send(embed=embed)
        # log
        data = load_data()
        data["logs"].setdefault("channel_update", []).append({
            "channel_id": after.id,
            "before_name": before.name,
            "after_name": after.name,
            "editor": actor.id if actor else None,
            "time": format_time(datetime.datetime.utcnow())
        })
        save_data(data)
    except Exception as e:
        safe_print("⚠️ on_guild_channel_update error:", e)
        traceback.print_exc()

# ------------------ COMMANDS: rmute/runmute/rmlb/rcache/tlb/rhelp/timetrack/tt/rping ------------------
@bot.command(name="rmute", help="Mute users: !rmute @u1 @u2 <duration> [reason]")
@commands.has_permissions(manage_roles=True)
async def cmd_rmute(ctx: commands.Context, targets: commands.Greedy[discord.Member], duration: str, *, reason: str = "No reason provided"):
    if not can_execute_command(ctx.author.id):
        await ctx.send("⌛ Command cooldown active.")
        return
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("❌ Invalid duration format (10s, 5m, 2h, 1d).")
        return
    if not targets:
        await ctx.send("❌ Mention at least one user.")
        return
    data = load_data()
    ch = bot.get_channel(TRACK_CHANNEL_ID)
    try:
        await ctx.message.delete()
    except:
        pass
    for target in targets:
        try:
            role = ctx.guild.get_role(RMUTE_ROLE_ID)
            if role is None:
                await ctx.send("⚠️ RMUTE role not configured on this server.")
                return
            # apply mute role
            await target.add_roles(role, reason=f"rmute by {ctx.author} reason: {reason}")
            mute_id = f"rmute_{target.id}_{int(datetime.datetime.utcnow().timestamp())}"
            unmute_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
            data["mutes"][mute_id] = {
                "user": target.id,
                "moderator": ctx.author.id,
                "reason": reason,
                "duration_seconds": seconds,
                "start_utc": format_time(datetime.datetime.utcnow()),
                "unmute_utc": format_time(unmute_at),
                "auto": True
            }
            # increment usage
            data["rmute_usage"][str(ctx.author.id)] = data.get("rmute_usage", {}).get(str(ctx.author.id), 0) + 1
            save_data(data)
            # DM the muted user with a cooler embed
            try:
                dm = build_mute_dm_embed(target, ctx.author, duration, reason, auto=False)
                await target.send(embed=dm)
            except:
                pass
            # log to track channel
            if ch:
                await ch.send(embed=build_mute_log_embed(target, ctx.author, duration, reason, format_time(unmute_at), source=f"{ctx.author}"))
            # schedule unmute
            async def auto_unmute(mute_record_id: str, user_id: int, seconds_left: int):
                await asyncio.sleep(seconds_left)
                g = bot.get_guild(GUILD_ID)
                if not g:
                    return
                member = g.get_member(user_id)
                if member:
                    r = g.get_role(RMUTE_ROLE_ID)
                    if r in member.roles:
                        try:
                            await member.remove_roles(r, reason="Auto-unmute")
                        except:
                            pass
                        # remove record
                        d2 = load_data()
                        if mute_record_id in d2.get("mutes", {}):
                            d2["mutes"].pop(mute_record_id, None)
                            save_data(d2)
                        c = bot.get_channel(TRACK_CHANNEL_ID)
                        if c:
                            await c.send(embed=build_unmute_log_embed(member, None, None, auto=True))
            bot.loop.create_task(auto_unmute(mute_id, target.id, seconds))
        except Exception as e:
            safe_print("❌ Error applying rmute:", e)
            traceback.print_exc()
    save_data(data)

@bot.command(name="runmute", help="Runmute a user (logs and auto-unmute).")
@commands.has_permissions(manage_roles=True)
async def cmd_runmute(ctx: commands.Context, target: discord.Member, duration: str, *, reason: str = "No reason provided"):
    if not can_execute_command(ctx.author.id):
        await ctx.send("⌛ Command cooldown active.")
        return
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("❌ Invalid duration.")
        return
    data = load_data()
    try:
        role = ctx.guild.get_role(RMUTE_ROLE_ID)
        if role is None:
            await ctx.send("⚠️ RMUTE role not configured.")
            return
        await target.add_roles(role, reason=f"runmute by {ctx.author}")
        mute_id = f"runmute_{target.id}_{int(datetime.datetime.utcnow().timestamp())}"
        unmute_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
        data["mutes"][mute_id] = {
            "user": target.id,
            "moderator": ctx.author.id,
            "reason": reason,
            "duration_seconds": seconds,
            "start_utc": format_time(datetime.datetime.utcnow()),
            "unmute_utc": format_time(unmute_at),
            "auto": True
        }
        save_data(data)
        if bot.get_channel(TRACK_CHANNEL_ID):
            await bot.get_channel(TRACK_CHANNEL_ID).send(embed=build_mute_log_embed(target, ctx.author, duration, reason, format_time(unmute_at)))
        # schedule unmute same as rmute
        async def runmute_unmute(mute_record_id: str, user_id: int, seconds_left: int):
            await asyncio.sleep(seconds_left)
            g = bot.get_guild(GUILD_ID)
            if not g:
                return
            member = g.get_member(user_id)
            if member:
                role_inner = g.get_role(RMUTE_ROLE_ID)
                try:
                    if role_inner in member.roles:
                        await member.remove_roles(role_inner, reason="Auto-unmute runmute")
                except:
                    pass
                dlocal = load_data()
                dlocal.get("mutes", {}).pop(mute_record_id, None)
                save_data(dlocal)
                c = bot.get_channel(TRACK_CHANNEL_ID)
                if c:
                    await c.send(embed=build_unmute_log_embed(member, None, None, auto=True))
        bot.loop.create_task(runmute_unmute(mute_id, target.id, seconds))
    except Exception as e:
        safe_print("❌ runmute error:", e)
        traceback.print_exc()

@bot.command(name="rmlb", help="Show top rmute users leaderboard")
async def cmd_rmlb(ctx: commands.Context):
    data = load_data()
    usage = data.get("rmute_usage", {})
    sorted_usage = sorted(usage.items(), key=lambda kv: kv[1], reverse=True)[:10]
    embed = discord.Embed(title="🏆 RMute Leaderboard", color=discord.Color.gold())
    for uid, cnt in sorted_usage:
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ID {uid}"
        embed.add_field(name=name, value=f"Mutes used: {cnt}", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="rcache", help="Show cached deleted images/files (role gated)")
async def cmd_rcache(ctx: commands.Context):
    # check roles
    if not any(r.id in RCACHE_ROLES for r in ctx.author.roles):
        await ctx.send("❌ You do not have permission to view cache.")
        return
    data = load_data()
    images = data.get("images", {})
    embed = discord.Embed(title="🗂️ Deleted Images/Files Cache", color=discord.Color.purple())
    count = 0
    for mid, info in list(images.items())[:40]:
        author = ctx.guild.get_member(info.get("author")) if info.get("author") else None
        author_str = author.display_name if author else str(info.get("author"))
        attachments = info.get("attachments", [])
        attachments_txt = "\n".join(attachments) if attachments else "None"
        content = (info.get("content") or "")[:500]
        deleted_by = info.get("deleted_by")
        embed.add_field(name=f"Msg {mid} by {author_str}", value=f"Time: {info.get('time')}\nDeleted by: {deleted_by}\nAttachments:\n{attachments_txt}\nContent: {content}", inline=False)
        count += 1
    if count == 0:
        embed.description = "No cached deleted images/files."
    await ctx.send(embed=embed)

@bot.command(name="tlb", help="Timetrack leaderboard")
async def cmd_tlb(ctx: commands.Context):
    data = load_data()
    users = data.get("users", {})
    top = sorted(users.items(), key=lambda kv: kv[1].get("total_online_seconds", 0), reverse=True)[:15]
    embed = discord.Embed(title="📊 Timetrack Leaderboard", color=discord.Color.green())
    for uid, ud in top:
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User ID {uid}"
        embed.add_field(name=name, value=f"Total Online: {format_duration_seconds(ud.get('total_online_seconds', 0))}", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="rhelp", help="Show commands")
async def cmd_rhelp(ctx: commands.Context):
    embed = discord.Embed(title="🤖 RHelp", color=discord.Color.blue())
    embed.add_field(name="!timetrack [user]", value="Show timetrack info.", inline=False)
    embed.add_field(name="!rmute @u1 @u2 <duration> [reason]", value="Mute user(s).", inline=False)
    embed.add_field(name="!runmute @u <duration> [reason]", value="Runmute + auto unmute.", inline=False)
    embed.add_field(name="!rmlb", value="Top mute-invokers.", inline=False)
    embed.add_field(name="!rcache", value="Show deleted images/files (roles only).", inline=False)
    embed.add_field(name="!tlb", value="Timetrack leaderboard.", inline=False)
    embed.add_field(name="!rping", value="Toggle ping replacement for your mentions (no ping if turned off).", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="timetrack", help="Show timetrack for a user.")
async def cmd_timetrack(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    data = load_data()
    uid = str(member.id)
    ensure_user_data(uid, data)
    embed = build_timetrack_embed(member, data["users"][uid])
    await ctx.send(embed=embed)

@bot.command(name="tt", help="Alias for timetrack")
async def cmd_tt(ctx: commands.Context, member: Optional[discord.Member] = None):
    await cmd_timetrack(ctx, member)

@bot.command(name="rping", help="Toggle whether the bot pings you (replaces mention with name when disabled).")
async def cmd_rping(ctx: commands.Context):
    # toggle per-user
    data = load_data()
    uid = str(ctx.author.id)
    disabled = data.get("rping_disabled_users", {}).get(uid, False)
    data.setdefault("rping_disabled_users", {})[uid] = not disabled
    save_data(data)
    status = "disabled" if not disabled else "enabled"
    await ctx.send(f"🔔 rping is now **{status}** for you. (When disabled, bot will not ping you; it will show your name instead.)")

# ------------------ ADMIN: rpurge check (attempt attribution) ------------------
@bot.command(name="rpurge", help="(Admin) Check recent cached bulk deletions and possible actors.")
@commands.has_permissions(manage_messages=True)
async def cmd_rpurge(ctx: commands.Context):
    data = load_data()
    deletions = data.get("logs", {}).get("deletions", [])[-70:]
    embed = discord.Embed(title="🧾 Recent Cached Deletions", color=discord.Color.dark_red())
    if not deletions:
        embed.description = "No cached deletions stored."
        await ctx.send(embed=embed)
        return
    for d in deletions[-15:]:
        content = (d.get("content") or "")[:200]
        embed.add_field(name=f"Msg {d.get('message_id')} by {d.get('author')}", value=f"{content}\nTime: {d.get('time')}", inline=False)
    await ctx.send(embed=embed)

# ------------------ DEBUG: rdump ------------------
@bot.command(name="rdump", help="(Admin) Dump JSON data for debugging")
@commands.has_permissions(administrator=True)
async def cmd_rdump(ctx: commands.Context):
    d = load_data()
    path = "rdump.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    await ctx.send("📦 Data dump:", file=discord.File(path))
    try:
        os.remove(path)
    except:
        pass

# ------------------ DAILY ARCHIVE / CLEANUP ------------------
@tasks.loop(hours=24)
async def daily_maintenance_task():
    try:
        data = load_data()
        # prune daily entries older than 120 days
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=120)
        for uid, u in data.get("users", {}).items():
            daily = u.get("daily_seconds", {})
            keys_to_remove = []
            for k in list(daily.keys()):
                try:
                    ddt = datetime.datetime.strptime(k, "%Y-%m-%d")
                    if ddt < cutoff:
                        keys_to_remove.append(k)
                except:
                    pass
            for k in keys_to_remove:
                daily.pop(k, None)
        save_data(data)
    except Exception as e:
        safe_print("⚠️ daily maintenance error:", e)
        traceback.print_exc()

# ------------------ STARTUP & RUN ------------------
if __name__ == "__main__":
    try:
        safe_print("🚀 Starting mega bot with audit reconciliation...")
        if not os.path.exists(DATA_FILE):
            save_data(init_data_structure())
        bot.run(TOKEN)
    except Exception as e:
        safe_print("❌ Fatal error while running bot:", e)
        traceback.print_exc()
# ------------------ STARTUP & RUN ------------------

if name == "main": try: safe_print("🚀 Starting mega bot with audit reconciliation...") if not os.path.exists(DATA_FILE): save_data(init_data_structure()) bot.run(TOKEN) except Exception as e: safe_print("❌ Fatal error while running bot:", e)

