# Elements Divided League Bot v5 — Elite Goon League
# pip install discord.py aiosqlite
# 2v2 league, max 3 players/team, rank-based system
import asyncio,logging,os,uuid,random,unicodedata
from datetime import datetime,timezone,timedelta
import aiosqlite,discord
from discord import app_commands
from discord.ext import commands,tasks
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)
TOKEN=os.environ.get("DISCORD_BOT_TOKEN","")
DB="league.db";DEFAULT_MMR=1000;MAX_TEAM=3;SEASON_WEEKS=6
ADMIN_ROLE="League Admin";TESTER_ROLE="EGL Tester";SPOON_ROLE="Spoon";SCRIM_BAN_ROLE="Scrim Banned"
MAPS=["Chessboard","Portal Mayhem","Construction Site","Parking Lot"]
SCHED_FMT="%d %b %H:%M";SCHED_HELP="DD Mon HH:MM or 8pm (e.g. 05 Aug 20:00 or 05 Aug 8pm)"

def parse_schedule(s):
    import re
    parts=s.strip().split()
    if len(parts)<3:raise ValueError("Need: DD Mon time")
    day=int(parts[0]);mon=parts[1];t=" ".join(parts[2:]).lower()
    m24=re.match(r'^(\d{1,2})[:\.](\d{2})$',t)
    m12=re.match(r'^(\d{1,2})[:\.]?(\d{2})?(am|pm)$',t)
    if m12:h=int(m12.group(1));mi=int(m12.group(2)or 0);ap=m12.group(3);h=0 if h==12 and ap=='am'else(12 if h==12 and ap=='pm'else(h+(12 if ap=='pm'else 0)))
    elif m24:h=int(m24.group(1));mi=int(m24.group(2))
    else:raise ValueError(f"Invalid time: {t}")
    yr=datetime.now(timezone.utc).year
    return datetime.strptime(f"{day:02d} {mon} {yr} {h:02d}:{mi:02d}","%d %b %Y %H:%M").replace(tzinfo=timezone.utc)
RANKS=[(0,"Awakened"),(900,"Adept"),(1000,"Elementalist"),(1050,"Master"),(1100,"Ascendant"),(1150,"Avatar")]
# Commands allowed while the emergency command lock is enabled.
LEAGUE_ADMIN_COMMANDS={
    "setup run","setup reset","scrimbot setup","scrimbot reset","setchannel",
    "guide","matchrules","scrimguide",
    "league create","league delete","league end","league finals","league tiebreaker","league grandfinal",
    "deleteteam","resetallteams","resetteams","scrimban","forcecaptainswap","forcekick","forcenamechange",
    "schedulestatus","closematch","mmr adjust","recordadjust",
    "generatematch week","test generatematches","leaderboard","teaminfoall","backup","restore",
    "tournament setup","tournament close-reg","tournament start","tournament nextround",
    "tournament end","tournament reset","tournament closematch",
    "disablecommands","enablecommands",
}

class EGLCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            return True
        try:
            async with aiosqlite.connect(DB) as db:
                async with db.execute(
                    "SELECT commands_disabled FROM bot_controls WHERE guild_id=?",
                    (str(interaction.guild_id),)
                ) as cur:
                    row=await cur.fetchone()
            disabled=bool(row and row[0])
        except Exception:
            log.exception("Could not read command-lock state")
            disabled=True  # Fail closed if the saved lock state cannot be read.

        if not disabled:
            return True
        qualified=interaction.command.qualified_name if interaction.command else ""
        if qualified in LEAGUE_ADMIN_COMMANDS and is_admin(interaction.user):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "🔒 **Commands are temporarily disabled.** Only League Admin commands are available. "
                "Ask a League Admin to run `/enablecommands`.", ephemeral=True
            )
        return False

intents=discord.Intents.default();intents.members=True;intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents,tree_cls=EGLCommandTree)

# Serialize private-thread member additions so match generation does not
# burst Discord's thread-member endpoint and trigger repeated 429s.
_thread_member_lock=asyncio.Lock()
_thread_member_last=0.0
_THREAD_MEMBER_INTERVAL=1.0

async def add_thread_member_safely(thread,member):
    global _thread_member_last
    async with _thread_member_lock:
        now=asyncio.get_running_loop().time()
        wait=_THREAD_MEMBER_INTERVAL-(now-_thread_member_last)
        if wait>0:
            await asyncio.sleep(wait)

        while True:
            try:
                await thread.add_user(member)
                _thread_member_last=asyncio.get_running_loop().time()
                return True
            except discord.HTTPException as e:
                if e.status==429:
                    retry_after=getattr(e,"retry_after",None)
                    if retry_after is None:
                        retry_after=5.0
                    log.warning(
                        "Discord rate limit adding %s to thread %s; retrying in %.2fs",
                        member.id,thread.id,retry_after
                    )
                    await asyncio.sleep(float(retry_after))
                    continue
                log.warning(
                    "Could not add %s to thread %s: %s",
                    member.id,thread.id,e
                )
                return False
            except Exception as e:
                log.warning(
                    "Could not add %s to thread %s: %s",
                    member.id,thread.id,e
                )
                return False

async def init_db():
    async with aiosqlite.connect(DB)as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS config(guild_id TEXT PRIMARY KEY,name TEXT,admin_id TEXT,announcements_ch TEXT,matches_ch TEXT,results_ch TEXT,general_ch TEXT,fa_ch TEXT,teams_ch TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS teams(guild_id TEXT,name TEXT,display TEXT,captain_id TEXT,secondary_captain_id TEXT,wins INT DEFAULT 0,losses INT DEFAULT 0,mmr INT DEFAULT 1000,thread_id TEXT,role_id TEXT,created_at TEXT,clantag TEXT,PRIMARY KEY(guild_id,name));
        CREATE TABLE IF NOT EXISTS members(guild_id TEXT,team_name TEXT,user_id TEXT,PRIMARY KEY(guild_id,team_name,user_id));
        CREATE TABLE IF NOT EXISTS fa(guild_id TEXT,user_id TEXT,username TEXT,joined_at TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,guild_id TEXT,week INT,team1 TEXT,team2 TEXT,score TEXT,winner TEXT,reporter TEXT,created_at TEXT,thread_id TEXT,is_finals INT DEFAULT 0,map TEXT,scheduled TEXT,reschedule_by TEXT,reschedule_to TEXT,reminder_sent INTEGER DEFAULT 0,overseer_id TEXT);
        CREATE TABLE IF NOT EXISTS season(guild_id TEXT PRIMARY KEY,weeks_done INT DEFAULT 0,finals_generated INT DEFAULT 0,last_generation_cycle TEXT);\n        CREATE TABLE IF NOT EXISTS finals_state(guild_id TEXT PRIMARY KEY,bracket_thread_id TEXT,bracket_msg_id TEXT,host_thread_id TEXT,stage_channel_id TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS player_history(guild_id TEXT,user_id TEXT,last_mmr INT DEFAULT 1000,cooldown_until TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS guild_settings(guild_id TEXT PRIMARY KEY,teams_ch TEXT);
        CREATE TABLE IF NOT EXISTS leaderboard_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT);
        CREATE TABLE IF NOT EXISTS roster_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT);
        CREATE TABLE IF NOT EXISTS team_commands_state(guild_id TEXT PRIMARY KEY,ch TEXT);
        CREATE TABLE IF NOT EXISTS setup_data(guild_id TEXT PRIMARY KEY,league_name TEXT,league_category_id TEXT,matches_category_id TEXT,announcements_ch TEXT,general_ch TEXT,teams_ch TEXT,fa_ch TEXT,matches_ch TEXT,results_ch TEXT);
        CREATE TABLE IF NOT EXISTS scrim_sessions(guild_id TEXT,date TEXT,thread_id TEXT,msg_id TEXT,max_players INT DEFAULT 4,PRIMARY KEY(guild_id,date));
        CREATE TABLE IF NOT EXISTS scrim_signups(guild_id TEXT,date TEXT,user_id TEXT,position INT,PRIMARY KEY(guild_id,date,user_id));
        CREATE TABLE IF NOT EXISTS scrim_v2(sid TEXT PRIMARY KEY,guild_id TEXT,date TEXT,thread_id TEXT,msg_id TEXT,thread_msg_id TEXT,max_players INT DEFAULT 4,scrim_title TEXT,unix_time INT,pinged INT DEFAULT 0,cleanup_retry_at INT);
        CREATE TABLE IF NOT EXISTS scrim_signups_v2(sid TEXT,guild_id TEXT,user_id TEXT,position INT,PRIMARY KEY(sid,user_id));
        CREATE TABLE IF NOT EXISTS scrim_bans(guild_id TEXT,user_id TEXT,expires_at INT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS bot_controls(guild_id TEXT PRIMARY KEY,commands_disabled INTEGER NOT NULL DEFAULT 0,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS underdog_bans(
            match_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            underdog_team TEXT NOT NULL,
            opponent_team TEXT NOT NULL,
            underdog_mmr INTEGER NOT NULL,
            opponent_mmr INTEGER NOT NULL,
            captain_id TEXT NOT NULL,
            selected_ultimate TEXT,
            banned_ultimate TEXT,
            announcement_message_id TEXT,
            schedule_message_id TEXT,
            created_at TEXT NOT NULL,
            confirmed_at TEXT
        );
        """)
        # Migrate: track which Sunday generation cycle has already run.
        try:await db.execute("ALTER TABLE season ADD COLUMN last_generation_cycle TEXT")
        except:pass
        # Migrate: add reminder_sent column for existing databases.
        try:await db.execute("ALTER TABLE matches ADD COLUMN reminder_sent INTEGER DEFAULT 0")
        except:pass
        # Migrate: store the Overseer who claims a match.
        try:await db.execute("ALTER TABLE matches ADD COLUMN overseer_id TEXT")
        except:pass
        # Migrate: store the League Admin reason when /closematch is used.
        try:await db.execute("ALTER TABLE matches ADD COLUMN close_reason TEXT")
        except:pass
        # Migrate: add optional second captain to existing teams.
        try:await db.execute("ALTER TABLE teams ADD COLUMN secondary_captain_id TEXT")
        except:pass
        # Migrate: add clantag column if missing
        try:await db.execute("ALTER TABLE teams ADD COLUMN clantag TEXT")
        except:pass
        # Migrate: add new columns to scrim_sessions if missing
        for col,typ in [("max_players","INT DEFAULT 4"),("scrim_title","TEXT"),("unix_time","INT"),("thread_id","TEXT"),("thread_msg_id","TEXT"),("pinged","INT DEFAULT 0")]:
            try:await db.execute(f"ALTER TABLE scrim_sessions ADD COLUMN {col} {typ}")
            except:pass
        # Persist mixed-scrim deletion retries across scheduler iterations/restarts.
        try:await db.execute("ALTER TABLE scrim_v2 ADD COLUMN cleanup_retry_at INT")
        except:pass
        # Migrate legacy scrims into scrim_v2 so their threads still close
        try:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT * FROM scrim_sessions")as c:
                legacy=[dict(r)for r in await c.fetchall()]
            for r in legacy:
                dd=r.get("date")or""
                if not dd:continue
                await db.execute("INSERT OR IGNORE INTO scrim_v2(sid,guild_id,date,thread_id,msg_id,thread_msg_id,max_players,scrim_title,unix_time,pinged) VALUES(?,?,?,?,?,NULL,?,?,?,0)",
                    ("L"+dd,r.get("guild_id"),dd,r.get("thread_id"),r.get("msg_id"),r.get("max_players")or 4,r.get("scrim_title")or"Mixed Scrim",r.get("unix_time")))
            async with db.execute("SELECT * FROM scrim_signups")as c:
                lsign=[dict(r)for r in await c.fetchall()]
            for r in lsign:
                dd=r.get("date")or""
                if not dd:continue
                await db.execute("INSERT OR IGNORE INTO scrim_signups_v2(sid,guild_id,user_id,position) VALUES(?,?,?,?)",("L"+dd,r.get("guild_id"),r.get("user_id"),r.get("position")or 0))
            if legacy or lsign:
                await db.execute("DELETE FROM scrim_sessions")
                await db.execute("DELETE FROM scrim_signups")
                log.info("migrated %d legacy scrims",len(legacy))
        except Exception as e:log.warning("scrim migrate: %s",e)
        # Enforce the roster cap at database level too, so concurrent invites
        # or future commands cannot add a fourth member.
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS limit_team_members
            BEFORE INSERT ON members
            WHEN (SELECT COUNT(*) FROM members
                  WHERE guild_id=NEW.guild_id AND team_name=NEW.team_name) >= 3
            BEGIN
                SELECT RAISE(ABORT, 'Team roster is limited to 3 players');
            END
        """)
        # Existing active mixed scrims must also follow the new 2v2 capacity.
        await db.execute("UPDATE scrim_v2 SET max_players=4 WHERE max_players>4")
        # Keep oversized legacy rosters intact (no players are removed), but
        # they will not be eligible for new league matches until corrected.
        async with db.execute("""
            SELECT guild_id, team_name, COUNT(*) AS n
            FROM members GROUP BY guild_id, team_name HAVING COUNT(*) > 3
        """) as c:
            oversized = await c.fetchall()
        for row in oversized:
            log.warning("Team %s in guild %s has %s members; reduce roster to 3 before it can play.",
                        row[1], row[0], row[2])
        await db.commit()

async def cfg_get(gid):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM config WHERE guild_id=?",(gid,))as c:
            r=await c.fetchone();return dict(r)if r else None

async def team_commands_channel(guild):
    """Return #general inside the EGL category for team commands and invites."""
    gid=str(guild.id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT ch FROM team_commands_state WHERE guild_id=?",(gid,)) as cur:
            row=await cur.fetchone()
    ch=guild.get_channel(int(row[0])) if row and row[0] else None
    # Never reuse a stale ID that points to the previously proposed management channel.
    if not (isinstance(ch,discord.TextChannel) and ch.name=="general" and ch.category and ch.category.name=="EGL"):
        ch=None
    if ch is None:
        egl_category=discord.utils.get(guild.categories,name="EGL")
        if egl_category:
            ch=discord.utils.get(egl_category.text_channels,name="general")
    return ch

async def log_team_action(guild, message, view=None):
    """Post team-management activity in #general under the EGL category."""
    ch=await team_commands_channel(guild)
    if ch:
        try:
            await ch.send(message,view=view)
            return ch
        except Exception as e:
            log.warning("Could not post team activity in EGL #general: %s",e)
    return None

async def team_get(gid,name):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM teams WHERE guild_id=? AND name=?",(gid,name.lower()))as c:
            r=await c.fetchone()
            if not r:return None
            t=dict(r)
        async with db.execute("SELECT user_id FROM members WHERE guild_id=? AND team_name=?",(gid,name.lower()))as c:
            t["members"]=[x[0]for x in await c.fetchall()]
        return t

async def team_by_player(gid,uid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT team_name FROM members WHERE guild_id=? AND user_id=?",(gid,uid))as c:
            r=await c.fetchone()
            if not r:return None
    return await team_get(gid,r[0])

async def teams_all(gid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT name FROM teams WHERE guild_id=?",(gid,))as c:
            names=[x[0]for x in await c.fetchall()]
    return[t for n in names if(t:=await team_get(gid,n))]

async def get_scrim_session(sid):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM scrim_v2 WHERE sid=?",(sid,))as c:
            r=await c.fetchone();return dict(r)if r else None

async def todays_scrims(gid,date):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM scrim_v2 WHERE guild_id=? AND date=? ORDER BY unix_time",(gid,date))as c:
            return[dict(r)for r in await c.fetchall()]

async def all_scrims(gid):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM scrim_v2 WHERE guild_id=? ORDER BY unix_time",(gid,))as c:
            return[dict(r)for r in await c.fetchall()]

async def scrim_signup_count(sid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT COUNT(*) FROM scrim_signups_v2 WHERE sid=?",(sid,))as c:
            r=await c.fetchone();return r[0]if r else 0

async def scrim_max_players(sid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT max_players FROM scrim_v2 WHERE sid=?",(sid,))as c:
            r=await c.fetchone();return r[0]if r and r[0] else 4

async def scrim_signups_active(sid):
    max_p=await scrim_max_players(sid)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups_v2 WHERE sid=? ORDER BY position ASC LIMIT ?",(sid,max_p))as c:
            return[r[0]for r in await c.fetchall()]

async def scrim_queue_list(sid):
    max_p=await scrim_max_players(sid)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups_v2 WHERE sid=? AND position>=? ORDER BY position ASC",(sid,max_p))as c:
            return[r[0]for r in await c.fetchall()]

async def scrim_next_queue(sid):
    max_p=await scrim_max_players(sid)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups_v2 WHERE sid=? AND position>=? ORDER BY position ASC LIMIT 1",(sid,max_p))as c:
            r=await c.fetchone();return r[0]if r else None

async def get_player_mmr(gid,uid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT last_mmr FROM player_history WHERE guild_id=? AND user_id=?",(gid,uid))as c:
            r=await c.fetchone();return r[0]if r else DEFAULT_MMR

async def set_cooldown(gid,uid,mmr):
    until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT OR REPLACE INTO player_history VALUES(?,?,?,?)",(gid,uid,mmr,until));await db.commit()

async def is_on_cooldown(gid,uid):
    g=bot.get_guild(int(gid))
    if g:
        m=g.get_member(int(uid))
        if m and any(r.name==TESTER_ROLE for r in m.roles):return False
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT cooldown_until FROM player_history WHERE guild_id=? AND user_id=?",(gid,uid))as c:
            r=await c.fetchone()
            if r and r[0]:
                until=datetime.fromisoformat(r[0])
                if datetime.now(timezone.utc)<until:return True
    return False

def find_role(guild,name):return discord.utils.get(guild.roles,name=name)

def is_admin(member):return any(r.name==ADMIN_ROLE for r in member.roles)
def is_tester(member):return any(r.name==TESTER_ROLE for r in member.roles)
def has_scrims(guild):
    for cat in guild.categories:
        if cat.name=="Scrims":return True
    return False
def get_rank(mmr):
    rank=RANKS[0][1]
    for floor,name in RANKS:
        if mmr>=floor:rank=name
    return rank

def rank_progress(mmr):
    cur_rank=RANKS[0];nxt=None
    for i,(floor,name)in enumerate(RANKS):
        if mmr>=floor:cur_rank=(floor,name)
        else:nxt=(floor,name);break
    if not nxt or cur_rank[0]==nxt[0]:return cur_rank[1],10,cur_rank[1]
    pct=min(10,int((mmr-cur_rank[0])/(nxt[0]-cur_rank[0])*10))
    bar="\u2588"*pct+"\u2591"*(10-pct)
    return cur_rank[1],pct,nxt[1]

async def add_roles(guild,member,team_display,captain=False):
    try:
        p=find_role(guild,"Player")
        if p:await member.add_roles(p)
        tr=find_role(guild,team_display)or await guild.create_role(name=team_display,mentionable=True,hoist=True)
        await member.add_roles(tr)
        if captain:
            cr=find_role(guild,"Captain")
            if cr:await member.add_roles(cr)
        return tr
    except discord.Forbidden:return None

async def rem_roles(guild,member,team_display,was_cap=False):
    try:
        to=[r for r in[find_role(guild,"Player"),find_role(guild,team_display)]if r]
        if was_cap and(cr:=find_role(guild,"Captain")):to.append(cr)
        if to:await member.remove_roles(*to)
    except discord.Forbidden:pass

def make_pairs(teams,week):
    if len(teams)<2:return[]
    lst=teams[:]
    if len(lst)%2:lst.append(None)
    n=len(lst);fixed=lst[0];rotating=lst[1:]
    off=(week-1)%(n-1);rotating=rotating[off:]+rotating[:off]
    full=[fixed]+rotating
    return[(full[i],full[n-1-i])for i in range(n//2)if full[i]and full[n-1-i]]

def _pairkey(a,b):
    a=str(a).strip().lower();b=str(b).strip().lower()
    return(a,b)if a<=b else(b,a)

async def need_league(i):
    if not await cfg_get(str(i.guild_id)):
        await i.response.send_message("\u274c No league.",ephemeral=True);return False
    return True

async def need_admin(i):
    if not is_admin(i.user):
        await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return False
    return True

@bot.tree.command(name="disablecommands",description="Temporarily disable all non-League-Admin commands")
async def disablecommands_cmd(i: discord.Interaction):
    if not await need_admin(i):
        return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO bot_controls(guild_id,commands_disabled,updated_at) VALUES(?,1,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_disabled=1,updated_at=excluded.updated_at",
            (gid,datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await i.response.send_message(
        "🔒 **Commands disabled.** Only League Admin commands are available. "
        "This setting remains active after a bot restart. Use `/enablecommands` to unlock commands.",
        ephemeral=True
    )
    await log_team_action(
        i.guild,
        "🔧 **EGL Bot Maintenance**\n"
        "Bot commands have been temporarily disabled while we work on updates and improvements. "
        "We’ll let everyone know when the bot is back online."
    )

@bot.tree.command(name="enablecommands",description="Re-enable all bot commands")
async def enablecommands_cmd(i: discord.Interaction):
    if not await need_admin(i):
        return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO bot_controls(guild_id,commands_disabled,updated_at) VALUES(?,0,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_disabled=0,updated_at=excluded.updated_at",
            (gid,datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    await i.response.send_message("🔓 **All commands are enabled again.**",ephemeral=True)
    await log_team_action(
        i.guild,
        "✅ **EGL Bot Update Complete**\n"
        "Bot maintenance is complete and commands are enabled again. Thanks for your patience!"
    )

def team_captain_ids(t):
    if not t:return []
    return [str(uid) for uid in (t.get("captain_id"),t.get("secondary_captain_id")) if uid]

async def need_captain(i):
    t=await team_by_player(str(i.guild_id),str(i.user.id))
    if not t:await i.response.send_message("\u274c Not on team.",ephemeral=True);return False
    if str(i.user.id) not in team_captain_ids(t):await i.response.send_message("\u274c Captain only.",ephemeral=True);return False
    return t["display"]

@bot.tree.command(name="addcaptain",description="Add a second captain to your team (captains only)")
@app_commands.describe(name="Player on your team to make the second captain")
async def addcaptain_cmd(i,name:discord.Member):
    gid=str(i.guild_id);uid=str(i.user.id)
    t=await team_by_player(gid,uid)
    if not t or uid not in team_captain_ids(t):
        await i.response.send_message("\u274c Only a captain of your team can use this command.",ephemeral=True);return
    if t.get("secondary_captain_id"):
        await i.response.send_message("\u274c Your team already has two captains.",ephemeral=True);return
    target_id=str(name.id)
    if target_id in team_captain_ids(t):
        await i.response.send_message("\u274c That player is already a captain.",ephemeral=True);return
    if target_id not in t["members"]:
        await i.response.send_message("\u274c The second captain must already be on your team.",ephemeral=True);return
    captain_role=find_role(i.guild,"Captain")
    if not captain_role:
        await i.response.send_message("\u274c The **Captain** role wasn't found. Ask a League Admin to run setup first.",ephemeral=True);return
    try:
        await name.add_roles(captain_role,reason=f"Added as second captain of {t['display']}")
    except discord.Forbidden:
        await i.response.send_message("\u274c I can't assign the Captain role. Check the bot's role permissions and role hierarchy.",ephemeral=True);return
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE teams SET secondary_captain_id=? WHERE guild_id=? AND name=? AND (secondary_captain_id IS NULL OR secondary_captain_id='')",(target_id,gid,t["name"]))
        await db.commit()
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT thread_id FROM matches WHERE guild_id=? AND winner IS NULL AND (team1=? OR team2=?) AND thread_id IS NOT NULL",(gid,t["name"],t["name"])) as cur:
            active_threads=await cur.fetchall()
    added_threads=0
    for tr in active_threads:
        try:
            th=i.guild.get_thread(int(tr["thread_id"]))
            if th is None: th=await i.guild.fetch_channel(int(tr["thread_id"]))
            if isinstance(th,discord.Thread) and th.is_private():
                await add_thread_member_safely(th,name)
                added_threads+=1
        except Exception as e:
            log.warning("Could not add second captain %s to match thread %s: %s",target_id,tr["thread_id"],e)
    await i.response.send_message(f"\u2705 {name.mention} is now the second captain of **{t['display']}** and has the same **Captain** role. Added to {added_threads} active match thread(s).")
    await log_team_action(i.guild,f"\u265c {name.mention} was made second captain of **{t['display']}** by {i.user.mention}.")

# ===== Underdog Ultimate Ban =====
UNDERDOG_ULTIMATES = [
    "Meteor", "Earth Trap", "Lava Eruption", "Metal Rain",
    "Water Torrent", "Atom Bomb", "Blood Flamethrower", "Leaf Tornado",
    "Fire Pillar", "Lightning Bolt", "Explosive Nova", "Fire Rain",
    "Air Vortex", "Air Slash Trap", "Storm Prison", "Spirit Orb",
]
_underdog_views_registered = set()


async def get_underdog_state(match_id):
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM underdog_bans WHERE match_id=?", (str(match_id),)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_underdog_schedule_confirmation(guild, match_id, banned_ultimate):
    """Update only the Underdog line in the already-posted schedule confirmation."""
    state = await get_underdog_state(match_id)
    if not state or not state.get("schedule_message_id"):
        return
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT thread_id FROM matches WHERE id=? AND guild_id=?", (str(match_id), state["guild_id"])) as cur:
            match_row = await cur.fetchone()
    if not match_row or not match_row[0]:
        return
    try:
        thread = guild.get_thread(int(match_row[0]))
        if thread is None:
            thread = await guild.fetch_channel(int(match_row[0]))
        message = await thread.fetch_message(int(state["schedule_message_id"]))
        line = f"🚫 **Underdog Ban:** {banned_ultimate}"
        content = message.content or ""
        import re
        if re.search(r"🚫 \*\*Underdog Ban:\*\*", content):
            content = re.sub(r"🚫 \*\*Underdog Ban:\*\*[^\n]*", line, content, count=1)
        else:
            content = content.rstrip() + "\n" + line
        # Keep the original schedule confirmation wording, line position and its existing view.
        await message.edit(content=content)
    except discord.NotFound:
        log.warning("Schedule confirmation %s for match %s no longer exists.", state["schedule_message_id"], match_id)
    except Exception as e:
        log.warning("Could not update Underdog line in schedule confirmation for %s: %s", match_id, e)


class UnderdogBanView(discord.ui.View):
    """Persistent, captain-only Ultimate selection and one-time confirmation."""
    def __init__(self, match_id, selected_ultimate=None):
        super().__init__(timeout=None)
        self.match_id = str(match_id)
        self.selected_ultimate = selected_ultimate
        options = [
            discord.SelectOption(label=name, value=name, default=(name == selected_ultimate))
            for name in UNDERDOG_ULTIMATES
        ]
        select = discord.ui.Select(
            placeholder="Choose one Ultimate to ban",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"egl_underdog_select:{self.match_id}",
            row=0,
        )
        select.callback = self.choose_ultimate
        self.add_item(select)
        confirm = discord.ui.Button(
            label="Confirm Ultimate Ban",
            style=discord.ButtonStyle.danger,
            custom_id=f"egl_underdog_confirm:{self.match_id}",
            row=1,
        )
        confirm.callback = self.confirm_ban
        self.add_item(confirm)

    async def _get_valid_state(self, interaction):
        state = await get_underdog_state(self.match_id)
        if not state:
            await interaction.response.send_message("This Underdog ban is no longer available.", ephemeral=True)
            return None
        if str(interaction.user.id) != str(state["captain_id"]):
            await interaction.response.send_message("Only the designated Underdog captain can use this selection.", ephemeral=True)
            return None
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT winner FROM matches WHERE id=? AND guild_id=?",
                (self.match_id, state["guild_id"])
            ) as cur:
                match_row = await cur.fetchone()
        if not match_row or match_row[0] is not None:
            await interaction.response.send_message("This match has ended or been deleted. The Underdog ban is closed.", ephemeral=True)
            return None
        if state.get("banned_ultimate"):
            await interaction.response.send_message("An Ultimate has already been banned for this match.", ephemeral=True)
            return None
        return state

    async def choose_ultimate(self, interaction):
        state = await self._get_valid_state(interaction)
        if not state:
            return
        selected = interaction.data.get("values", [None])[0]
        if selected not in UNDERDOG_ULTIMATES:
            await interaction.response.send_message("Invalid Ultimate selection.", ephemeral=True)
            return
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "UPDATE underdog_bans SET selected_ultimate=? WHERE match_id=? AND banned_ultimate IS NULL "
                "AND EXISTS (SELECT 1 FROM matches WHERE matches.id=underdog_bans.match_id AND matches.winner IS NULL)",
                (selected, self.match_id)
            )
            await db.commit()
        if cur.rowcount != 1:
            await interaction.response.send_message("The Underdog ban is no longer available.", ephemeral=True)
            return
        self.selected_ultimate = selected
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                for option in item.options:
                    option.default = option.value == selected
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"You selected **{selected}**. Press **Confirm Ultimate Ban** to finalize it.",
            ephemeral=True
        )

    async def confirm_ban(self, interaction):
        state = await self._get_valid_state(interaction)
        if not state:
            return
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT selected_ultimate FROM underdog_bans WHERE match_id=? AND banned_ultimate IS NULL",
                (self.match_id,)
            ) as cur:
                selected_row = await cur.fetchone()
            selected = selected_row["selected_ultimate"] if selected_row else None
            if not selected:
                await db.rollback()
                await interaction.response.send_message("Choose an Ultimate from the dropdown first.", ephemeral=True)
                return
            cur = await db.execute(
                "UPDATE underdog_bans SET banned_ultimate=?,confirmed_at=? "
                "WHERE match_id=? AND banned_ultimate IS NULL "
                "AND EXISTS (SELECT 1 FROM matches WHERE matches.id=underdog_bans.match_id AND matches.winner IS NULL)",
                (selected, datetime.now(timezone.utc).isoformat(), self.match_id)
            )
            await db.commit()
            if cur.rowcount != 1:
                await interaction.response.send_message("An Ultimate has already been banned or this match has ended.", ephemeral=True)
                return

        await interaction.response.defer()
        # Edit the original pinned public announcement, removing the controls.
        try:
            announcement = interaction.message
            base = announcement.content or ""
            base += f"\n\n🚫 **Banned Ultimate: {selected}**\nThe opposing team cannot use this Ultimate during this match."
            await announcement.edit(content=base, view=None)
        except Exception as e:
            log.warning("Could not update Underdog announcement for match %s: %s", self.match_id, e)
        await update_underdog_schedule_confirmation(interaction.guild, self.match_id, selected)
        try:
            await interaction.followup.send(f"🚫 **{selected}** has been banned for the opposing team for this match.")
        except Exception as e:
            log.warning("Could not post Underdog ban confirmation for match %s: %s", self.match_id, e)


async def snapshot_underdog_for_match(gid, match_id, team1_display, team2_display):
    """Persist the MMR comparison as soon as a match is created."""
    teams = await teams_all(str(gid))
    t1 = next((t for t in teams if str(t.get("display", "")).casefold() == str(team1_display).casefold()), None)
    t2 = next((t for t in teams if str(t.get("display", "")).casefold() == str(team2_display).casefold()), None)
    if not t1 or not t2:
        log.warning("Underdog snapshot skipped for match %s: could not resolve both teams.", match_id)
        return None
    mmr1, mmr2 = int(t1.get("mmr", DEFAULT_MMR)), int(t2.get("mmr", DEFAULT_MMR))
    if mmr1 == mmr2:
        return None
    underdog, opponent = (t1, t2) if mmr1 < mmr2 else (t2, t1)
    underdog_mmr, opponent_mmr = (mmr1, mmr2) if mmr1 < mmr2 else (mmr2, mmr1)
    captain_id = underdog.get("captain_id")
    if not captain_id:
        log.warning("Underdog snapshot skipped for match %s: %s has no primary captain ID.", match_id, underdog.get("display"))
        return None
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO underdog_bans(match_id,guild_id,underdog_team,opponent_team,underdog_mmr,opponent_mmr,captain_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(match_id), str(gid), underdog["display"], opponent["display"], underdog_mmr, opponent_mmr,
             str(captain_id), datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    return True


async def create_underdog_announcement(guild, thread, match_id, team1_display, team2_display):
    """Post/pin the saved Underdog decision before any other thread text."""
    teams = await teams_all(str(guild.id))
    t1 = next((t for t in teams if str(t.get("display", "")).casefold() == str(team1_display).casefold()), None)
    t2 = next((t for t in teams if str(t.get("display", "")).casefold() == str(team2_display).casefold()), None)
    if not t1 or not t2:
        log.warning("Underdog skipped for match %s: could not resolve both teams (%s vs %s).", match_id, team1_display, team2_display)
        return None
    state = await get_underdog_state(match_id)
    if not state:
        await snapshot_underdog_for_match(str(guild.id), match_id, team1_display, team2_display)
        state = await get_underdog_state(match_id)
    if not state:
        return None
    underdog = next((t for t in teams if str(t.get("display", "")).casefold() == str(state["underdog_team"]).casefold()), None)
    opponent = next((t for t in teams if str(t.get("display", "")).casefold() == str(state["opponent_team"]).casefold()), None)
    if not underdog or not opponent:
        log.warning("Underdog announcement skipped for match %s: a team record no longer exists.", match_id)
        return None
    captain_id = state["captain_id"]
    underdog_role = guild.get_role(int(underdog["role_id"])) if underdog.get("role_id") else None
    opponent_role = guild.get_role(int(opponent["role_id"])) if opponent.get("role_id") else None
    underdog_mention = underdog_role.mention if underdog_role else f"**{underdog['display']}**"
    opponent_mention = opponent_role.mention if opponent_role else f"**{opponent['display']}**"
    content = (
        "**UNDERDOGS:**\n\n"
        f"{underdog_mention} has a lower rating than {opponent_mention} and receives one Ultimate ban against the opposing team.\n\n"
        f"<@{captain_id}>, choose which Ultimate you want removed from the enemy team's selection using the dropdown menu below."
    )
    view = UnderdogBanView(match_id)
    try:
        message = await thread.send(content, view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        try:
            await message.pin(reason="EGL Underdog Ultimate Ban")
        except Exception as e:
            log.warning("Could not pin Underdog announcement for match %s: %s", match_id, e)
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "UPDATE underdog_bans SET announcement_message_id=? WHERE match_id=?",
                (str(message.id), str(match_id))
            )
            await db.commit()
        _underdog_views_registered.add(str(match_id))
        return message
    except Exception as e:
        log.warning("Could not create Underdog announcement for match %s: %s", match_id, e)
        return None


# ===== Views =====
async def captain_team(gid,uid):
    t=await team_by_player(gid,uid)
    return t["display"]if t else None

async def record_sched_event(gid,match_id,team,action,detail=""):
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS sched_events(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT,match_id TEXT,team TEXT,action TEXT,detail TEXT,ts TEXT)")
        await db.execute("INSERT INTO sched_events(guild_id,match_id,team,action,detail,ts) VALUES(?,?,?,?,?,?)",(gid,match_id,team,action,detail,datetime.now(timezone.utc).isoformat()))
        await db.commit()

class MapVoteView(discord.ui.View):
    def __init__(self,mid,th):
        super().__init__(timeout=604800);self.mid=mid;self.th=th;self.p={}
    async def _v(self,i,mn):
        uid=str(i.user.id)
        async with aiosqlite.connect(DB)as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT team1,team2 FROM matches WHERE id=?",(self.mid,))as cur:
                row=await cur.fetchone()
        if not row:await i.response.send_message("Match not found.",ephemeral=True);return
        gid=str(i.guild_id)
        t1=await team_get(gid,row["team1"]);t2=await team_get(gid,row["team2"])
        caps=team_captain_ids(t1)+team_captain_ids(t2)
        if uid not in caps:await i.response.send_message("Captains only.",ephemeral=True);return
        if uid in self.p:await i.response.send_message("Already voted.",ephemeral=True);return
        self.p[uid]=mn;await i.response.send_message(f"{mn}!",ephemeral=True)
        if len(self.p)==2:
            vs=list(self.p.values())
            res=vs[0]if vs[0]==vs[1]else random.choice(vs)
            coin=" (coin flip!)"if vs[0]!=vs[1]else""
            for c in self.children:c.disabled=True
            await i.edit_original_response(content=f"Map: **{res}**{coin}",view=None)
            await self.th.send(f"\U0001f5fa\ufe0f Map: **{res}**{coin}")
            async with aiosqlite.connect(DB)as db2:await db2.execute("UPDATE matches SET map=? WHERE id=?",(res,self.mid));await db2.commit()

async def send_map_vote(channel,mid):
    mv=MapVoteView(mid,channel)
    for mn in MAPS:
        btn=discord.ui.Button(label=mn,style=discord.ButtonStyle.primary)
        async def cb(i,mn=mn):await mv._v(i,mn)
        btn.callback=cb;mv.add_item(btn)
    return await channel.send("**\U0001f5fa\ufe0f Map Vote:**",view=mv)


async def _match_schedule_row(i):
    if not isinstance(i.channel,discord.Thread):
        await i.response.send_message("\u274c Match threads only.",ephemeral=True);return None,None
    captain=await need_captain(i)
    if not captain:return None,None
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=?",(str(i.channel.id),gid))as cur:
            row=await cur.fetchone()
    if not row:
        await i.response.send_message("\u274c No match here.",ephemeral=True);return None,None
    return dict(row),captain

async def _match_week_dates(gid,mid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT created_at,week FROM matches WHERE id=? AND guild_id=?",(mid,gid))as cur:
            row=await cur.fetchone()
    if not row:return []
    try:
        created=datetime.fromisoformat(row[0].replace("Z","+00:00")).date()
    except:
        return []
    # League scheduling week is Monday through Sunday.
    # If the match was generated on Sunday, that Sunday is the generation day;
    # the selectable week therefore starts the following Monday and ends the
    # following Sunday.
    if created.weekday()==6:
        start=created+timedelta(days=1)
    else:
        start=created-timedelta(days=created.weekday())
    return [start+timedelta(days=n) for n in range(7)]

class SchedulePickerView(discord.ui.View):
    def __init__(self,author_id,dates,callback):
        super().__init__(timeout=300)
        self.author_id=str(author_id);self.dates=dates;self.callback_fn=callback;self.selected_date=None
        self._show_days()

    async def _owner(self,i):
        if str(i.user.id)!=self.author_id:
            await i.response.send_message("\u274c Only the captain who opened this scheduler can use it.",ephemeral=True);return False
        return True

    def _show_days(self):
        self.clear_items()
        opts=[discord.SelectOption(label=d.strftime("%a %d %b"),value=d.isoformat(),description=d.strftime("%A")) for d in self.dates]
        sel=discord.ui.Select(placeholder="\U0001f4c5 Choose a day",options=opts,row=0)
        async def choose(i):
            if not await self._owner(i):return
            self.selected_date=datetime.fromisoformat(sel.values[0]).date();self._show_times()
            await i.response.edit_message(content=f"\U0001f4c5 **{self.selected_date.strftime('%A, %d %B')}**\n\nChoose a time (GMT):",view=self)
        sel.callback=choose;self.add_item(sel)
        cancel=discord.ui.Button(label="\u274c Cancel",style=discord.ButtonStyle.secondary,row=4)
        async def cancel_cb(i):
            if not await self._owner(i):return
            self.stop();await i.response.edit_message(content="\u274c Scheduling cancelled.",view=None)
        cancel.callback=cancel_cb;self.add_item(cancel)

    def _show_times(self):
        self.clear_items()
        for row,(start,end,label) in enumerate(((0,12,"00:00 \u2013 11:30"),(12,24,"12:00 \u2013 23:30"))):
            opts=[discord.SelectOption(label=f"{h:02d}:{m:02d} GMT",value=f"{h:02d}:{m:02d}") for h in range(start,end) for m in (0,30)]
            sel=discord.ui.Select(placeholder=f"\U0001f550 {label}",options=opts,row=row)
            async def choose_time(i,sel=sel):
                if not await self._owner(i):return
                hh,mm=map(int,sel.values[0].split(":"))
                dt=datetime.combine(self.selected_date,datetime.min.time(),tzinfo=timezone.utc).replace(hour=hh,minute=mm)
                if dt.timestamp()<=datetime.now(timezone.utc).timestamp():
                    await i.response.send_message("\u274c That time has already passed. Please choose a future time.",ephemeral=True);return
                self._show_confirm(dt)
                unix=int(dt.timestamp())
                await i.response.edit_message(
                    content=f"\U0001f4c5 **{dt.strftime('%A, %d %B')}**\n"
                            f"\U0001f550 **{dt.strftime('%H:%M')} GMT**\n"
                            f"\U0001f552 **Your time:** <t:{unix}:f>\n\n"
                            f"Confirm this time?",
                    view=self
                )
            sel.callback=choose_time;self.add_item(sel)
        back=discord.ui.Button(label="\u2190 Back",style=discord.ButtonStyle.secondary,row=4)
        async def back_cb(i):
            if not await self._owner(i):return
            self._show_days();await i.response.edit_message(content="\U0001f4c5 **Choose a day for this match**",view=self)
        back.callback=back_cb;self.add_item(back)

    def _show_confirm(self,dt):
        self.clear_items()
        confirm=discord.ui.Button(label="\u2705 Use This Time",style=discord.ButtonStyle.green,row=4)
        async def confirm_cb(i):
            if not await self._owner(i):return
            self.stop()
            try:await i.message.delete()
            except:pass
            await self.callback_fn(i,dt)
        confirm.callback=confirm_cb;self.add_item(confirm)
        change=discord.ui.Button(label="\u21a9\ufe0f Change",style=discord.ButtonStyle.secondary,row=4)
        async def change_cb(i):
            if not await self._owner(i):return
            self._show_times();await i.response.edit_message(content=f"\U0001f4c5 **{self.selected_date.strftime('%A, %d %B')}**\n\nChoose a time (GMT):",view=self)
        change.callback=change_cb;self.add_item(change)
        cancel=discord.ui.Button(label="\u274c Cancel",style=discord.ButtonStyle.red,row=4)
        async def cancel_cb(i):
            if not await self._owner(i):return
            self.stop();await i.response.edit_message(content="\u274c Scheduling cancelled.",view=None)
        cancel.callback=cancel_cb;self.add_item(cancel)

async def _send_schedule_proposal(i,row,actor_team,dt,mode):
    if dt.timestamp()<=datetime.now(timezone.utc).timestamp():
        await i.response.send_message("\u274c That time has already passed. Please choose a future time.",ephemeral=True);return
    gid=str(i.guild_id);other=row["team2"] if actor_team==row["team1"] else row["team1"];ot=await team_get(gid,other)
    if not ot:
        await i.response.send_message("\u274c Other team gone.",ephemeral=True);return
    oc=i.guild.get_member(int(ot["captain_id"]))
    if not oc:
        await i.response.send_message("\u274c Other captain not found.",ephemeral=True);return
    sched=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
    await record_sched_event(gid,row["id"],actor_team,"propose",sched)
    v=ScheduleConfirmView(row["id"],ot["captain_id"],str(i.user.id),sched,unix,str(i.channel.id))
    verb="proposes" if mode=="schedule" else "wants"
    await i.response.send_message(
        f"\U0001f4c5 {i.user.mention} {verb}: **{sched}**\n"
        f"\U0001f552 **Your time:** <t:{unix}:f>\n\n"
        f"{oc.mention} please confirm:",
        view=v
    )

async def _open_schedule_picker(i,mode):
    row,actor=await _match_schedule_row(i)
    if not row:return

    dates=await _match_week_dates(str(i.guild_id),row["id"])
    if not dates:
        await i.response.send_message("\u274c Could not determine this match's week.",ephemeral=True);return
    async def picked(interaction,dt): await _send_schedule_proposal(interaction,row,actor,dt,mode)
    view=SchedulePickerView(i.user.id,dates,picked)
    await i.response.send_message(f"\U0001f4c5 **Choose a day for {row['team1']} vs {row['team2']}**\n\nOnly the 7 days belonging to **Week {row['week']}** are available.",view=view,ephemeral=True)

class CounterPickerLauncher:
    def __init__(self,view):self.view=view
    async def open(self,i):
        gid=str(i.guild_id);dates=await _match_week_dates(gid,self.view.mid)
        if not dates:
            await i.response.send_message("\u274c Could not determine this match's week.",ephemeral=True);return
        async def picked(interaction,dt):
            if dt.timestamp()<=datetime.now(timezone.utc).timestamp():
                await interaction.response.send_message("\u274c That time has already passed. Please choose a future time.",ephemeral=True);return
            team=await captain_team(gid,self.view.actor_id);ns=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
            await record_sched_event(gid,self.view.mid,team or "?","counter",ns);self.view.done=True
            other_member=i.guild.get_member(int(self.view.proposer_id))
            nv=ScheduleConfirmView(self.view.mid,self.view.proposer_id,self.view.actor_id,ns,unix,self.view.thread_id)
            await interaction.response.send_message(
                f"\U0001f504 {interaction.user.mention} counter-proposes **{ns}**\n"
                f"\U0001f552 **Your time:** <t:{unix}:f>\n\n"
                f"{other_member.mention if other_member else ''} please reply:",
                view=nv
            )
        await i.response.send_message("\U0001f504 **Counter Offer**\n\nChoose a new day and time from this match's week.",view=SchedulePickerView(i.user.id,dates,picked),ephemeral=True)


class OverseerSpectateView(discord.ui.View):
    def __init__(self,mid):
        super().__init__(timeout=None)
        self.mid=mid
        self.claimed=False

    @discord.ui.button(label="👁️ Spectate Match",style=discord.ButtonStyle.secondary)
    async def spectate(self,i,btn):
        role=find_role(i.guild,"Overseer")
        if not role or role not in i.user.roles:
            await i.response.send_message("❌ Only members with the **Overseer** role can choose to spectate a match.",ephemeral=True)
            return
        if self.claimed:
            await i.response.send_message("❌ An Overseer has already chosen to spectate this match.",ephemeral=True)
            return
        async with aiosqlite.connect(DB) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT overseer_id FROM matches WHERE id=?",(self.mid,)) as cur:
                row=await cur.fetchone()
        if row and row["overseer_id"]:
            await i.response.send_message("❌ An Overseer has already chosen to spectate this match.",ephemeral=True)
            self.claimed=True
            return

        async with aiosqlite.connect(DB) as db:
            cur=await db.execute(
                "UPDATE matches SET overseer_id=? WHERE id=? AND (overseer_id IS NULL OR overseer_id='')",
                (str(i.user.id),self.mid)
            )
            await db.commit()
            if cur.rowcount != 1:
                await i.response.send_message("❌ An Overseer has already chosen to spectate this match.",ephemeral=True)
                self.claimed=True
                return

        self.claimed=True
        btn.disabled=True
        btn.label=f"👁️ Overseer: {i.user.display_name}"
        await i.response.edit_message(
            content=i.message.content+
                    f"\n\n👁️ **Overseer spectating:** {i.user.mention}\n"
                    "🎥 **Recording the match only — VC must remain OFF.**",
            view=self
        )


class ScheduleConfirmView(discord.ui.View):
    def __init__(self,mid,actor_id,proposer_id,sched,unix,thread_id):
        super().__init__(timeout=86400);self.mid=mid;self.actor_id=actor_id;self.proposer_id=proposer_id;self.sched=sched;self.unix=unix;self.thread_id=thread_id;self.done=False
    async def _check(self,i):
        if self.done:await i.response.send_message("Stale proposal.",ephemeral=True);return False
        team_name=await captain_team(str(i.guild_id),self.actor_id)
        team=await team_get(str(i.guild_id),team_name) if team_name else None
        if str(i.user.id) not in team_captain_ids(team):await i.response.send_message("Other captain only.",ephemeral=True);return False
        return True
    @discord.ui.button(label="\u2705 Confirm",style=discord.ButtonStyle.green)
    async def confirm(self,i,btn):
        if not await self._check(i):return
        gid=str(i.guild_id)
        async with aiosqlite.connect(DB)as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT team1,team2 FROM matches WHERE id=?",(self.mid,))as cur:row=await cur.fetchone()
        if not row:await i.response.send_message("Match not found.",ephemeral=True);return
        team=await captain_team(gid,self.actor_id)
        await record_sched_event(gid,self.mid,team or "?","confirm",self.sched)
        async with aiosqlite.connect(DB)as db:await db.execute("UPDATE matches SET scheduled=?, reminder_sent=0 WHERE thread_id=?",(self.sched,self.thread_id));await db.commit()
        self.done=True
        for c in self.children:c.disabled=True
        await i.response.defer()
        try:
            await i.message.delete()
        except Exception:
            pass

        overseer_role=find_role(i.guild,"Overseer")
        overseer_ping=f" {overseer_role.mention}" if overseer_role else ""
        underdog_state=await get_underdog_state(self.mid)
        underdog_line=(
            f"\n🚫 **Underdog Ban:** {underdog_state['banned_ultimate'] or 'Pending'}\n\n"
            if underdog_state else "\n\n"
        )
        confirmation=await i.channel.send(
            content=f"📅 **Confirmed!** Match scheduled: **{self.sched}**\n"
                    f"🕐 Your time: <t:{self.unix}:f>"
                    f"{underdog_line}"
                    f"First {overseer_ping} to Click **👁️ Spectate Match** will record the match. "
                    "Overseer's VC will be turned OFF and will NOT interfere with your match.",
            view=OverseerSpectateView(self.mid)
        )
        if underdog_state:
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE underdog_bans SET schedule_message_id=? WHERE match_id=?", (str(confirmation.id), self.mid))
                await db.commit()
        await send_map_vote(i.channel,self.mid)
    @discord.ui.button(label="\u274c Decline",style=discord.ButtonStyle.red)
    async def decline(self,i,btn):
        if not await self._check(i):return
        gid=str(i.guild_id)
        team=await captain_team(gid,self.actor_id)
        await record_sched_event(gid,self.mid,team or "?","decline")
        self.done=True
        for c in self.children:c.disabled=True
        await i.response.edit_message(content="\u274c Schedule proposal declined.",view=self)
    @discord.ui.button(label="\U0001f504 Counter",style=discord.ButtonStyle.blurple)
    async def counter(self,i,btn):
        if not await self._check(i):return
        await CounterPickerLauncher(self).open(i)

class ResultConfirmView(discord.ui.View):
    def __init__(self,ocid,rep_key,rep_disp,opp_key,opp_disp,score,won,winner,delta,gid,cfg,thread_id=""):
        super().__init__(timeout=86400)
        self.ocid=ocid;self.rep_key=rep_key;self.rep_disp=rep_disp
        self.opp_key=opp_key;self.opp_disp=opp_disp
        self.score=self._norm_score(score);self.won=won;self.winner=winner;self.delta=delta
        self.gid=gid;self.cfg=cfg;self.thread_id=thread_id
    @staticmethod
    def _norm_score(score):
        try:
            a,b=[int(x) for x in str(score).split("-")]
            return f"{a}-{b}"
        except Exception:
            return str(score)
    @discord.ui.button(label="\u2705 Confirm Result",style=discord.ButtonStyle.green)
    async def confirm(self,i,btn):
        opponent_team=await team_get(self.gid,self.opp_key)
        if str(i.user.id) not in team_captain_ids(opponent_team):
            await i.response.send_message("Other captain only.",ephemeral=True);return
        score=self.score;winner=self.winner;delta=self.delta;gid=self.gid
        rep=self.rep_key;opp=self.opp_key
        rep_disp=self.rep_disp;opp_disp=self.opp_disp
        w_key=rep if self.won else opp
        l_key=opp if self.won else rep
        l_disp=opp_disp if self.won else rep_disp

        # Read and claim the match first.  This prevents a double-click or stale
        # confirmation from awarding a result twice.
        async with aiosqlite.connect(DB) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT * FROM matches WHERE guild_id=? AND thread_id=?",(gid,self.thread_id)) as cur:
                match=await cur.fetchone()
            if not match:
                await i.response.send_message("Match not found.",ephemeral=True);return
            if match["winner"] is not None:
                await i.response.send_message("This match has already been recorded.",ephemeral=True);return

            finals_type=int(match["is_finals"] or 0)
            if finals_type in (1,3):
                try:
                    sa,sb=[int(x) for x in score.split("-")]
                except Exception:
                    await i.response.send_message("\u274c Finals results must be a BO5 score such as **3-0**, **3-1**, or **3-2**.",ephemeral=True);return
                if sorted((sa,sb)) not in ([0,3],[1,3],[2,3]):
                    await i.response.send_message("\u274c Finals results must be **3-0**, **3-1**, or **3-2**.",ephemeral=True);return
                if winner != (match["team1"] if sa > sb else match["team2"]):
                    await i.response.send_message("\u274c The winner does not match the BO5 score.",ephemeral=True);return

            if finals_type in (1,3):
                # Finals do not change regular-season MMR or W/L.
                await db.execute(
                    "UPDATE matches SET score=?,winner=? WHERE id=? AND winner IS NULL",
                    (score,winner,match["id"])
                )
            else:
                await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,w_key))
                await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,l_key))
                await db.execute("UPDATE matches SET score=?,winner=? WHERE id=? AND winner IS NULL",(score,winner,match["id"]))
            await db.commit()

        c=self.cfg
        finals_type=int(match["is_finals"] or 0)
        if finals_type in (1,3):
            try:
                sa,sb=[int(x) for x in score.split("-")]
            except Exception:
                sa,sb=0,0
            if finals_type==1:
                wp,lp=sa,sb
                if winner==match["team2"]:wp,lp=sb,sa
                msg=(
                    f"\U0001f3c6 **FINALS RESULT** \U0001f3c6\n\n"
                    f"**{winner} {score} {l_disp}**\n\n"
                    f"\U0001f4ca Finals Points Earned:\n"
                    f"\u2022 **{winner}: +{wp}**\n"
                    f"\u2022 **{l_disp}: +{lp}**\n\n"
                    f"Round-robin Finals match recorded."
                )
            else:
                msg=(
                    f"\u2694\ufe0f **FINALS TIEBREAKER RESULT** \u2694\ufe0f\n\n"
                    f"**{winner} {score} {l_disp}**\n\n"
                    f"Tiebreaker recorded. No regular-season MMR/W-L or Finals Points are changed."
                )
            if c and c.get("results_ch"):
                rc=i.guild.get_channel(int(c["results_ch"]))
                if rc:await rc.send(msg)
            await update_finals_bracket(i.guild, gid)
        else:
            if c and c.get("results_ch"):
                rc=i.guild.get_channel(int(c["results_ch"]))
                if rc:
                    w_after=int((await team_get(gid,w_key))["mmr"])
                    l_after=int((await team_get(gid,l_key))["mmr"])
                    w_before=w_after-delta
                    l_before=l_after+delta
                    await rc.send(
                        f"\u26a1 **{winner} {score} {l_disp}**\n"
                        f"Winner: **{winner}**\n"
                        f"**{w_key}**: **{w_before} \u2192 {w_after} MMR** (**+{delta} MMR**)\n"
                        f"**{l_key}**: **{l_before} \u2192 {l_after} MMR** (**-{delta} MMR**)\n"
                        f"Confirmed by both captains."
                    )

        th=None
        if self.thread_id:
            try:th=i.guild.get_thread(int(self.thread_id))
            except:th=None
            if th is None:
                try:th=await i.guild.fetch_channel(int(self.thread_id))
                except:th=None
        if th is None and c and c.get("matches_ch"):
            mc=i.guild.get_channel(int(c["matches_ch"]))
            if mc:
                for t in mc.threads:
                    if rep_disp.lower() in t.name.lower() and opp_disp.lower() in t.name.lower():th=t;break
                if th is None:
                    async for t in mc.archived_threads():
                        if rep_disp.lower() in t.name.lower() and opp_disp.lower() in t.name.lower():th=t;break
        if th:
            # Match result has already been saved above. Permanently delete the
            # Discord thread so it is no longer visible, while keeping the
            # match history in the database/results channel.
            deleted=False
            for attempt in range(3):
                try:
                    await th.delete(
                        reason=f"Match result confirmed: {winner} {score} {l_disp}"
                    )
                    deleted=True
                    break
                except discord.HTTPException as e:
                    log.warning(
                        "match result thread delete attempt %d/3 failed for %s: %s",
                        attempt+1,self.thread_id,e
                    )
                    if attempt<2:
                        await asyncio.sleep(1.0*(attempt+1))
                        try:
                            th=await i.guild.fetch_channel(int(self.thread_id))
                        except Exception:
                            th=None
                        if th is None:
                            deleted=True
                            break
                except Exception as e:
                    log.warning(
                        "match result thread delete attempt %d/3 failed for %s: %s",
                        attempt+1,self.thread_id,e
                    )
                    break
            if not deleted:
                log.warning("Could not permanently delete match result thread %s.",self.thread_id)

        for c2 in self.children:c2.disabled=True
        if finals_type in (1,3):
            await i.response.edit_message(content=f"\U0001f3c6 **Finals result recorded!**\n**{winner} {score} {l_disp}**",view=self)
        else:
            w_t=await team_get(gid,w_key);l_t=await team_get(gid,l_key)
            w_after=int(w_t["mmr"]);l_after=int(l_t["mmr"])
            w_before=w_after-delta;l_before=l_after+delta
            await i.response.edit_message(
                content=(
                    f"\u26a1 **Result confirmed!**\n"
                    f"**{winner} {score} {l_disp}**\n"
                    f"{winner}: **{w_before} \u2192 {w_after} MMR** (**+{delta} MMR**)\n"
                    f"{l_disp}: **{l_before} \u2192 {l_after} MMR** (**-{delta} MMR**)"
                ),
                view=self
            )

    @discord.ui.button(label="\u26a0\ufe0f Dispute",style=discord.ButtonStyle.red)
    async def dispute(self,i,btn):
        if str(i.user.id)!=self.ocid:await i.response.send_message("Other captain only.",ephemeral=True);return
        for c in self.children:c.disabled=True
        await i.response.edit_message(content="\u26a0\ufe0f **Result disputed!** Contact a League Admin to resolve.",view=self)

class ScrimSignupView(discord.ui.View):
    def __init__(self,gid,sid):
        super().__init__(timeout=None);self.gid=gid;self.sid=sid
    @discord.ui.button(label="Sign Up",style=discord.ButtonStyle.green,emoji="\u2795")
    async def signup(self,i,btn):
        gid=self.gid;sid=self.sid;uid=str(i.user.id)
        ban_role=find_role(i.guild,SCRIM_BAN_ROLE)
        if ban_role and ban_role in i.user.roles:
            await i.response.send_message("\u274c You are currently banned from signing up for scrims.",ephemeral=True)
            return
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT 1 FROM scrim_signups_v2 WHERE sid=? AND user_id=?",(sid,uid))as c:
                if await c.fetchone():await i.response.send_message("\u274c You're already signed up! Sign off first if you want to leave.",ephemeral=True);return
        count=await scrim_signup_count(sid)
        max_p=await scrim_max_players(sid)
        async with aiosqlite.connect(DB)as db:
            await db.execute("INSERT OR IGNORE INTO scrim_signups_v2 VALUES(?,?,?,?)",(sid,gid,uid,count));await db.commit()
        active=count<max_p
        status="You're in the scrim!"if active else f"You're up next (#{count-max_p+1} in queue)."
        await i.response.send_message(f"\u2705 Signed up! {status}",ephemeral=True)
        th=await _scrim_thread(sid)
        if th:
            try:await th.add_user(i.user)
            except:pass
        await update_scrim_embed(sid)
        await update_scrim_thread(sid)
    @discord.ui.button(label="Sign Off",style=discord.ButtonStyle.red,emoji="\u274c")
    async def leave(self,i,btn):
        await scrim_leave(i,self.gid,self.sid)

async def _scrim_thread(sid):
    session=await get_scrim_session(sid)
    if not session or not session.get("thread_id"):return None
    g=bot.get_guild(int(session["guild_id"]))
    if not g:return None
    tid=int(session["thread_id"])
    th=g.get_thread(tid)
    if th:return th
    try:return await g.fetch_thread(tid)
    except:return None

async def close_scrim(sid,sess=None):
    # Delete both the mixed-scrim thread and its signup/embed message.
    # Keep the database row until both are confirmed gone, so failures can
    # be retried by scrim_check five minutes later.
    if sess is None:
        sess=await get_scrim_session(sid)
    if not sess:
        return

    g=bot.get_guild(int(sess["guild_id"]))
    if not g:
        log.warning("Mixed scrim cleanup cannot find guild (sid=%s)",sid)
        return

    cleanup_ok=True

    # 1) Delete the thread directly. No archive/lock step.
    thread_id=sess.get("thread_id")
    if thread_id:
        try:
            th=g.get_thread(int(thread_id))
            if th is None:
                th=await g.fetch_channel(int(thread_id))
            await th.delete(reason=f"Scrim {sid} finished")
            log.info("Deleted mixed scrim thread (sid=%s, thread_id=%s)",sid,thread_id)
        except discord.NotFound:
            # Already deleted is the desired final state.
            log.info("Mixed scrim thread already deleted (sid=%s, thread_id=%s)",sid,thread_id)
        except Exception as e:
            cleanup_ok=False
            log.warning("Mixed scrim thread delete failed (sid=%s, thread_id=%s): %s",sid,thread_id,e)

    # 2) Delete the signup embed in #mixed-scrims as well.
    msg_id=sess.get("msg_id")
    msc=None
    for cat in g.categories:
        if cat.name=="Scrims":
            msc=discord.utils.get(cat.text_channels,name="mixed-scrims")
            if msc:
                break
    if msg_id:
        if not msc:
            cleanup_ok=False
            log.warning("Mixed scrim signup message channel not found (sid=%s, msg_id=%s)",sid,msg_id)
        else:
            try:
                msg=await msc.fetch_message(int(msg_id))
                await msg.delete()
                log.info("Deleted mixed scrim signup message (sid=%s, msg_id=%s)",sid,msg_id)
            except discord.NotFound:
                # Already deleted is the desired final state.
                log.info("Mixed scrim signup message already deleted (sid=%s, msg_id=%s)",sid,msg_id)
            except Exception as e:
                cleanup_ok=False
                log.warning("Mixed scrim signup message delete failed (sid=%s, msg_id=%s): %s",sid,msg_id,e)

    if not cleanup_ok:
        retry_at=int(datetime.now(timezone.utc).timestamp())+300
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "UPDATE scrim_v2 SET cleanup_retry_at=? WHERE sid=?",
                (retry_at,sid)
            )
            await db.commit()
        log.warning("Mixed scrim cleanup will retry in 5 minutes (sid=%s)",sid)
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM scrim_signups_v2 WHERE sid=?",(sid,))
        await db.execute("DELETE FROM scrim_v2 WHERE sid=?",(sid,))
        await db.commit()

async def scrim_leave(i,gid,sid):
    uid=str(i.user.id)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT position FROM scrim_signups_v2 WHERE sid=? AND user_id=?",(sid,uid))as c:
            r=await c.fetchone()
    if not r:await i.response.send_message("\u274c You're not signed up to this scrim.",ephemeral=True);return
    pos=r[0]
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM scrim_signups_v2 WHERE sid=? AND user_id=?",(sid,uid));await db.commit()
    max_p=await scrim_max_players(sid)
    was_active=pos<max_p
    await i.response.send_message("\U0001f44b You've signed off.",ephemeral=True)
    th=await _scrim_thread(sid)
    g=bot.get_guild(int(gid))
    if th and g:
        try:
            m=g.get_member(int(uid))
            if m:await th.remove_user(m)
        except:pass
    if was_active:
        nxt=await scrim_next_queue(sid)
        if nxt:
            async with aiosqlite.connect(DB)as db:
                await db.execute("UPDATE scrim_signups_v2 SET position=? WHERE sid=? AND user_id=?",(pos,sid,nxt));await db.commit()
            if th:
                try:await th.send(f"\U0001f4e2 <@{nxt}> a spot opened up - you're in the scrim!")
                except:pass
    await update_scrim_embed(sid)
    await update_scrim_thread(sid)

class ScrimThreadView(discord.ui.View):
    def __init__(self,gid,sid):
        super().__init__(timeout=None);self.gid=gid;self.sid=sid
    @discord.ui.button(label="Sign Off",style=discord.ButtonStyle.red,emoji="\u274c")
    async def signoff(self,i,btn):
        await scrim_leave(i,self.gid,self.sid)
    @discord.ui.button(label="Ask for Queue",style=discord.ButtonStyle.blurple,emoji="\U0001f4e3")
    async def askqueue(self,i,btn):
        sid=self.sid
        members=await scrim_signups_active(sid)
        max_p=await scrim_max_players(sid)
        queue=await scrim_queue_list(sid)
        if len(members)<max_p:
            await i.response.send_message(f"\u2705 There's still a free spot - sign up in the scrim channel!",ephemeral=True);return
        if not queue:
            await i.response.send_message("\u274c Nobody is in the queue right now.",ephemeral=True);return
        nxt=queue[0]
        await i.response.send_message(f"\U0001f4e3 Asked <@{nxt}> to fill a spot.",ephemeral=True)
        try:await i.channel.send(f"\U0001f4e3 <@{nxt}> a spot is open - can you play?")
        except:pass

async def update_scrim_thread(sid):
    session=await get_scrim_session(sid)
    if not session or not session.get("thread_id"):return
    th=await _scrim_thread(sid)
    if not th:return
    members=await scrim_signups_active(sid)
    queue=await scrim_queue_list(sid)
    max_p=await scrim_max_players(sid)
    unix=session.get("unix_time")
    title=session.get("scrim_title")or"Mixed Scrim"
    lines=["**In the scrim:**"]
    lines+=[f"{n+1}. <@{u}>"for n,u in enumerate(members)]if members else["*Nobody signed up yet* "]
    lines.append("")
    lines.append(f"**Up next ({len(queue)}):**")
    lines+=[f"{n+1}. <@{u}>"for n,u in enumerate(queue)]if queue else["*Nobody in queue*"]
    embed=discord.Embed(title=f"\U0001f3ae {title}",description="\n".join(lines),color=0x5865F2)
    if unix:
        embed.add_field(name="\u23f0 Time (GMT)",value=datetime.fromtimestamp(int(unix),timezone.utc).strftime("%H:%M"),inline=True)
        embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
    embed.set_footer(text=f"{len(members)}/{max_p} in \u00b7 Sign Off or Ask for Queue below")
    view=ScrimThreadView(session["guild_id"],sid)
    target=None;dups=[]
    scanned=False
    try:
        async for m in th.history(limit=100,oldest_first=True):
            scanned=True
            if m.author.id==bot.user.id and m.embeds and "In the scrim:"in(m.embeds[0].description or""):
                if target is None:target=m
                else:dups.append(m)
    except Exception as e:log.warning("scrim history: %s",e)
    if not scanned and session.get("thread_msg_id"):
        try:target=await th.fetch_message(int(session["thread_msg_id"]))
        except:target=None
    if target is not None:
        try:
            await target.edit(embed=embed,view=view)
        except Exception as e:
            log.warning("scrim thread edit: %s",e)
            try:await target.edit(embed=embed)
            except Exception as e2:log.warning("scrim thread edit2: %s",e2)
        if str(target.id)!=str(session.get("thread_msg_id")):
            async with aiosqlite.connect(DB)as db:
                await db.execute("UPDATE scrim_v2 SET thread_msg_id=? WHERE sid=?",(str(target.id),sid));await db.commit()
        for d in dups:
            try:await d.delete()
            except:pass
        return
    try:
        m=await th.send(embed=embed,view=view)
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE scrim_v2 SET thread_msg_id=? WHERE sid=?",(str(m.id),sid));await db.commit()
        try:await m.pin()
        except:pass
    except Exception as e:log.warning("scrim thread msg: %s",e)

async def update_scrim_embed(sid):
    session=await get_scrim_session(sid)
    if not session or not session.get("msg_id"):return
    g=bot.get_guild(int(session["guild_id"]))
    if not g:return
    members=await scrim_signups_active(sid)
    total=await scrim_signup_count(sid)
    max_p=await scrim_max_players(sid)
    desc="\n".join(f"{n+1}. <@{uid}>"for n,uid in enumerate(members))if members else"*No signups yet. Be the first!*"
    q_count=max(0,total-max_p)
    title=session.get("scrim_title","Mixed Scrim")or"Mixed Scrim"
    unix=session.get("unix_time")
    embed=discord.Embed(title=title,description=desc,color=0x5865F2)
    embed.add_field(name="Signed Up",value=f"{len(members)}/{max_p} active"+(f" (+{q_count} in queue)"if q_count else""),inline=True)
    if unix:
        gmt=datetime.fromtimestamp(int(unix),timezone.utc).strftime("%H:%M")
        embed.add_field(name="\u23f0 Time (GMT)",value=gmt,inline=True)
        embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
    embed.set_footer(text="Click Sign Up to join")
    try:
        msc=None
        for cat in g.categories:
            if cat.name=="Scrims":
                for ch in cat.text_channels:
                    if ch.name=="mixed-scrims":msc=ch;break
        if msc and session.get("msg_id"):
            msg=await msc.fetch_message(int(session["msg_id"]))
            await msg.edit(embed=embed)
    except Exception as e:log.warning("update_scrim_embed failed: %s",e)

async def create_scrim_thread(gid,date):
    # Not used — scrim threads removed
    pass


# ===== Gen Matches =====
def _build_week_pairs(names, history, week, seed):
    """Build exactly two matches per team for the generated week.

    For 3+ teams this creates a cycle: each team appears in exactly two matches
    and no pairing appears twice in the same generated week. With only two teams,
    playing the same opponent twice is unavoidable if both teams must play twice.
    Historical pairings are used to reduce recent rematches.
    """
    names = list(names)
    n = len(names)
    if n < 2:
        return []
    if n == 2:
        return [(names[0], names[1]), (names[0], names[1])]

    # History is keyed by normalized pair and stores every week in which it played.
    def pair_cost(a, b):
        weeks = history.get(_pairkey(a, b), [])
        if not weeks:
            return 0
        last_week = max(weeks)
        gap = max(0, int(week) - int(last_week))
        # Strongly avoid a week-to-week rematch. If all opponents have been faced,
        # prefer the least-recently-played and least-frequent pairings.
        recent_penalty = 18000 if gap == 1 else (7000 if gap == 2 else (2500 if gap == 3 else 0))
        return 10000 + (len(weeks) * 750) + recent_penalty

    def cycle_score(order):
        return sum(pair_cost(order[i], order[(i + 1) % n]) for i in range(n))

    # Seeded randomness makes the same input produce the same proposed schedule,
    # while allowing different weeks/team lists to explore different cycles.
    rng = random.Random(str(seed))
    best_order = names[:]
    best_score = float("inf")
    iterations = min(2500, max(500, n * 100))
    restarts = 12

    for _ in range(restarts):
        order = names[:]
        rng.shuffle(order)
        score = cycle_score(order)
        if score < best_score:
            best_order, best_score = order[:], score

        # Simulated annealing explores alternative cycles, including when every
        # possible opponent has already been faced at least once.
        temperature = 5000.0
        cooling = 0.995
        for _step in range(iterations):
            i, j = rng.sample(range(n), 2)
            order[i], order[j] = order[j], order[i]
            new_score = cycle_score(order)
            delta = new_score - score
            if delta <= 0 or rng.random() < (2.718281828 ** (-delta / max(temperature, 1.0))):
                score = new_score
                if score < best_score:
                    best_order, best_score = order[:], score
            else:
                order[i], order[j] = order[j], order[i]
            temperature *= cooling

    return [(best_order[i], best_order[(i + 1) % n]) for i in range(n)]


async def gen_matches(guild,c,force=False,target_week=None):
    gid=str(guild.id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT weeks_done FROM season WHERE guild_id=?",(gid,))as cur:
            row=await cur.fetchone()
    if not row:return
    # An explicit target_week generates that week without changing season progress.
    # Normal/automatic generation still targets the next week.
    week=int(target_week) if target_week is not None else int(row["weeks_done"])+1
    if week<1:return
    if week>SEASON_WEEKS and not force:return
    ts=await teams_all(gid)
    eligible=[t for t in ts if 2 <= len(t.get("members",[])) <= MAX_TEAM]
    names=[t["display"]for t in eligible]
    n=len(names)
    if n<2:return

    # Read the full non-finals history for this guild. Pair frequency and the
    # latest week played are both used so repeat opponents are spread apart.
    history={}
    async with aiosqlite.connect(DB)as db:
        async with db.execute(
            "SELECT team1,team2,week FROM matches WHERE guild_id=? AND COALESCE(is_finals,0)=0",
            (gid,)
        )as cur:
            for a,b,played_week in await cur.fetchall():
                if not a or not b:
                    continue
                try:
                    played_week=int(played_week or 0)
                except (TypeError,ValueError):
                    played_week=0
                history.setdefault(_pairkey(a,b),[]).append(played_week)

    names_sorted=sorted(names,key=str.lower)
    all_pairs=_build_week_pairs(
        names_sorted,
        history,
        week,
        seed=f"{gid}:{week}:{'|'.join(names_sorted)}"
    )
    if not all_pairs:return
    match_ids=[]
    eligible_by_display={str(t["display"]).casefold():t for t in eligible}
    async with aiosqlite.connect(DB)as db:
        for a,b in all_pairs:
            mid=str(uuid.uuid4())[:8]
            await db.execute(
                "INSERT INTO matches (id,guild_id,week,team1,team2,created_at) VALUES(?,?,?,?,?,?)",
                (mid,gid,week,a,b,datetime.now(timezone.utc).isoformat())
            )
            ta=eligible_by_display.get(str(a).casefold());tb=eligible_by_display.get(str(b).casefold())
            if ta and tb and int(ta.get("mmr",DEFAULT_MMR)) != int(tb.get("mmr",DEFAULT_MMR)):
                underdog,opponent=(ta,tb) if int(ta.get("mmr",DEFAULT_MMR)) < int(tb.get("mmr",DEFAULT_MMR)) else (tb,ta)
                if underdog.get("captain_id"):
                    await db.execute(
                        "INSERT OR IGNORE INTO underdog_bans(match_id,guild_id,underdog_team,opponent_team,underdog_mmr,opponent_mmr,captain_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (mid,gid,underdog["display"],opponent["display"],int(underdog["mmr"]),int(opponent["mmr"]),str(underdog["captain_id"]),datetime.now(timezone.utc).isoformat())
                    )
            match_ids.append((mid,a,b))
        if not force and target_week is None:
            now_cycle=datetime.now(timezone.utc)
            cycle_date=(now_cycle.date()-timedelta(days=(now_cycle.weekday()+1)%7)).isoformat()
            await db.execute(
                "UPDATE season SET weeks_done=?,last_generation_cycle=? WHERE guild_id=?",
                (week,cycle_date,gid)
            )
        await db.commit()
    ch=guild.get_channel(int(c["matches_ch"]))
    if ch and isinstance(ch,discord.TextChannel):
        for mid,a,b in match_ids:
            try:
                th=await ch.create_thread(name=f"{a} vs {b} - W{week}",type=discord.ChannelType.private_thread,auto_archive_duration=10080)
                async with aiosqlite.connect(DB)as db2:await db2.execute("UPDATE matches SET thread_id=? WHERE id=?",(str(th.id),mid));await db2.commit()
                t1=await team_get(gid,a);t2=await team_get(gid,b)
                added=set()
                for t in[t1,t2]:
                    if not t:continue
                    for uid in t["members"]:
                        m=guild.get_member(int(uid))
                        if m and uid not in added:
                            if await add_thread_member_safely(th,m):
                                added.add(uid)
                for m in guild.members:
                    if is_admin(m)and str(m.id)not in added:
                        if await add_thread_member_safely(th,m):
                            added.add(str(m.id))
                await create_underdog_announcement(guild,th,mid,a,b)
                await th.send(f"⚡ **{a} vs {b} - Week {week}**\nEveryone's here! **Each team can choose any 2 of its registered players to compete in this 2v2 match.** All registered team members are equal, so there is no designated reserve. Captains, use `/schedule` to choose a match day and time. Once both captains confirm, the map vote will open.")
            except Exception as e:log.warning("Thread: %s",e)
        lines=[f"⚡ **Week {week} Matches**",""]+[f"• **{a}** vs **{b}**"for _,a,b in match_ids]+["","Check threads!"]
        await ch.send("\n".join(lines))
        c2=await cfg_get(gid)
        if target_week is None and c2 and c2.get("announcements_ch"):
            ann=guild.get_channel(int(c2["announcements_ch"]))
            if ann:
                weeks_left=SEASON_WEEKS-week
                ts_all=await teams_all(gid)
                total_matches=sum(t["wins"]+t["losses"]for t in ts_all)//2
                finals_note="🏆 Finals next week! Top 4 teams advance."if weeks_left==0 else f"Top 4 by rank advance to finals after Week {SEASON_WEEKS}."
                await ann.send(f"📊 **Season Update  -  Week {week}/{SEASON_WEEKS}**\n\nWeeks remaining: **{weeks_left}**\nMatches played: **{total_matches}**\nTeams in league: **{len(ts_all)}**\n\n{finals_note}")
    return len(match_ids)

# ===== /guide =====
@bot.tree.command(name="guide",description="Post the league guide (League Admin only)")
async def guide_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    embed1=discord.Embed(title="\U0001f4d6 EGL - Elite Goon League",description="Welcome to the EGL, our own 2v2 competitive league for Elements Divided.",color=0x5865F2)
    embed1.add_field(name="\U0001f3c6 League Format",value="- Teams can have **up to 3 players**. All registered players are equal members of the team.\n- Every match is **2v2**. For each match, the team chooses which 2 of its registered players will compete.\n- There is no designated reserve or substitute within the team.\n- Season lasts **6 weeks**\n- Every team plays **2 matches per week** (12 regular-season matches total)\n- Matches are generated every **Sunday 10pm GMT**\n- Opponents are rotated to reduce repeat matchups\n- Captains vote on map (coin flip on tie)\n- **Top 4** advance to Finals, then Grand Final!",inline=False)
    embed1.add_field(name="\U0001f4ca Ranks",value="\U0001f7e4 Awakened\n\u26aa Adept\n\U0001f7e1 **Elementalist** *(start)*\n\U0001f7e2 Master\n\U0001f535 Ascendant\n\U0001f451 Avatar",inline=True)
    embed1.add_field(name="MMR System",value="**Who you play matters:**\nWin vs a stronger team = **bigger gain**\nWin vs a weaker team = **smaller gain**\nLose vs a stronger team = **smaller loss**\nLose vs a weaker team = **bigger loss**\n\n**How you play matters too:**\nSweep (3-0) = **bigger reward**\nClose game (3-2) = **smaller reward**",inline=True)
    embed1.add_field(name="\u23f1\ufe0f Rules",value="- **24h cooldown** after leaving/disbanding\n- Returning players inherit **old team's MMR**",inline=False)
    await i.response.send_message(embed=embed1)
    flow_embed=discord.Embed(title="\U0001f451 Captain's Role & How a Match Works",color=0xe67e22)
    flow_embed.add_field(name="What is the Captain?",value="The **captain** is the team leader. They:\n- Created the team\n- Invite & kick players\n- Vote on the map\n- Schedule matches\n- Report results\n\n*Only the captain can do these things!*",inline=False)
    flow_embed.add_field(name="\U0001f504 Match Flow - Step by Step",value="**1.** Matches are generated every **Sunday 10pm GMT**\n**2.** A private thread appears in #matches for your team\n**3.** Both captains **vote on the map** in that thread\n**4.** Captain picks a time with `/schedule` - other captain confirms\n**5.** Play the match! (BO5, first to **3 wins**)\n**6.** Winning captain reports with `/match result` - other captain confirms\n**7.** Result posts to #results, MMR updates, done!\n",inline=False)
    flow_embed.set_footer(text="If you're ever confused, ask a League Admin!")
    await i.followup.send(embed=flow_embed)
    embed2=discord.Embed(title="\U0001f4cb Commands",color=0x5865F2)
    embed2.add_field(name="\u2694\ufe0f Team",value="`/team create` - Start a team (you become captain)\n`/team invite @player` - Invite someone\n`/team kick @player` - Remove someone\n`/team leave` - Leave your team\n`/disband` - Delete your team\n`/captain swap @player` - Transfer captain\n`/teaminfo name:Name` - Look up any team",inline=False)
    embed2.add_field(name="\U0001f4c5 Match *(captains only, in match thread)*",value="`/schedule 05 Aug 19:00` - Set match time (other captain confirms)\n`/match result opponent:Name score:2-1` - Report result",inline=False)
    embed2.add_field(name="\U0001f4ca Stats",value="`/teaminfo name:Name` - Rank, MMR, progress bar\n`/leaderboard` - Full leaderboard (live)\n`/league status` - Season progress + top 4",inline=False)
    embed2.set_footer(text="Questions? Ask a League Admin. Good luck, Goons!")
    await i.followup.send(embed=embed2)

# ===== /matchrules =====
@bot.tree.command(name="matchrules",description="Post the match rules in this channel (League Admin)")
async def matchrules_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    rules_embed=discord.Embed(title="\U0001f4dc Match Rules",color=0x9b59b6)
    rules_embed.add_field(name="\U0001f525 Element Rules",value="Play **anything you want**, as long as there are no copies.\n- Example: You can't play 2x fire, but you can play fire and explosion.\n- **No duplicate ultimates** on a team (2 players = 2 different ults)",inline=False)
    rules_embed.add_field(name="\U0001f3ae Game Rules",value="- **Control point:** OFF\n- **Damage zone:** ON\n- **Game mode:** Rounds\n- **Rounds to win:** 3 (BO5)\n- **Stage hazards:** ON\n- **Off map damage:** ON\n- **Multipliers:** All x1",inline=False)
    rules_embed.add_field(name="\U0001f3a8 Team Shader Rule",value="All players on the same team must use the same team shader during the match. Captains are responsible for making sure their team follows this rule.",inline=False)
    rules_embed.add_field(name="\U0001f30d Server Region",value="- All matches are played on **EU servers by default**.\n- **US servers are allowed only when every player in the match is from the US.**\n- If there is **even one EU player**, the match must be played on EU servers.",inline=False)
    rules_embed.add_field(name="\U0001f5fa\ufe0f Map",value="Voted on after the match time is set.",inline=False)
    rules_embed.add_field(name="\u26a1 Toggles",value="- Powerups: OFF\n- Ultimates: ON\n- Techniques: ON\n- Blocking: ON\n- Perfect blocking: ON\n- Dashing: ON",inline=False)
    rules_embed.add_field(name="\U0001f465 Match Size",value="- All matches must be played as **2v2**. Each team can have up to 3 registered players and may choose any 2 of them to compete in each match.",inline=False)
    rules_embed.add_field(name="\u23f0 No-Shows & Forfeits",value="- If some of your team **doesn't show up within 15 minutes**, you can either **play with the players available** (e.g. 1v2) or **forfeit** (3-0 loss).\n- If a team **can't play** that week, they forfeit the match **3-0**.\n- If you **can't agree on a time**, contact the League Admins.\n- Captains are responsible for the rules - a match played with invalid rules **must be replayed**.",inline=False)
    rules_embed.add_field(name="\u2696\ufe0f Scheduling Disputes",value="- If teams cannot agree on a match time, an **impartial League Admin or assigned neutral person** will review the scheduling chat.\n- They will decide whether either team failed to make a reasonable effort, whether both teams tried their best, and who should forfeit if necessary.\n- Decisions will be based on the chat history and each team's effort to find a suitable time.",inline=False)
    rules_embed.add_field(name="\ud83d\udeab Team Roster",value="- Only players on the registered team roster may participate.\n- Teams with 3 registered players can choose freely which 2 players participate in each match.\n- All registered players are equal team members. There is no designated reserve or substitute.",inline=False)
    rules_embed.add_field(name="\U0001f91d Sportsmanship",value="- We trust all players and teams to act with good sportsmanship, respect, and honesty.\n- Treat opponents fairly, respect agreed match arrangements, and avoid toxic behaviour, harassment, or deliberate rule abuse.\n- Disagreements should be handled respectfully. If an issue cannot be resolved between both teams, contact a League Admin.\n- Everyone is expected to play in good faith and help keep the league competitive, fair, and enjoyable for everyone.",inline=False)
    await i.response.send_message(embed=rules_embed)

# ===== /scrimguide =====
@bot.tree.command(name="scrimguide",description="Post the scrim guide (League Admin)")
async def scrimguide_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    embed=discord.Embed(title="\U0001f3ae Scrims Guide",description="Casual practice matches - no league points, just fun.",color=0xe67e22)
    embed.add_field(name="\U0001f4dd How it works",value="`/create mixedscrim time:20:00` - **anyone can create one**\n- **2v2 = 4 spots**\n- Click **Sign Up** in #mixed-scrims to join\n- Once 4 players are in, anyone else goes on the **Up Next** queue\n- If someone drops out, the **first person in the queue is pulled in** automatically",inline=False)
    embed.add_field(name="\u23f1\ufe0f Before you sign up",value="Scrims usually last **around an hour**.\nMake sure you have the **time and the charge** before you sign up - don't leave your team short.",inline=False)
    embed.add_field(name="\U0001f6aa If you drop out",value="You **have to get a new player in** to replace you.\nDon't just sign off and leave - the next person in the **Up Next** queue gets pulled in automatically, but it's on you to make sure the spot is filled.",inline=False)
    embed.add_field(name="\U0001f9f5 The scrim thread",value="Every scrim gets its own thread - everyone signed up (and everyone in the queue) is added automatically.\n\nIn the thread you'll see the live list, plus:\n- **Sign Off** - drop out\n- **Ask for Queue** - ping the next player in line to fill a spot",inline=False)
    embed.add_field(name="\u23f0 Reminder",value="**5 minutes before start**, everyone in the scrim gets pinged in the thread.\nGet a lobby ready and drop the code in the chat!",inline=False)
    await i.response.send_message(embed=embed)

# ===== Command groups and slash commands =====
setup=app_commands.Group(name="setup",description="Bot setup")
@setup.command(name="run",description="Create all league channels (League Admin only)")
async def setup_run(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT guild_id FROM setup_data WHERE guild_id=?",(gid,))as cur:
            if await cur.fetchone():await i.followup.send("\u274c Already set up. Use `/setup reset` first.",ephemeral=True);return
    ev=i.guild.default_role;ar=discord.utils.get(i.guild.roles,name=ADMIN_ROLE)
    goon=discord.utils.get(i.guild.roles,name="Regular Goon")
    bot_override=discord.PermissionOverwrite(send_messages=True,read_messages=True,send_messages_in_threads=True,create_private_threads=True)
    everyone_deny=discord.PermissionOverwrite(read_messages=False,send_messages=False,add_reactions=False)
    def _base():
        d={ev:everyone_deny,i.guild.me:bot_override}
        if ar:d[ar]=discord.PermissionOverwrite(send_messages=True,read_messages=True)
        return d
    admin_only=_base()
    if goon:admin_only[goon]=discord.PermissionOverwrite(read_messages=True,send_messages=False)
    react_only=_base()
    if goon:react_only[goon]=discord.PermissionOverwrite(read_messages=True,send_messages=False,add_reactions=True)
    open_perm=_base()
    if goon:open_perm[goon]=discord.PermissionOverwrite(read_messages=True,send_messages=True)
    # #recruiting is a forum: Regular Goons can create LFG/LFT posts and reply.
    forum_perm=_base()
    if ar:forum_perm[ar]=discord.PermissionOverwrite(read_messages=True,send_messages=True,create_public_threads=True,send_messages_in_threads=True)
    forum_perm[i.guild.me]=discord.PermissionOverwrite(read_messages=True,send_messages=True,create_public_threads=True,send_messages_in_threads=True)
    if goon:forum_perm[goon]=discord.PermissionOverwrite(read_messages=True,send_messages=False,create_public_threads=True,send_messages_in_threads=True)
    # #teams is a read-only team directory for Regular Goons; private team threads remain usable.
    teams_view_perm=_base()
    if goon:teams_view_perm[goon]=discord.PermissionOverwrite(read_messages=True,send_messages=False,send_messages_in_threads=True,create_private_threads=False,create_public_threads=False)
    cat_perm=_base()
    if goon:cat_perm[goon]=discord.PermissionOverwrite(read_messages=True)
    try:
        lcat=await i.guild.create_category("EGL",overwrites=cat_perm)
        ann=await i.guild.create_text_channel("announcements",category=lcat,overwrites=admin_only)
        guide_ch=await i.guild.create_text_channel("guide",category=lcat,overwrites=admin_only)
        admin_cmd_ch=await i.guild.create_text_channel("admin-commands",category=lcat,overwrites=admin_only)
        gen=await i.guild.create_text_channel("general",category=lcat,overwrites=open_perm)
        tch=await i.guild.create_text_channel("teams",category=lcat,overwrites=teams_view_perm)
        rec=await i.guild.create_forum("recruiting",category=lcat,overwrites=forum_perm)
        mcat=await i.guild.create_category("Matches",overwrites=cat_perm)
        mat=await i.guild.create_text_channel("matches",category=mcat,overwrites=react_only)
        mr=await i.guild.create_text_channel("match-rules",category=mcat,overwrites=admin_only)
        res=await i.guild.create_text_channel("results",category=mcat,overwrites=react_only)
        lb=await i.guild.create_text_channel("leaderboard",category=mcat,overwrites=admin_only)
        ov=await i.guild.create_text_channel("overseer",category=mcat,overwrites=admin_only)
    except Exception as e:await i.followup.send(f"\u274c Failed: {e}");return
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO setup_data VALUES(?,?,?,?,?,?,?,?,?,?)",(gid,"EGL",str(lcat.id),str(mcat.id),str(ann.id),str(gen.id),str(tch.id),"",str(mat.id),str(res.id)))
        await db.execute("INSERT OR REPLACE INTO team_commands_state VALUES(?,?)",(gid,str(gen.id)))
        await db.commit()
    msg="\u2694\ufe0f **Elite Goon League is live.**\nAll channels and permissions have been configured.\n\n"
    msg+=f"{ann.mention} \u2014 Announcements\n"
    msg+=f"{guide_ch.mention} \u2014 Player guide\n"
    msg+=f"{admin_cmd_ch.mention} \u2014 Admin commands\n"
    msg+=f"{gen.mention} \u2014 General chat\n"
    msg+=f"{tch.mention} \u2014 Team directory (read-only) + private team threads\n"
    msg+=f"{rec.mention} \u2014 Recruiting forum (LFG / LFT posts; Regular Goons can create posts)\n"
    msg+=f"{mat.mention} \u2014 Match schedule\n"
    msg+=f"{mr.mention} \u2014 Match rules\n"
    msg+=f"{res.mention} \u2014 Results\n"
    msg+=f"{lb.mention} \u2014 Leaderboard\n"
    msg+=f"{ov.mention} \u2014 Overseer leaderboard\n\n"
    await i.followup.send(msg)
@setup.command(name="reset",description="Delete all bot channels and reset")
async def setup_reset(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM setup_data WHERE guild_id=?",(gid,))as cur:row=await cur.fetchone()
    if not row:await i.followup.send("\u274c Not set up yet.",ephemeral=True);return
    row=dict(row)
    for cat_id in[row["league_category_id"],row["matches_category_id"]]:
        try:
            cat=await i.guild.fetch_channel(int(cat_id))
            for ch in list(cat.channels):
                try:await ch.delete()
                except:pass
            await cat.delete()
        except:pass
    # Also delete Scrims category
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            try:
                for ch in list(cat.channels):
                    try:await ch.delete()
                    except:pass
                await cat.delete()
            except:pass
            break
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM setup_data WHERE guild_id=?",(gid,))
        await db.execute("DELETE FROM team_commands_state WHERE guild_id=?",(gid,))
        await db.execute("DELETE FROM roster_state WHERE guild_id=?",(gid,))
        await db.commit()
    await i.followup.send("\u2705 Reset complete.")

# ===== /scrimbot =====
scrimbot_grp=app_commands.Group(name="scrimbot",description="Scrim management (League Admin)")
@scrimbot_grp.command(name="setup",description="Create the Scrims category + channels")
async def scrimbot_setup(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    await i.response.defer()
    ev=i.guild.default_role;ar=discord.utils.get(i.guild.roles,name=ADMIN_ROLE)
    bot_ow=discord.PermissionOverwrite(send_messages=True,read_messages=True)
    ao={ev:discord.PermissionOverwrite(send_messages=False,read_messages=True),i.guild.me:bot_ow}
    if ar:ao[ar]=discord.PermissionOverwrite(send_messages=True,read_messages=True)
    try:
        scat=await i.guild.create_category("Scrims")
        msc=await i.guild.create_text_channel("mixed-scrims",category=scat)
        sgch=await i.guild.create_text_channel("scrimguide",category=scat,overwrites=ao)
    except Exception as e:await i.followup.send(f"\u274c Failed (may already exist): {e}");return
    await i.followup.send(f"\U0001f3ae **Scrims activated!**\n{msc.mention} - Mixed scrims\n{sgch.mention} - Scrim guide (read-only)\n\nUse `/create mixedscrim` to get started.")
@scrimbot_grp.command(name="reset",description="Delete the Scrims category + channels")
async def scrimbot_reset(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    await i.response.defer()
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            try:
                for ch in list(cat.channels):
                    try:await ch.delete()
                    except:pass
                await cat.delete()
            except:pass
            await i.followup.send("\u2705 Scrims removed.");return
    await i.followup.send("\u274c No Scrims category found.")




@bot.tree.command(name="setchannel",description="Set #teams for team threads (League Admin only)")
@app_commands.describe(channel="The #teams channel")
async def setchannel(i,channel:discord.TextChannel):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT OR REPLACE INTO guild_settings VALUES(?,?)",(str(i.guild_id),str(channel.id)));await db.commit()
    await i.response.send_message(f"\u2705 Team threads: {channel.mention}.")

overseer=app_commands.Group(name="overseer",description="Overseer statistics")

@overseer.command(name="leaderboard",description="Show the Overseer spectating leaderboard")
async def overseer_leaderboard(i):
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT overseer_id,COUNT(*) FROM matches "
            "WHERE guild_id=? AND overseer_id IS NOT NULL AND overseer_id!='' "
            "GROUP BY overseer_id ORDER BY COUNT(*) DESC",
            (gid,)
        ) as cur:
            rows=await cur.fetchall()

    if not rows:
        await i.response.send_message(
            "👁️ **Overseer Spectating Leaderboard**\n\nNo matches have been claimed by an Overseer yet."
        )
        return

    lines=["👁️ **Overseer Spectating Leaderboard**",""]
    for rank,(uid,count) in enumerate(rows,1):
        member=i.guild.get_member(int(uid))
        name=member.mention if member else f"<@{uid}>"
        lines.append(f"**{rank}.** {name} — **{count}** match{'es' if count != 1 else ''}")

    await i.response.send_message("\n".join(lines))

@overseer.command(name="rules",description="Show the Overseer spectating and recording rules")
async def overseer_rules(i):
    embed=discord.Embed(
        title="👁️ Overseer Rules",
        description=(
            "As soon as a match is scheduled, you guys get pinged and you can choose to Spectate.\n\n"
            "- Go to the match thread and click **👁️ Spectate Match**.\n"
            "- As soon as you join, set everything up immediately. **Do NOT make the teams wait for you.**\n"
            "- **Do NOT talk to the teams.** Let them set up and start their match without interruption.\n"
            "- Keep VC + mic **OFF** at all times. You should not hear them, and they should not hear you.\n"
            "- Once everything is ready, join as a spectator and start recording.\n"
            "- Record the entire match from start to finish.\n"
            "- Do not communicate, give callouts, or interfere with the teams in any way.\n"
            "- Keep your camera/head movement as smooth as possible. Avoid sharp joystick turns if possible.\n"
            "- Send the recording after the match."
        ),
        color=0x5865F2
    )
    embed.add_field(
        name="Recording",
        value=(
            "Make sure the footage is clear and watchable. Very shaky/jagged footage may not be used. "
            "Show the leaderboard when the game starts and when it ends, so everyone can see the stats."
        ),
        inline=False
    )
    embed.add_field(
        name="Thumbnail",
        value="Thumbnails are optional. Ask the team if they want to provide one. If not, that's completely fine.",
        inline=False
    )
    embed.add_field(
        name="Your job",
        value="Set up quickly → stay muted → let the teams play → record the full match → send the video.",
        inline=False
    )
    await i.response.send_message(embed=embed)

league=app_commands.Group(name="league",description="League management")
@league.command(name="create",description="Create a season")
@app_commands.describe(name="Season name")
async def league_create(i,name:str):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    if await cfg_get(gid):await i.response.send_message("\u274c League already exists. Use `/league delete` first.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM setup_data WHERE guild_id=?",(gid,))as cur:sd=await cur.fetchone()
    if not sd:await i.response.send_message("\u274c Run `/setup run` first.",ephemeral=True);return
    sd=dict(sd)
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO config VALUES(?,?,?,?,?,?,?,?,?,?)",(gid,name,str(i.user.id),sd["announcements_ch"],sd["matches_ch"],sd["results_ch"],sd["general_ch"],sd["fa_ch"],sd["teams_ch"],datetime.now(timezone.utc).isoformat()))
        await db.execute("INSERT OR IGNORE INTO season(guild_id,weeks_done,finals_generated) VALUES(?,0,0)",(gid,));await db.commit()
    end_date=datetime.now(timezone.utc)+timedelta(weeks=SEASON_WEEKS)
    now=datetime.now(timezone.utc)
    days_until_sunday=(6-now.weekday())%7
    next_sunday=now.replace(hour=22,minute=0,second=0)+timedelta(days=days_until_sunday if days_until_sunday>0 else 0)
    if days_until_sunday==0 and now.hour>=22:next_sunday+=timedelta(days=7)
    sun_unix=int(next_sunday.replace(tzinfo=timezone.utc).timestamp())
    if sd.get("announcements_ch"):
        ann=i.guild.get_channel(int(sd["announcements_ch"]))
        if ann:
            await ann.send(f"\U0001f3c6 **{name}** has begun!\n\n\u2022 **{SEASON_WEEKS}-week season**  -  every team plays **2 matches per week** (12 regular-season matches total); opponents are rotated to reduce repeats\n\u2022 Matches generated **Sunday at 10pm GMT** (automatic recovery runs until **Monday 4am GMT** if the bot was offline) (<t:{sun_unix}:t> your time)\n\u2022 End of regular season: **{end_date.strftime('%d %b %Y')}**\n\u2022 Top 4 advance to Finals, then Grand Final\n\nGood luck, Goons!")
    await i.response.send_message(f"\u2694\ufe0f **{name}** season started!")
@league.command(name="delete",description="Delete league")
async def league_delete(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM underdog_bans WHERE guild_id=?",(gid,))
        for tbl in("config","teams","members","fa","matches","season","finals_state"):await db.execute(f"DELETE FROM {tbl} WHERE guild_id=?",(gid,))
        await db.commit()
    await i.response.send_message(f"\U0001f5d1\ufe0f **{c['name']}** deleted.")
@league.command(name="end",description="End Finals and clean up Finals Discord infrastructure")
async def league_end(i):
    if not await need_admin(i):return
    gid=str(i.guild_id)

    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT bracket_thread_id,host_thread_id,stage_channel_id FROM finals_state WHERE guild_id=?",
            (gid,)
        ) as cur:
            state=await cur.fetchone()
        async with db.execute(
            "SELECT thread_id FROM matches WHERE guild_id=? AND is_finals IN (1,2,3) AND thread_id IS NOT NULL",
            (gid,)
        ) as cur:
            match_rows=await cur.fetchall()

    if not state and not match_rows:
        await i.response.send_message("❌ No active Finals infrastructure was found.",ephemeral=True)
        return

    deleted=0
    failed=0
    ids=[]
    if state:
        ids.extend([state["bracket_thread_id"],state["host_thread_id"],state["stage_channel_id"]])
    ids.extend(row["thread_id"] for row in match_rows)

    seen=set()
    for raw_id in ids:
        if not raw_id:continue
        sid=str(raw_id)
        if sid in seen:continue
        seen.add(sid)

        obj=i.guild.get_channel(int(sid))
        if obj is None:
            try:obj=await i.guild.fetch_channel(int(sid))
            except Exception:obj=None
        if obj is None:continue

        try:
            await obj.delete(reason="Finals ended by League Admin")
            deleted+=1
        except Exception as e:
            failed+=1
            log.warning("Finals cleanup failed for %s: %s",sid,e)

    async with aiosqlite.connect(DB) as db:
        # Preserve historical Finals match/result records.
        # Only clear Discord thread references and active Finals state.
        await db.execute(
            "UPDATE matches SET thread_id=NULL WHERE guild_id=? AND is_finals IN (1,2,3)",
            (gid,)
        )
        await db.execute("DELETE FROM finals_state WHERE guild_id=?",(gid,))
        await db.commit()

    msg=f"🏁 Finals ended. Cleaned up **{deleted}** Discord item(s)"
    if failed:msg+=f"; **{failed}** could not be deleted."
    else:msg+="."
    msg+="\nHistorical Finals results and match records were preserved."
    await i.response.send_message(msg)


async def finals_standings(gid):
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT id,team1,team2,score,winner,is_finals FROM matches "
            "WHERE guild_id=? AND is_finals IN (1,3) ORDER BY created_at,id",(gid,)
        ) as cur:
            rows=await cur.fetchall()
    points={};tb_wins={};base_matches=[]
    for r in rows:
        if r["is_finals"]==1:
            points.setdefault(r["team1"],0);points.setdefault(r["team2"],0)
            base_matches.append(r)
            if r["score"] and r["winner"]:
                try:a,b=[int(x) for x in str(r["score"]).split("-")]
                except Exception:continue
                if r["winner"]==r["team1"]:points[r["team1"]]+=a;points[r["team2"]]+=b
                else:points[r["team1"]]+=b;points[r["team2"]]+=a
        elif r["is_finals"]==3 and r["winner"]:
            tb_wins[r["winner"]]=tb_wins.get(r["winner"],0)+1
    ranked=sorted(points,key=lambda t:(points[t],tb_wins.get(t,0),str(t).lower()),reverse=True)
    return points,tb_wins,ranked,base_matches,rows

async def update_finals_bracket(guild,gid):
    points,tb_wins,ranked,base_matches,rows=await finals_standings(gid)
    state=None
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM finals_state WHERE guild_id=?",(gid,)) as cur:state=await cur.fetchone()
    if not state or not state["bracket_thread_id"] or not state["bracket_msg_id"]:return

    lines=[]
    for n,team in enumerate(ranked,1):
        tb=tb_wins.get(team,0)
        extra=f"  | TB wins: **{tb}**" if tb else ""
        lines.append(f"**{n}. {team}** — **{points[team]} pts**{extra}")
    standings="\n".join(lines) or "No results yet."

    match_lines=[]
    for idx,r in enumerate(base_matches,1):
        status=f"**{r['score']}** — {r['winner']}" if r["winner"] else "⏳ Not played"
        match_lines.append(f"**Match {idx}:** {r['team1']} vs {r['team2']} — {status}")
    tb_rows=[r for r in rows if r["is_finals"]==3]
    for r in tb_rows:
        status=f"**{r['score']}** — {r['winner']}" if r["winner"] else "⏳ Pending"
        match_lines.append(f"**Tiebreaker:** {r['team1']} vs {r['team2']} — {status}")

    desc=(
        "Every finalist plays every other finalist once. "
        "Each BO5 awards points equal to rounds won: **3–0 = 3–0, 3–1 = 3–1, 3–2 = 3–2**.\n\n"
        "**FINALS POINTS**\n"+standings+
        "\n\n**ROUND-ROBIN MATCHES**\n"+"\n".join(match_lines)+
        "\n\n**QUALIFICATION**\nThe two highest resolved point totals advance to the Grand Final. "
        "A tie is resolved by an admin-hosted BO5 tiebreaker; the tiebreaker is recorded but does not add Finals Points."
    )
    embed=discord.Embed(title="🏆 FINALS BRACKET",description=desc,colour=discord.Colour.gold())
    embed.set_footer(text="Match order uses regular-season seeding to build toward the biggest matchups.")
    try:
        th=guild.get_thread(int(state["bracket_thread_id"]))
        if th is None:th=await guild.fetch_channel(int(state["bracket_thread_id"]))
        msg=await th.fetch_message(int(state["bracket_msg_id"]))
        await msg.edit(embed=embed)
    except Exception as e:
        log.warning("Finals bracket update: %s",e)

async def _add_thread_members(thread,guild,users):
    added=set()
    for uid in users:
        m=guild.get_member(int(uid))
        if m and str(m.id) not in added:
            try:
                await thread.add_user(m)
                added.add(str(m.id))
            except Exception as e:log.warning("Thread member add: %s",e)
    return added

async def _finals_admin_ids(guild):
    return [str(m.id) for m in guild.members if is_admin(m)]

async def _create_finals_match_thread(guild,c,gid,mid,a,b,label):
    ch=guild.get_channel(int(c["matches_ch"]))
    if not ch or not isinstance(ch,discord.TextChannel):return None
    try:
        await snapshot_underdog_for_match(gid,mid,a,b)
        th=await ch.create_thread(
            name=f"🏆 {a} vs {b} — {label}",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=10080
        )
        t1=await team_get(gid,a);t2=await team_get(gid,b)
        ids=[]
        for t in (t1,t2):
            if t:ids.extend(t.get("members",[]))

        # Private Finals match threads are visible/speakable by both teams and League Admins.
        ids.extend(await _finals_admin_ids(guild))
        await _add_thread_members(th,guild,ids)
        await create_underdog_announcement(guild,th,mid,a,b)
        await th.send(
            f"🏆 **FINALS — {a} vs {b}**\n\n"
            "**2v2 — each team can choose any 2 of its registered players to compete.** "
            "All registered team members are equal, and there is no designated reserve or substitute. "
            "Only players on the registered team roster may participate.\n"
            "BO5 — first team to 3 rounds wins.\n"
            "When the match is complete, either captain uses `/match result` here. "
            "The confirmed result will be posted to #results and this thread will then close."
        )
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE matches SET thread_id=? WHERE id=?",(str(th.id),mid));await db.commit()
        return th
    except Exception as e:
        log.warning("Finals match thread: %s",e)
        return None

@league.command(name="finals",description="Start the Top-4 Finals")
async def league_finals(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("❌ No league.",ephemeral=True);return
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT weeks_done,finals_generated FROM season WHERE guild_id=?",(gid,)) as cur:row=await cur.fetchone()
    if not row:
        await i.response.send_message("❌ No season record.",ephemeral=True);return
    if row["finals_generated"]:
        await i.response.send_message("❌ Finals already started.",ephemeral=True);return
    if int(row["weeks_done"] or 0)<SEASON_WEEKS:
        await i.response.send_message(f"❌ The regular season is not finished yet. Week **{row['weeks_done']}/{SEASON_WEEKS}**.",ephemeral=True);return

    # Only valid 2–3 player rosters can qualify for Finals.
    eligible_teams=[t for t in await teams_all(gid) if 2 <= len(t.get("members",[])) <= MAX_TEAM]
    ts=sorted(eligible_teams,key=lambda x:(x["mmr"],x["wins"],-x["losses"]),reverse=True)[:4]
    if len(ts)<4:
        await i.response.send_message(
            f"❌ Need 4 eligible teams with 2–{MAX_TEAM} registered players. Found {len(ts)}. "
            "Teams with fewer than 2 or more than 3 players cannot qualify.",ephemeral=True
        );return
    names=[t["display"] for t in ts]

    # Seed-based buildup: 1v4, 2v3, 1v3, 2v4, 1v2, 3v4.
    pairs=[(names[0],names[3]),(names[1],names[2]),(names[0],names[2]),
           (names[1],names[3]),(names[0],names[1]),(names[2],names[3])]

    matches_ch=i.guild.get_channel(int(c["matches_ch"]))
    if not matches_ch or not isinstance(matches_ch,discord.TextChannel):
        await i.response.send_message("❌ The configured #matches channel could not be found.",ephemeral=True);return
    async with aiosqlite.connect(DB) as db:
        # Defensive duplicate check: never create another Finals set.
        async with db.execute("SELECT 1 FROM matches WHERE guild_id=? AND is_finals=1 LIMIT 1",(gid,)) as cur:
            if await cur.fetchone():
                await i.response.send_message("❌ Finals matches already exist for this league.",ephemeral=True);return
        for a,b in pairs:
            await db.execute(
                "INSERT INTO matches VALUES(?,?,?,?,?,NULL,NULL,NULL,?,NULL,1,NULL,NULL,NULL,NULL)",
                (str(uuid.uuid4())[:8],gid,0,a,b,datetime.now(timezone.utc).isoformat())
            )
        await db.execute("UPDATE season SET finals_generated=1 WHERE guild_id=?",(gid,))
        await db.commit()

    category=matches_ch.category

    stage=None
    if category:
        try:stage=await i.guild.create_stage_channel(
            name="🎙️ STAGE MATCHES",category=category,topic="EGL Finals — streamed by League Admins"
        )
        except Exception as e:log.warning("Finals Stage creation: %s",e)

    # Public live bracket thread.
    bracket=None
    try:
        bracket=await matches_ch.create_thread(
            name="🏆 FINALS BRACKET",type=discord.ChannelType.public_thread,auto_archive_duration=10080
        )
    except Exception as e:log.warning("Finals bracket thread: %s",e)

    # Private host/captains thread.
    host=None
    if category is not None:
        try:
            host=await matches_ch.create_thread(
                name="🎙️ FINALS HOST",type=discord.ChannelType.private_thread,auto_archive_duration=10080
            )
            captains=[]
            for t in ts:captains.append(t["captain_id"])
            await _add_thread_members(host,i.guild,captains+await _finals_admin_ids(i.guild))
            await host.send("🎙️ **FINALS HOST / CAPTAINS**\n\nUse this private thread for Finals coordination and streaming logistics.")
        except Exception as e:log.warning("Finals host thread: %s",e)

    bracket_msg=None
    if bracket:
        bracket_msg=await bracket.send("🏆 **FINALS BRACKET**\n\nFinals setup is ready. The live bracket will update as results are confirmed.")
        try:await bracket.edit(locked=True)
        except Exception as e:log.warning("Lock Finals bracket thread: %s",e)

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO finals_state(guild_id,bracket_thread_id,bracket_msg_id,host_thread_id,stage_channel_id,created_at) VALUES(?,?,?,?,?,?)",
            (gid,str(bracket.id) if bracket else None,str(bracket_msg.id) if bracket_msg else None,
             str(host.id) if host else None,str(stage.id) if stage else None,datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

    for n,(a,b) in enumerate(pairs,1):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id FROM matches WHERE guild_id=? AND is_finals=1 AND team1=? AND team2=? AND winner IS NULL ORDER BY created_at DESC LIMIT 1",
                (gid,a,b)
            ) as cur:mr=await cur.fetchone()
        if mr:await _create_finals_match_thread(i.guild,c,gid,mr["id"],a,b,f"Match {n}")

    await update_finals_bracket(i.guild,gid)
    seed_txt="\n".join(f"{n+1}. **{nm}**" for n,nm in enumerate(names))
    if matches_ch:
        await matches_ch.send(
            "🏆 **TOP 4 FINALS HAVE BEGUN**\n\n"+seed_txt+
            "\n\n**Match order:**\n"+
            "\n".join(f"{n}. **{a}** vs **{b}**" for n,(a,b) in enumerate(pairs,1))+
            "\n\nEvery team plays every other team once. Finals Points are based on rounds won."
        )
    ann=i.guild.get_channel(int(c["announcements_ch"])) if c.get("announcements_ch") else None
    if ann:await ann.send(f"🏆 **The Top 4 Finals have begun!**\n\n{seed_txt}\n\nThe live bracket is in <#{c['matches_ch']}>.")
    await i.response.send_message("🏆 Finals started: Stage, bracket, Host thread, and 6 private match threads created.",ephemeral=False)

@league.command(name="tiebreaker",description="Create an admin-hosted Finals tiebreaker")
@app_commands.describe(team1="First tied team",team2="Second tied team")
async def league_tiebreaker(i,team1:str,team2:str):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("❌ No league.",ephemeral=True);return
    if team1.strip().lower()==team2.strip().lower():
        await i.response.send_message("❌ Choose two different teams.",ephemeral=True);return
    points,tb_wins,ranked,base_matches,rows=await finals_standings(gid)
    lookup={x.lower():x for x in points}
    a=lookup.get(team1.strip().lower());b=lookup.get(team2.strip().lower())
    if not a or not b:
        await i.response.send_message("❌ Both teams must be Top-4 Finals teams.",ephemeral=True);return
    for team_name in (a,b):
        team=await team_get(gid,team_name)
        if not team or not (2 <= len(team.get("members",[])) <= MAX_TEAM):
            await i.response.send_message(
                f"❌ **{team_name}** does not have a valid roster. Finals teams must have 2–{MAX_TEAM} registered players.",
                ephemeral=True
            );return
    if points[a]!=points[b]:
        await i.response.send_message(f"❌ A tiebreaker can only be created between teams tied on Finals Points. **{a}: {points[a]}**, **{b}: {points[b]}**.",ephemeral=True);return
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT winner FROM matches WHERE guild_id=? AND is_finals=3 AND ((team1=? AND team2=?) OR (team1=? AND team2=?))",
            (gid,a,b,b,a)
        ) as cur:
            existing=await cur.fetchall()
        if any(r[0] is None for r in existing):
            await i.response.send_message("❌ A tiebreaker between these teams is already pending.",ephemeral=True);return
        if any(r[0] is not None for r in existing):
            await i.response.send_message("❌ A tiebreaker between these teams has already been recorded.",ephemeral=True);return
        mid=str(uuid.uuid4())[:8]
        await db.execute(
            "INSERT INTO matches VALUES(?,?,?,?,?,NULL,NULL,NULL,?,NULL,3,NULL,NULL,NULL,NULL)",
            (mid,gid,0,a,b,datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    th=await _create_finals_match_thread(i.guild,c,gid,mid,a,b,"TIEBREAKER")
    await update_finals_bracket(i.guild,gid)
    await i.response.send_message(f"⚔️ Tiebreaker created: **{a} vs {b}**."+(f" <#{th.id}>" if th else ""),ephemeral=False)

@league.command(name="grandfinal",description="Create the Grand Final after Finals are resolved")
async def league_grandfinal(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("❌ No league.",ephemeral=True);return
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT finals_generated FROM season WHERE guild_id=?",(gid,)) as cur:season=await cur.fetchone()
    if not season or not season["finals_generated"]:
        await i.response.send_message("❌ Finals have not been started.",ephemeral=True);return

    points,tb_wins,ranked,base_matches,rows=await finals_standings(gid)
    if len(base_matches)!=6:
        await i.response.send_message("❌ The 6 Finals round-robin matches have not been created.",ephemeral=True);return
    if any(not r["winner"] for r in base_matches):
        await i.response.send_message("❌ All 6 Finals matches must be completed first.",ephemeral=True);return

    # Re-rank using Finals Points, then completed tiebreaker wins.
    ranked=sorted(points,key=lambda t:(points[t],tb_wins.get(t,0)),reverse=True)
    if len(ranked)<4:
        await i.response.send_message("❌ Finals standings are incomplete.",ephemeral=True);return
    # Recheck finalists before creating the Grand Final in case a roster changed.
    for team_name in ranked[:2]:
        team=await team_get(gid,team_name)
        if not team or not (2 <= len(team.get("members",[])) <= MAX_TEAM):
            await i.response.send_message(
                f"❌ **{team_name}** no longer has a valid 2–{MAX_TEAM} player roster. Correct the roster before creating the Grand Final.",
                ephemeral=True
            );return

    # Any unresolved tie crossing a qualification boundary must be resolved first:
    # - tie for 1st/2nd
    # - tie for the 2nd Grand Final qualification spot
    key=lambda t:(points[t],tb_wins.get(t,0))
    if key(ranked[0])==key(ranked[1]) or key(ranked[1])==key(ranked[2]):
        boundary_key=key(ranked[0]) if key(ranked[0])==key(ranked[1]) else key(ranked[1])
        tied=[t for t in ranked if key(t)==boundary_key]
        await i.response.send_message(
            "❌ The Grand Final cannot be created yet. Resolve the tied teams with `/league tiebreaker`: **"+", ".join(tied)+"**.",
            ephemeral=True
        );return

    # If #3/#4 are tied, require their placement tiebreaker before the Grand Final.
    if points[ranked[2]]==points[ranked[3]] and tb_wins.get(ranked[2],0)==tb_wins.get(ranked[3],0):
        tied=[ranked[2],ranked[3]]
        await i.response.send_message("❌ #3/#4 are tied. Resolve their placement with `/league tiebreaker` before creating the Grand Final: **"+", ".join(tied)+"**.",ephemeral=True);return

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT 1 FROM matches WHERE guild_id=? AND is_finals=2 LIMIT 1",(gid,)) as cur:
            if await cur.fetchone():
                await i.response.send_message("❌ Grand Final already exists.",ephemeral=True);return
        mid=str(uuid.uuid4())[:8]
        t1,t2=ranked[0],ranked[1]
        await db.execute(
            """INSERT INTO matches
               (id, guild_id, week, team1, team2, created_at, is_finals)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, gid, 0, t1, t2, datetime.now(timezone.utc).isoformat(), 2)
        )
        await db.commit()
    th=await _create_finals_match_thread(i.guild,c,gid,mid,t1,t2,"GRAND FINAL")
    matches_ch=i.guild.get_channel(int(c["matches_ch"]))
    if matches_ch:await matches_ch.send(f"🏆🔥 **GRAND FINAL**\n\n**{t1}** vs **{t2}**\n\nFinals Points: **{points[t1]}** vs **{points[t2]}**\n\nThe Grand Final is now ready.")
    await i.response.send_message(f"🏆 Grand Final created: **{t1} vs {t2}**."+(f" <#{th.id}>" if th else ""))

@league.command(name="status",description="Season progress")
async def league_status(i):
    if not await need_league(i):return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT weeks_done FROM season WHERE guild_id=?",(gid,))as cur:row=await cur.fetchone()
    weeks_done=row["weeks_done"]if row else 0
    weeks_left=max(0,SEASON_WEEKS-weeks_done)
    ts=await teams_all(gid)
    total_matches=sum(t["wins"]+t["losses"]for t in ts)//2
    sorted_ts=sorted(ts,key=lambda x:x["mmr"],reverse=True)
    board="\n".join(f"{n+1}. **{t['display']}**  -  {t['wins']}W/{t['losses']}L  -  **{get_rank(t['mmr'])}**"for n,t in enumerate(sorted_ts[:4]))
    await i.response.send_message(f"\U0001f4ca **Season Status**\n\nWeek: **{weeks_done}/{SEASON_WEEKS}**\nWeeks left: **{weeks_left}**\nMatches played: **{total_matches}**\nTeams: **{len(ts)}**\n\n**Current Top 4:**\n{board}")

bot.tree.add_command(league)

class InviteView(discord.ui.View):
    def __init__(s,iid,tn,cid,gid):super().__init__(timeout=86400);s.iid=iid;s.tn=tn;s.cid=cid;s.gid=gid
    @discord.ui.button(label="\u2705 Accept",style=discord.ButtonStyle.green)
    async def a(s,i,btn):
        if i.user.id!=s.iid:await i.response.send_message("Not for you.",ephemeral=True);return
        t=await team_get(str(i.guild_id),s.tn)
        if not t:await i.response.send_message("Gone.",ephemeral=True);return
        if len(t["members"])>=MAX_TEAM:await i.response.send_message("Full.",ephemeral=True);return
        if await team_by_player(str(i.guild_id),str(i.user.id)):await i.response.send_message("On team.",ephemeral=True);return
        if await is_on_cooldown(str(i.guild_id),str(i.user.id)):await i.response.send_message("\u274c 24h cooldown.",ephemeral=True);return
        try:
            async with aiosqlite.connect(DB) as db:
                await db.execute("INSERT INTO members VALUES(?,?,?)",
                                 (str(i.guild_id),s.tn.lower(),str(i.user.id)))
                await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",
                                 (str(i.guild_id),str(i.user.id)))
                await db.commit()
        except aiosqlite.IntegrityError:
            await i.response.send_message("❌ This team is already full (maximum 3 players).",ephemeral=True)
            return
        await add_roles(i.guild,i.user,t["display"])
        if t.get("thread_id"):
            th=i.guild.get_thread(int(t["thread_id"]))
            if th:
                try:await th.add_user(i.user);await th.send(f"\U0001f44b Welcome {i.user.mention}!")
                except:pass
        for c in s.children:c.disabled=True
        await i.response.edit_message(content=f"\u2705 {i.user.mention} joined **{t['display']}**!",view=s)
    @discord.ui.button(label="\u274c Decline",style=discord.ButtonStyle.red)
    async def d(s,i,btn):
        if i.user.id!=s.iid:await i.response.send_message("Not for you.",ephemeral=True);return
        for c in s.children:c.disabled=True;await i.response.edit_message(content="\u274c Declined.",view=s)

team=app_commands.Group(name="team",description="Team management")
@team.command(name="create",description="Create team")
@app_commands.describe(name="Team name",clantag="Clan tag (max 4 characters)")
async def team_create(i,name:str,clantag:str):
    gid=str(i.guild_id);uid=str(i.user.id)
    clantag=clantag.strip().upper()
    if not clantag:await i.response.send_message("\u274c Clan tag is required.",ephemeral=True);return
    if len(clantag)>4:await i.response.send_message("\u274c Clan tag max 4 characters.",ephemeral=True);return
    if len(name)>15:await i.response.send_message("\u274c Team name max 15 characters.",ephemeral=True);return
    import re as _re
    if not _re.match(r"^[A-Za-z0-9 \-']+$",name):await i.response.send_message("\u274c Team name can only use **letters, numbers, spaces, - and '**. No emojis or special characters.",ephemeral=True);return
    if ex:=await team_by_player(gid,uid):await i.response.send_message(f"\u274c On **{ex['display']}**.",ephemeral=True);return
    if await team_get(gid,name):await i.response.send_message(f"\u274c Exists.",ephemeral=True);return
    if await is_on_cooldown(gid,uid):await i.response.send_message("\u274c 24h cooldown.",ephemeral=True);return
    start_mmr=await get_player_mmr(gid,uid)
    # Another callback/check may already have acknowledged this interaction.
    # Never attempt a second initial response; continue with a followup instead.
    if not i.response.is_done():
        try:
            await i.response.defer(thinking=True)
        except discord.HTTPException as e:
            if getattr(e, "code", None) != 40060:
                raise
            log.warning("team_create interaction %s was already acknowledged; continuing with followup", i.id)
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT INTO teams (guild_id,name,display,captain_id,secondary_captain_id,wins,losses,mmr,thread_id,role_id,created_at,clantag) VALUES (?,?,?,?,NULL,0,0,?,NULL,NULL,?,?)",(gid,name.lower(),name,uid,start_mmr,datetime.now(timezone.utc).isoformat(),clantag));await db.execute("INSERT INTO members VALUES(?,?,?)",(gid,name.lower(),uid));await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",(gid,uid));await db.commit()
    c=await cfg_get(gid);tr=await add_roles(i.guild,i.user,name,captain=True)
    role_id=str(tr.id)if tr else None;thread_id=None
    target_ch=None
    if c and c.get("teams_ch"):
        try:target_ch=await i.guild.fetch_channel(int(c["teams_ch"]))
        except:pass
    if target_ch is None:
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT teams_ch FROM setup_data WHERE guild_id=?",(gid,))as cur:sd=await cur.fetchone()
            if not (sd and sd[0]):
                async with db.execute("SELECT teams_ch FROM guild_settings WHERE guild_id=?",(gid,))as cur:sd=await cur.fetchone()
        if sd and sd[0]:
            try:target_ch=await i.guild.fetch_channel(int(sd[0]))
            except:pass
    if target_ch is None and isinstance(i.channel,discord.TextChannel):target_ch=i.channel
    if isinstance(target_ch,discord.TextChannel):
        try:
            th=await target_ch.create_thread(name=f"{name} [{clantag}]"if clantag else name,type=discord.ChannelType.private_thread,auto_archive_duration=10080)
            thread_id=str(th.id);await th.add_user(i.user)
            tip=""
            if clantag:
                tip=f"\n\n\U0001f3ae **Team tip:** Add `[{clantag}]` to your in-game name and rock a matching team shader to represent your team!"
            await th.send(f"\u2694\ufe0f **{name}** thread!{' (clan tag: '+clantag+')' if clantag else ''}\nCaptain:{i.user.mention}\n`/team invite @player`{tip}")
        except Exception as e:log.warning("THREAD ERROR: %s",e)
    async with aiosqlite.connect(DB)as db:await db.execute("UPDATE teams SET role_id=?,thread_id=? WHERE guild_id=? AND name=?",(role_id,thread_id,gid,name.lower()));await db.commit()
    extra=f"\n\U0001f4cc <#{thread_id}>"if thread_id else""
    await i.followup.send(f"\u2694\ufe0f **{name}** created!\nCaptain:{i.user.mention}|Rank:{get_rank(start_mmr)}|1/{MAX_TEAM}{' | Clan tag: '+clantag if clantag else ''}{extra}")
    await log_team_action(i.guild,f"\u2694\ufe0f Team **{name}** created by {i.user.mention}.")
    await refresh_leaderboard(i.guild)
    await refresh_rosters(i.guild)
@team.command(name="invite",description="Invite (captain)")
@app_commands.describe(player="Player")
async def team_invite(i,player:discord.Member):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id);t=await team_get(gid,d)
    if str(player.id)in t["members"]:await i.response.send_message("\u274c Already.",ephemeral=True);return
    if len(t["members"])>=MAX_TEAM:await i.response.send_message("\u274c Full.",ephemeral=True);return
    if await team_by_player(gid,str(player.id)):await i.response.send_message("\u274c Other team.",ephemeral=True);return
    view=InviteView(player.id,d,i.user.id,gid)
    ch=await log_team_action(i.guild,f"\U0001f4e8 {player.mention} invited to **{d}** by {i.user.mention}!",view=view)
    if ch:await i.response.send_message(f"\u2705 Invite posted in {ch.mention}.",ephemeral=True)
    else:await i.response.send_message("\u274c EGL #general was not found. Run `/setup reset` and `/setup run` again.",ephemeral=True)
@team.command(name="kick",description="Kick (captain)")
@app_commands.describe(player="Player")
async def team_kick(i,player:discord.Member):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id)
    if player.id==i.user.id:await i.response.send_message("\u274c Can't self.",ephemeral=True);return
    t=await team_get(gid,d)
    if str(player.id)not in t["members"]:await i.response.send_message("\u274c Not on team.",ephemeral=True);return
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=? AND user_id=?",(gid,d.lower(),str(player.id)));await db.commit()
    await rem_roles(i.guild,player,d)
    t2=await team_get(gid,d)
    if t2 and t2.get("thread_id"):
        th=i.guild.get_thread(int(t2["thread_id"]))
        if th:
            try:await th.remove_user(player)
            except:pass
    await i.followup.send(f"\U0001f9b5 **{player.display_name}** has been kicked.")
    await log_team_action(i.guild,f"\U0001f9b5 {player.mention} was removed from **{t['display']}** by {i.user.mention}.")
@team.command(name="leave",description="Leave (players only)")
async def team_leave(i):
    gid=str(i.guild_id);uid=str(i.user.id);t=await team_by_player(gid,uid)
    if not t:await i.response.send_message("\u274c Not on team.",ephemeral=True);return
    if t["captain_id"]==uid:await i.response.send_message("\u274c Captains use `/disband`.",ephemeral=True);return
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=? AND user_id=?",(gid,t["name"],uid));await db.commit()
    await rem_roles(i.guild,i.user,t["display"])
    if not is_tester(i.user):await set_cooldown(gid,uid,t["mmr"])
    if t.get("thread_id"):
        th=i.guild.get_thread(int(t["thread_id"]))
        if th:
            try:await th.remove_user(i.user)
            except:pass
    await i.followup.send(f"\U0001f44b Left **{t['display']}**.")
    await log_team_action(i.guild,f"\U0001f44b {i.user.mention} left **{t['display']}**.")
bot.tree.add_command(team)

@bot.tree.command(name="disband",description="Disband your team (captain only)")
async def disband(i):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id);t=await team_get(gid,d)
    await i.response.defer()
    if t.get("thread_id"):
        th=i.guild.get_thread(int(t["thread_id"]))
        if th:
            try:await th.edit(archived=True,locked=True)
            except:pass
    if t.get("role_id"):
        role=i.guild.get_role(int(t["role_id"]))
        if role:
            try:await role.delete(reason="Disbanded")
            except:pass
    for uid in t["members"]:
        m=i.guild.get_member(int(uid))
        if m:await rem_roles(i.guild,m,t["display"],uid==t["captain_id"])
        if not is_tester(m):await set_cooldown(gid,uid,t["mmr"])
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=?",(gid,t["name"]));await db.execute("DELETE FROM teams WHERE guild_id=? AND name=?",(gid,t["name"]));await db.commit()
    await i.followup.send(f"\U0001f5d1\ufe0f **{t['display']}** disbanded.")
    await log_team_action(i.guild,f"\U0001f5d1\ufe0f Team **{t['display']}** was disbanded by {i.user.mention}.")
    await refresh_leaderboard(i.guild)
    await refresh_rosters(i.guild)

@bot.tree.command(name="deleteteam",description="Delete a team by name (League Admin only)")
@app_commands.describe(name="Team name")
async def deleteteam(i,name:str):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id);t=await team_get(gid,name)
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    await i.response.defer()
    if t.get("thread_id"):
        th=i.guild.get_thread(int(t["thread_id"]))
        if th:
            try:await th.edit(archived=True,locked=True)
            except:pass
    if t.get("role_id"):
        role=i.guild.get_role(int(t["role_id"]))
        if role:
            try:await role.delete(reason="Deleted by admin")
            except:pass
    for uid in t["members"]:
        m=i.guild.get_member(int(uid))
        if m:await rem_roles(i.guild,m,t["display"],uid==t["captain_id"])
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=?",(gid,t["name"]))
        await db.execute("DELETE FROM teams WHERE guild_id=? AND name=?",(gid,t["name"]))
        await db.commit()
    await i.followup.send(f"\U0001f5d1\ufe0f **{t['display']}** deleted by admin.")
    await refresh_leaderboard(i.guild)
    await refresh_rosters(i.guild)


class ResetAllTeamsView(discord.ui.View):
    def __init__(self, guild_id, admin_id):
        super().__init__(timeout=120)
        self.guild_id=str(guild_id)
        self.admin_id=str(admin_id)
        self.finished=False

    async def interaction_check(self, i):
        if str(i.user.id)!=self.admin_id or not is_admin(i.user):
            await i.response.send_message("Only the League Admin who started this reset can use these buttons.",ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Reset",style=discord.ButtonStyle.danger,emoji="🗑️")
    async def confirm_reset(self,i,button):
        if self.finished:
            await i.response.send_message("This reset has already been handled.",ephemeral=True)
            return
        self.finished=True
        for child in self.children: child.disabled=True
        await i.response.edit_message(content="⏳ Resetting teams, memberships, roles and team threads…",view=self)
        guild=i.guild
        gid=self.guild_id
        async with aiosqlite.connect(DB) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT * FROM teams WHERE guild_id=?",(gid,)) as cur:
                teams=[dict(r) for r in await cur.fetchall()]
            async with db.execute("SELECT team_name,user_id FROM members WHERE guild_id=?",(gid,)) as cur:
                memberships=await cur.fetchall()

        player_ids={str(r["user_id"]) for r in memberships}
        captain_ids=set()
        for t in teams:
            captain_ids.update(str(x) for x in (t.get("captain_id"),t.get("secondary_captain_id")) if x)

        removed_roles=0
        failed=[]
        player_role=find_role(guild,"Player")
        captain_role=find_role(guild,"Captain")
        for uid in player_ids | captain_ids:
            member=guild.get_member(int(uid))
            if member is None: continue
            roles=[]
            if player_role and player_role in member.roles: roles.append(player_role)
            if captain_role and uid in captain_ids and captain_role in member.roles: roles.append(captain_role)
            if roles:
                try:
                    await member.remove_roles(*roles,reason="EGL /resetallteams")
                    removed_roles+=len(roles)
                except Exception as e:
                    failed.append(f"Could not remove roles from {member}: {e}")

        deleted_threads=0
        deleted_team_roles=0
        for t in teams:
            tid=t.get("thread_id")
            if tid:
                try:
                    thread=guild.get_thread(int(tid))
                    if thread is None: thread=await guild.fetch_channel(int(tid))
                    await thread.delete(reason="EGL /resetallteams — team reset")
                    deleted_threads+=1
                except discord.NotFound:
                    pass
                except Exception as e:
                    failed.append(f"Could not delete team thread for {t.get('display')}: {e}")
            rid=t.get("role_id")
            if rid:
                role=guild.get_role(int(rid))
                if role:
                    try:
                        await role.delete(reason="EGL /resetallteams — team reset")
                        deleted_team_roles+=1
                    except Exception as e:
                        failed.append(f"Could not delete team role for {t.get('display')}: {e}")

        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM members WHERE guild_id=?",(gid,))
            await db.execute("DELETE FROM teams WHERE guild_id=?",(gid,))
            await db.commit()

        try:
            await refresh_leaderboard(guild)
            await refresh_rosters(guild)
        except Exception as e:
            failed.append(f"Could not refresh leaderboard/rosters: {e}")

        summary=("✅ **Team reset completed.**\n"
                 f"• Removed team memberships: **{len(memberships)}**\n"
                 f"• Removed Player/Captain roles: **{removed_roles}**\n"
                 f"• Deleted team roles: **{deleted_team_roles}**\n"
                 f"• Deleted team threads: **{deleted_threads}**\n"
                 "• Refreshed leaderboard and roster displays.\n\n"
                 "**Preserved:** league configuration, channels, match history, player history/cooldowns, and unrelated Discord roles.")
        if failed:
            summary+="\n\n⚠️ **Some Discord cleanup actions failed:**\n"+"\n".join(f"• {x}" for x in failed[:10])
        await i.edit_original_response(content=summary,view=None)

    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary,emoji="✖️")
    async def cancel_reset(self,i,button):
        if self.finished:
            await i.response.send_message("This reset has already been handled.",ephemeral=True)
            return
        self.finished=True
        for child in self.children: child.disabled=True
        await i.response.edit_message(content="❎ Team reset cancelled. No changes were made.",view=self)


@bot.tree.command(name="resetallteams",description="Remove all teams, memberships, team roles and threads (League Admin only)")
async def resetallteams_cmd(i):
    if not is_admin(i.user):
        await i.response.send_message(f"❌ Need **{ADMIN_ROLE}**.",ephemeral=True)
        return
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM teams WHERE guild_id=?",(str(i.guild_id),)) as cur:
            count=(await cur.fetchone())[0]
    if count==0:
        await i.response.send_message("There are no registered teams to reset.",ephemeral=True)
        await refresh_leaderboard(i.guild)
        await refresh_rosters(i.guild)
        return
    view=ResetAllTeamsView(i.guild_id,i.user.id)
    await i.response.send_message(
        "⚠️ **ARE YOU SURE YOU WANT TO RESET ALL TEAMS?**\n\n"
        f"This will affect **{count} team(s)** and will:\n"
        "1. Remove every registered player from their team and remove their **Player** role.\n"
        "2. Clear primary/secondary captain assignments and remove the **Captain** role from those captains.\n"
        "3. Delete every team record, each team's dedicated Discord role, and each team's associated team thread.\n"
        "4. Refresh the leaderboard and roster displays so they show no registered teams.\n\n"
        "**This will NOT delete** league channels/configuration, match history, player history/cooldowns, or unrelated Discord roles.\n\n"
        "Press **Confirm Reset** to proceed, or **Cancel** to leave everything unchanged.",
        view=view,ephemeral=True
    )

@bot.tree.command(name="resetteams",description="Reset all teams' MMR + record + match history (League Admin only)")
async def resetteams(i):
    if not is_admin(i.user):await i.response.send_message(f"❌ Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer()
    async with aiosqlite.connect(DB) as db:
        # Reset ONLY MMR, W/L, and match history.
        # Do not reset season state, Finals state, teams, members, captains,
        # player history, Discord roles, or Discord threads.
        await db.execute("UPDATE teams SET mmr=1000,wins=0,losses=0 WHERE guild_id=?",(gid,))
        await db.execute("DELETE FROM underdog_bans WHERE guild_id=?",(gid,))
        await db.execute("DELETE FROM matches WHERE guild_id=?",(gid,))
        await db.commit()
    await i.followup.send("✅ Teams reset to 1000 MMR, 0W/0L, and match history was cleared.")
@bot.tree.command(name="scrimban",description="Toggle a player's scrim signup ban (League Admin only)")
@app_commands.describe(name="Player to ban or unban",duration="How long the ban lasts")
@app_commands.choices(duration=[
    app_commands.Choice(name="1 day",value="1d"),
    app_commands.Choice(name="3 days",value="3d"),
    app_commands.Choice(name="1 week",value="1w"),
    app_commands.Choice(name="Permanent",value="perm"),
])
async def scrimban_cmd(i:discord.Interaction,name:discord.Member,duration:app_commands.Choice[str]=None):
    if not is_admin(i.user):
        await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True)
        return
    if name.bot:
        await i.response.send_message("\u274c Bots cannot be scrim-banned.",ephemeral=True)
        return
    role=find_role(i.guild,SCRIM_BAN_ROLE)
    if not role:
        try:
            role=await i.guild.create_role(name=SCRIM_BAN_ROLE,color=discord.Color.red(),reason="Scrim signup restriction role")
        except Exception as e:
            await i.response.send_message(f"\u274c Couldn't create the scrim-ban role: {e}",ephemeral=True)
            return
    gid=str(i.guild_id);uid=str(name.id)
    if role in name.roles:
        try:
            await name.remove_roles(role,reason=f"Scrim ban toggled off by {i.user}")
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM scrim_bans WHERE guild_id=? AND user_id=?",(gid,uid))
                await db.commit()
            await i.response.send_message(f"\u2705 Removed the scrim ban from {name.mention}. They can sign up for scrims again.")
        except Exception as e:
            await i.response.send_message(f"\u274c Couldn't remove the scrim ban: {e}",ephemeral=True)
        return

    choice=duration.value if duration else "perm"
    now=int(datetime.now(timezone.utc).timestamp())
    expires_at={"1d":now+86400,"3d":now+3*86400,"1w":now+7*86400}.get(choice)
    label={"1d":"1 day","3d":"3 days","1w":"1 week","perm":"permanently"}[choice]
    try:
        await name.add_roles(role,reason=f"Scrim ban applied by {i.user} ({label})")
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO scrim_bans(guild_id,user_id,expires_at) VALUES(?,?,?)",
                (gid,uid,expires_at)
            )
            await db.commit()
        await i.response.send_message(f"\u2705 {name.mention} is banned from signing up for scrims {label}. Use `/scrimban` on them again to remove the ban early.")
    except Exception as e:
        await i.response.send_message(f"\u274c Couldn't apply the scrim ban: {e}",ephemeral=True)

@bot.tree.command(name="spoon",description="Get the Spoon role - anyone can ping you")
async def spoon_cmd(i):
    await i.response.defer()
    role=find_role(i.guild,SPOON_ROLE)
    if not role:
        try:role=await i.guild.create_role(name=SPOON_ROLE,color=discord.Color.from_rgb(200,200,200),mentionable=True)
        except Exception as e:
            await i.followup.send(f"\u274c Couldn't create the Spoon role: {e}",ephemeral=True);return
    if not role.mentionable:
        try:await role.edit(mentionable=True)
        except:pass
    already=role in i.user.roles
    if already:
        try:
            await i.user.remove_roles(role)
            await i.followup.send(f"\U0001f944 {i.user.mention} is no longer a **Spoon**.")
        except Exception as e:
            await i.followup.send(f"\u274c Couldn't remove the role: {e}",ephemeral=True);return
    else:
        try:
            await i.user.add_roles(role)
            await i.followup.send(f"\U0001f944 {i.user.mention} is now a **Spoon**.")
        except Exception as e:
            await i.followup.send(f"\u274c Couldn't give you the role: {e}",ephemeral=True);return
    try:
        msg=await i.original_response()
        await msg.add_reaction("\U0001f944")
    except:pass

@bot.tree.command(name="forcecaptainswap",description="Change a team's captain, even if the old one left (League Admin only)")
@app_commands.describe(team="Team name",new_captain="New captain")
async def forcecaptainswap(i,team:str,new_captain:discord.Member):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id);t=await team_get(gid,team)
    if not t:await i.response.send_message("\u274c Team not found.",ephemeral=True);return
    if str(new_captain.id)not in t["members"]:await i.response.send_message("\u274c That player isn't on this team.",ephemeral=True);return
    old_id=t["captain_id"]
    if str(new_captain.id)==old_id:await i.response.send_message("\u274c Already the captain.",ephemeral=True);return
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:
        await db.execute("UPDATE teams SET captain_id=? WHERE guild_id=? AND name=?",(str(new_captain.id),gid,t["name"]));await db.commit()
    cr=find_role(i.guild,"Captain")
    old_member=i.guild.get_member(int(old_id))
    if cr:
        try:
            if old_member:await old_member.remove_roles(cr)
            await new_captain.add_roles(cr)
        except:pass
    await i.followup.send(f"\U0001f451 **{t['display']}** captain is now {new_captain.mention}.")

@bot.tree.command(name="forcekick",description="Force remove a player from a team (League Admin only)")
@app_commands.describe(team="Team name",player="Player to remove")
async def forcekick(i,team:str,player:discord.Member):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id);t=await team_get(gid,team)
    if not t:await i.response.send_message("\u274c Team not found.",ephemeral=True);return
    if str(player.id)not in t["members"]:await i.response.send_message("\u274c Not on that team.",ephemeral=True);return
    await i.response.defer()
    was_cap=str(player.id)==t["captain_id"]
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=? AND user_id=?",(gid,t["name"],str(player.id)));await db.commit()
    await rem_roles(i.guild,player,t["display"],was_cap)
    if t.get("thread_id"):
        th=i.guild.get_thread(int(t["thread_id"]))
        if th:
            try:await th.remove_user(player)
            except:pass
    # If the kicked player was captain, promote the next member
    if was_cap:
        t2=await team_get(gid,t["name"])
        if t2 and t2["members"]:
            ncap=str(t2["members"][0])
            async with aiosqlite.connect(DB)as db:
                await db.execute("UPDATE teams SET captain_id=? WHERE guild_id=? AND name=?",(ncap,gid,t["name"]));await db.commit()
            nm=i.guild.get_member(int(ncap))
            cr=find_role(i.guild,"Captain")
            if nm and cr:
                try:await nm.add_roles(cr)
                except:pass
            await i.followup.send(f"\U0001f9b5 Removed {player.mention}. New captain: <@{ncap}>.")
        else:
            if t.get("thread_id"):
                th=i.guild.get_thread(int(t["thread_id"]))
                if th:
                    try:await th.edit(archived=True,locked=True)
                    except:pass
            if t.get("role_id"):
                role=i.guild.get_role(int(t["role_id"]))
                if role:
                    try:await role.delete()
                    except:pass
            async with aiosqlite.connect(DB)as db:
                await db.execute("DELETE FROM teams WHERE guild_id=? AND name=?",(gid,t["name"]));await db.commit()
            await i.followup.send(f"\U0001f9b5 Removed {player.mention}. Team **{t['display']}** is now empty and was disbanded.")
    else:
        await i.followup.send(f"\U0001f9b5 Removed {player.mention} from **{t['display']}**.")

@bot.tree.command(name="forcenamechange",description="Change a team's name (League Admin only)")
@app_commands.describe(old_name="Current team name",new_name="New team name")
async def forcenamechange(i,old_name:str,new_name:str):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id);t=await team_get(gid,old_name)
    if not t:await i.response.send_message("\u274c Team not found.",ephemeral=True);return
    new_name=new_name.strip()
    if not new_name:await i.response.send_message("\u274c New name required.",ephemeral=True);return
    if len(new_name)>15:await i.response.send_message("\u274c Team name max 15 characters.",ephemeral=True);return
    import re as _re
    if not _re.match(r"^[A-Za-z0-9 \-']+$",new_name):await i.response.send_message("\u274c Name can only use **letters, numbers, spaces, - and '**. No emojis or special characters.",ephemeral=True);return
    if await team_get(gid,new_name):await i.response.send_message("\u274c A team with that name already exists.",ephemeral=True);return
    old_key=t["name"];old_disp=t["display"];new_key=new_name.lower()
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:
        await db.execute("UPDATE teams SET name=?,display=? WHERE guild_id=? AND name=?",(new_key,new_name,gid,old_key))
        await db.execute("UPDATE members SET team_name=? WHERE guild_id=? AND team_name=?",(new_key,gid,old_key))
        await db.execute("UPDATE matches SET team1=? WHERE guild_id=? AND team1=?",(new_name,gid,old_disp))
        await db.execute("UPDATE matches SET team2=? WHERE guild_id=? AND team2=?",(new_name,gid,old_disp))
        await db.commit()
    if t.get("role_id"):
        role=i.guild.get_role(int(t["role_id"]))
        if role:
            try:await role.edit(name=new_name)
            except:pass
    if t.get("thread_id"):
        th=i.guild.get_thread(int(t["thread_id"]))
        if th:
            try:await th.edit(name=new_name)
            except:pass
    await refresh_leaderboard(i.guild)
    await refresh_rosters(i.guild)
    await i.followup.send(f"\u270e **{old_disp}** is now **{new_name}**.")

@bot.tree.command(name="schedulestatus",description="Check which matches are scheduled (League Admin only)")
async def schedulestatus(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT MAX(week) AS wk FROM matches WHERE guild_id=? AND winner IS NULL",(gid,))as cur:
            mrow=await cur.fetchone()
    if not mrow or mrow["wk"] is None:await i.followup.send("\u2705 No open matches.",ephemeral=True);return
    cur_week=mrow["wk"]
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT team1,team2,map,scheduled,week FROM matches WHERE guild_id=? AND winner IS NULL AND week=? ORDER BY team1",(gid,cur_week))as cur:
            rows=await cur.fetchall()
    if not rows:await i.followup.send("\u2705 No open matches.",ephemeral=True);return
    lines=[]
    now=datetime.now(timezone.utc)
    for r in rows:
        r=dict(r)
        sched=r.get("scheduled")
        time_part=("\u274c Not scheduled")
        if sched:
            try:
                dt=datetime.strptime(sched.replace(" GMT",""),SCHED_FMT).replace(year=now.year,tzinfo=timezone.utc)
                unix=int(dt.timestamp())
                time_part=f"**{sched}** \u00b7 Your time: <t:{unix}:f>"
            except:time_part=f"**{sched}**"
        mp=r.get("map")or"\u274c No map"
        lines.append(f"**W{r['week']}** {r['team1']} vs {r['team2']}\n\U0001f4c5 {time_part}\n\U0001f5fa\ufe0f {mp}")
    embed=discord.Embed(title=f"\U0001f4c5 Schedule Status \u00b7 Week {cur_week}",description="\n\n".join(lines),color=0x5865F2)
    embed.set_footer(text=f"{len(rows)} open matches this week")
    await i.followup.send(embed=embed,ephemeral=True)


class CloseReasonModal(discord.ui.Modal):
    def __init__(self,callback_fn):
        super().__init__(title="Reason for closing match")
        self.callback_fn=callback_fn
        self.reason=discord.ui.TextInput(
            label="Reason",
            placeholder="Enter the reason this match is being closed...",
            required=True,
            min_length=1,
            max_length=1000,
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.reason)

    async def on_submit(self,i):
        reason=self.reason.value.strip()
        if not reason:
            await i.response.send_message("❌ A reason is required.",ephemeral=True)
            return
        await self.callback_fn(i,reason)

class AdminScoreView(discord.ui.View):
    def __init__(self,gid,wk,wd,lk,ld,thread_id):
        super().__init__(timeout=600)
        self.gid=gid;self.wk=wk;self.wd=wd;self.lk=lk;self.ld=ld;self.tid=thread_id
        sel=discord.ui.Select(placeholder="Pick the final score",options=[
            discord.SelectOption(label=f"{wd} 3-0 {ld}",value="3-0"),
            discord.SelectOption(label=f"{wd} 3-1 {ld}",value="3-1"),
            discord.SelectOption(label=f"{wd} 3-2 {ld}",value="3-2"),
        ])
        sel.callback=self._submit
        self.add_item(sel)

    async def _submit(self,i):
        score=self.children[0].values[0]

        async def finish(interaction,reason):
            await interaction.response.defer(ephemeral=True)
            result=await admin_close_win(
                interaction,self.gid,self.wk,self.wd,self.lk,self.ld,
                score,self.tid,reason
            )
            status=[]
            if result["results_sent"]:
                status.append("posted to #results")
            else:
                status.append("⚠️ could not post to #results")
            if result["thread_closed"]:
                status.append("thread closed")
            else:
                status.append("⚠️ thread could not be archived/locked")
            await interaction.followup.send(
                f"✅ Closed as **{self.wd} {score} {self.ld}**.\n"
                f"Reason: **{reason}**\n"
                f"{' • '.join(status)}",
                ephemeral=True
            )

        await i.response.send_modal(CloseReasonModal(finish))

class ClosematchView(discord.ui.View):
    def __init__(self,gid,t1k,t1d,t2k,t2d,thread_id):
        super().__init__(timeout=600)
        self.gid=gid;self.t1k=t1k;self.t1d=t1d;self.t2k=t2k;self.t2d=t2d;self.tid=thread_id
        for k,d in((t1k,t1d),(t2k,t2d)):
            b=discord.ui.Button(label=d,style=discord.ButtonStyle.primary)
            b.callback=self._mk(k,d)
            self.add_item(b)
        bf=discord.ui.Button(label="Both forfeit",style=discord.ButtonStyle.secondary)
        bf.callback=self._both
        self.add_item(bf)
        bc=discord.ui.Button(label="Cancel (no changes)",style=discord.ButtonStyle.danger)
        bc.callback=self._cancel
        self.add_item(bc)

    def _mk(self,wk,wd):
        async def cb(i):
            lk=self.t2k if wk==self.t1k else self.t1k
            ld=self.t2d if wk==self.t1k else self.t1d
            v=AdminScoreView(self.gid,wk,wd,lk,ld,self.tid)
            await i.response.edit_message(
                content=f"**Step 2:** pick the final score - {wd} vs {ld}",
                view=v
            )
        return cb

    async def _both(self,i):
        async def finish(interaction,reason):
            await interaction.response.defer(ephemeral=True)
            result=await admin_close_both(
                interaction,self.gid,self.t1k,self.t1d,self.t2k,self.t2d,
                self.tid,reason
            )
            status=[]
            if result["results_sent"]:
                status.append("posted to #results")
            else:
                status.append("⚠️ could not post to #results")
            if result["thread_closed"]:
                status.append("thread closed")
            else:
                status.append("⚠️ thread could not be archived/locked")
            await interaction.followup.send(
                f"✅ Closed: both forfeit ({self.t1d} vs {self.t2d}).\n"
                f"Reason: **{reason}**\n"
                f"{' • '.join(status)}",
                ephemeral=True
            )

        await i.response.send_modal(CloseReasonModal(finish))

    async def _cancel(self,i):
        async def finish(interaction,reason):
            await interaction.response.defer(ephemeral=True)
            result=await admin_close_cancel(
                interaction,self.gid,self.t1d,self.t2d,self.tid,reason
            )
            status=[]
            if result["results_sent"]:
                status.append("posted to #results")
            else:
                status.append("⚠️ could not post to #results")
            if result["thread_closed"]:
                status.append("thread closed")
            else:
                status.append("⚠️ thread could not be archived/locked")
            await interaction.followup.send(
                f"✅ Canceled: {self.t1d} vs {self.t2d} - no changes.\n"
                f"Reason: **{reason}**\n"
                f"{' • '.join(status)}",
                ephemeral=True
            )

        await i.response.send_modal(CloseReasonModal(finish))

async def _admin_thread(i,thread_id):
    t=None
    if thread_id:
        try:t=i.guild.get_thread(int(thread_id))
        except:t=None
        if t is None:
            try:t=await i.guild.fetch_channel(int(thread_id))
            except Exception as e:
                log.warning("Could not fetch match thread %s: %s",thread_id,e)
                t=None
    if t is None and isinstance(i.channel,discord.Thread):t=i.channel
    return t

async def _admin_close_thread(i,thread_id,msg):
    th=await _admin_thread(i,thread_id)
    if not th:
        log.warning("Could not find match thread %s to delete.",thread_id)
        return False

    for attempt in range(3):
        try:
            # Match threads should be permanently deleted when /closematch
            # completes, not archived or locked.
            await th.delete(reason="Match closed by league admin")
            return True
        except discord.HTTPException as e:
            log.warning(
                "Admin delete thread attempt %d/3 failed for %s: %s",
                attempt+1,thread_id,e
            )
        except Exception as e:
            log.warning(
                "Admin delete thread attempt %d/3 failed for %s: %s",
                attempt+1,thread_id,e
            )

        if attempt<2:
            await asyncio.sleep(1.0*(attempt+1))
            refreshed=await _admin_thread(i,thread_id)
            if refreshed:
                th=refreshed

    return False

async def _admin_post_results(i,gid,line):
    c=await cfg_get(gid)
    if not c or not c.get("results_ch"):
        log.warning("No results channel configured for guild %s.",gid)
        return False

    channel_id=int(c["results_ch"])
    rc=i.guild.get_channel(channel_id)
    if rc is None:
        try:
            rc=await i.guild.fetch_channel(channel_id)
        except Exception as e:
            log.warning("Could not fetch results channel %s: %s",channel_id,e)
            return False

    for attempt in range(3):
        try:
            await rc.send(line)
            return True
        except discord.HTTPException as e:
            log.warning(
                "Admin results post attempt %d/3 failed for %s: %s",
                attempt+1,channel_id,e
            )
        except Exception as e:
            log.warning(
                "Admin results post attempt %d/3 failed for %s: %s",
                attempt+1,channel_id,e
            )
        if attempt<2:
            await asyncio.sleep(1.0*(attempt+1))
            if rc is None:
                try:rc=await i.guild.fetch_channel(channel_id)
                except:pass

    return False

async def admin_close_win(i,gid,wk,wd,lk,ld,score,thread_id,reason):
    wt=await team_get(gid,wk);lt=await team_get(gid,lk)
    if not wt or not lt:
        return {"results_sent":False,"thread_closed":False}

    wmmr=int(wt["mmr"]);lmmr=int(lt["mmr"])
    expected=0.5 if wmmr==lmmr else 1/(1+10**((lmmr-wmmr)/400))
    try:our,their=map(int,score.split("-"))
    except:our,their=3,0
    delta=round(50*(1-expected)*{3:1.25,2:1.0,1:0.75}.get(abs(our-their),1.0))

    async with aiosqlite.connect(DB)as db:
        await db.execute(
            "UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",
            (delta,gid,wk)
        )
        await db.execute(
            "UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",
            (delta,gid,lk)
        )
        await db.execute(
            "UPDATE matches SET score=?,winner=?,reporter=?,close_reason=? "
            "WHERE thread_id=? AND guild_id=? AND winner IS NULL",
            (score,wd,str(i.user.id),reason,str(thread_id),gid)
        )
        await db.commit()

    w_after=wmmr+delta
    l_after=lmmr-delta
    results_sent=await _admin_post_results(
        i,gid,
        f"⚡ **{wd} {score} {ld}**\n"
        f"Winner: **{wd}**\n"
        f"**{wd}**: **{wmmr} → {w_after} MMR** (**+{delta} MMR**)\n"
        f"**{ld}**: **{lmmr} → {l_after} MMR** (**-{delta} MMR**)\n"
        f"Closed by an admin.\n"
        f"📝 **Reason:** {reason}"
    )
    thread_closed=await _admin_close_thread(
        i,thread_id,
        f"🔒 Match closed by an admin: **{wd} {score} {ld}**.\n"
        f"📝 **Reason:** {reason}"
    )
    try:await refresh_leaderboard(i.guild)
    except:pass
    return {"results_sent":results_sent,"thread_closed":thread_closed}

async def admin_close_both(i,gid,k1,d1,k2,d2,thread_id,reason):
    t1=await team_get(gid,k1);t2=await team_get(gid,k2)
    if not t1 or not t2:
        return {"results_sent":False,"thread_closed":False}
    m1=int(t1["mmr"]);m2=int(t2["mmr"])
    async with aiosqlite.connect(DB)as db:
        await db.execute(
            "UPDATE teams SET mmr=mmr-10 WHERE guild_id=? AND name IN (?,?)",
            (gid,k1,k2)
        )
        await db.execute(
            "UPDATE matches SET score='forfeit',winner='both-forfeit',reporter=?,close_reason=? "
            "WHERE thread_id=? AND guild_id=? AND winner IS NULL",
            (str(i.user.id),reason,str(thread_id),gid)
        )
        await db.commit()

    results_sent=await _admin_post_results(
        i,gid,
        f"⚠️ **{d1} vs {d2}**\n"
        f"**{d1}**: **{m1} → {m1-10} MMR** (**-10 MMR**)\n"
        f"**{d2}**: **{m2} → {m2-10} MMR** (**-10 MMR**)\n"
        f"Both teams forfeit - no W/L change.\n"
        f"📝 **Reason:** {reason}"
    )
    thread_closed=await _admin_close_thread(
        i,thread_id,
        f"🔒 Match closed by an admin: **both teams forfeit**.\n"
        f"📝 **Reason:** {reason}"
    )
    try:await refresh_leaderboard(i.guild)
    except:pass
    return {"results_sent":results_sent,"thread_closed":thread_closed}

async def admin_close_cancel(i,gid,d1,d2,thread_id,reason):
    async with aiosqlite.connect(DB)as db:
        await db.execute(
            "UPDATE matches SET score='canceled',winner='canceled',reporter=?,close_reason=? "
            "WHERE thread_id=? AND guild_id=? AND winner IS NULL",
            (str(i.user.id),reason,str(thread_id),gid)
        )
        await db.commit()

    results_sent=await _admin_post_results(
        i,gid,
        f"🚫 **{d1} vs {d2}**\n"
        f"Canceled by an admin - no MMR or W/L changes.\n"
        f"📝 **Reason:** {reason}"
    )
    thread_closed=await _admin_close_thread(
        i,thread_id,
        f"🔒 Match canceled by an admin - no changes.\n"
        f"📝 **Reason:** {reason}"
    )
    return {"results_sent":results_sent,"thread_closed":thread_closed}

@bot.tree.command(name="closematch",description="Close a match using the result picker (League Admin only, in match thread)")
async def closematch(i):
    if not is_admin(i.user):await i.response.send_message(f"❌ Need **{ADMIN_ROLE}**.",ephemeral=True);return
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("❌ Use inside the match thread.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=? AND winner IS NULL",(str(i.channel.id),gid))as cur:
            row=await cur.fetchone()
    if not row:
        await i.followup.send("❌ No open match in this thread (or already resolved).",ephemeral=True)
        return
    row=dict(row);t1=row["team1"];t2=row["team2"]
    tt=await teams_all(gid)
    t1k=next((x["name"]for x in tt if x["display"]==t1),t1.lower())
    t2k=next((x["name"]for x in tt if x["display"]==t2),t2.lower())
    v=ClosematchView(gid,t1k,t1,t2k,t2,str(i.channel.id))
    await i.followup.send(
        f"🔒 **Close match** - {t1} vs {t2}\n\n"
        "**Step 1:** who won? (or use forfeit / cancel below)\n"
        "**Final step:** you will be asked for the reason for closing the match.",
        view=v,ephemeral=True
    )

@bot.tree.command(name="teaminfo",description="Team info")
@app_commands.describe(team="Team name")
async def teaminfo(i,team:str):
    t=await team_get(str(i.guild_id),team)
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    tot=t["wins"]+t["losses"];wr=f"{round(t['wins']/tot*100)}%"if tot else"N/A"
    crown="\U0001f451";players="\n".join(f"\u2022 <@{m}> {crown if m==t['captain_id']else''}"for m in t["members"])
    rn,rpct,next_r=rank_progress(t["mmr"])
    bar="\u2588"*rpct+"\u2591"*(10-rpct)
    embed=discord.Embed(title=f"\u2694\ufe0f {t['display']}",color=0x5865F2)
    embed.add_field(name="Rank",value=f"**{rn}** `{t['mmr']} MMR`\n{bar} `{rpct*10}%` to {next_r}",inline=False)
    embed.add_field(name="Record",value=f"{t['wins']}W / {t['losses']}L",inline=True)
    embed.add_field(name="Win Rate",value=wr,inline=True)
    embed.add_field(name="Players",value=f"{len(t['members'])}/{MAX_TEAM}",inline=True)
    embed.add_field(name="Roster",value=players,inline=False)
    await i.response.send_message(embed=embed)

cap=app_commands.Group(name="captain",description="Captain")
@cap.command(name="swap",description="Transfer captain")
@app_commands.describe(player="New captain")
async def captain_swap(i,player:discord.Member):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id);t=await team_get(gid,d)
    if str(player.id)not in t["members"]:await i.response.send_message("\u274c Not on team.",ephemeral=True);return
    if player.id==i.user.id:await i.response.send_message("\u274c Already.",ephemeral=True);return
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:await db.execute("UPDATE teams SET captain_id=? WHERE guild_id=? AND name=?",(str(player.id),gid,t["name"]));await db.commit()
    cr=find_role(i.guild,"Captain")
    if cr:
        try:await i.user.remove_roles(cr);await player.add_roles(cr)
        except:pass
    await i.followup.send(f"\U0001f451 Capt of **{d}** \u2192 {player.mention}.")
    await log_team_action(i.guild,f"\U0001f451 Captain of **{d}** transferred from {i.user.mention} to {player.mention}.")
bot.tree.add_command(cap)

mat=app_commands.Group(name="match",description="Match")
class ResultScoreView(discord.ui.View):
    def __init__(self,gid,reporter,opp_key,winner,loser):
        super().__init__(timeout=600)
        self.gid=gid;self.reporter=reporter;self.opp_key=opp_key;self.winner=winner;self.loser=loser
        sel=discord.ui.Select(placeholder="Pick the final score",options=[
            discord.SelectOption(label=f"{winner} 3-0 {loser}",value="3-0"),
            discord.SelectOption(label=f"{winner} 3-1 {loser}",value="3-1"),
            discord.SelectOption(label=f"{winner} 3-2 {loser}",value="3-2"),
        ])
        sel.callback=self._submit
        self.add_item(sel)
    async def _submit(self,i):
        score=self.children[0].values[0]
        await i.response.edit_message(content=f"\u2705 Reporting **{self.winner} {score} {self.loser}**...",view=None)
        await finalize_report(i,self.gid,self.reporter,self.opp_key,self.winner,self.loser,score)

class ResultWinnerView(discord.ui.View):
    def __init__(self,gid,t1,t2,reporter,opp_key):
        super().__init__(timeout=600)
        self.gid=gid;self.t1=t1;self.t2=t2;self.reporter=reporter;self.opp_key=opp_key
        for nm in(t1,t2):
            b=discord.ui.Button(label=nm,style=discord.ButtonStyle.primary)
            b.callback=self._mk(nm)
            self.add_item(b)
    def _mk(self,name):
        async def cb(i):
            loser=self.t2 if name==self.t1 else self.t1
            v=ResultScoreView(self.gid,self.reporter,self.opp_key,name,loser)
            await i.response.edit_message(content=f"**Step 2:** pick the final score - {name} vs {loser}",view=v)
        return cb

async def finalize_report(i,gid,reporter,opp_key,winner,loser,score):
    my_t=await team_get(gid,reporter);opp_t=await team_get(gid,opp_key)
    if not my_t or not opp_t:return False
    my_mmr=int(my_t["mmr"]);opp_mmr=int(opp_t["mmr"])
    won=(str(winner).strip().lower()!=str(opp_t["display"]).strip().lower())
    if my_mmr==opp_mmr:expected=0.5
    else:expected=1/(1+10**((opp_mmr-my_mmr)/400))
    base=50*(1-expected)if won else 50*expected
    try:our,their=map(int,score.split("-"))
    except:our,their=3,0
    mult={3:1.25,2:1.0,1:0.75}.get(abs(our-their),1.0)
    delta=round(base*mult)
    c=await cfg_get(gid)
    oc=i.guild.get_member(int(opp_t["captain_id"]))
    if not oc:return False
    su="+"if won else"-";st="-"if won else"+"
    v=ResultConfirmView(opp_t["captain_id"],my_t["name"],my_t["display"],opp_t["name"],opp_t["display"],score,won,winner,delta,gid,c,str(i.channel.id))
    await i.channel.send(f"\u26a1 {i.user.mention} reports: **{winner} {score} {loser}**\n{reporter}({get_rank(my_mmr)}) {su}{delta} | {opp_t['display']}({get_rank(opp_mmr)}) {st}{delta}\n\n{oc.mention} please confirm:",view=v)
    return True

@mat.command(name="result",description="Report the result of this match (captain)")
async def match_report(i):
    d=await need_captain(i)
    if not d:return
    if not isinstance(i.channel,discord.Thread):
        await i.response.send_message("\u274c Use this inside the match thread.",ephemeral=True);return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=? AND winner IS NULL",(str(i.channel.id),gid))as cur:
            row=await cur.fetchone()
    if not row:
        await i.response.send_message("\u274c No open match in this thread.",ephemeral=True);return
    row=dict(row);t1=row["team1"];t2=row["team2"]
    if d not in(t1,t2):
        await i.response.send_message("\u274c You're not in this match.",ephemeral=True);return
    opp=t2 if d==t1 else t1
    v=ResultWinnerView(gid,t1,t2,d,opp)
    await i.response.send_message(f"\u26a1 **Report result** - {t1} vs {t2}\n\n**Step 1:** which team won?",view=v,ephemeral=True)
bot.tree.add_command(mat)

mmr=app_commands.Group(name="mmr",description="MMR (Admin)")
@mmr.command(name="adjust",description="Adjust rank (admin)")
@app_commands.describe(team="Team",amount="+/- change")
async def mmr_adjust(i,team:str,amount:int):
    if not await need_admin(i):return
    gid=str(i.guild_id);t=await team_get(gid,team)
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    old=t["mmr"];new=old+amount
    async with aiosqlite.connect(DB)as db:await db.execute("UPDATE teams SET mmr=? WHERE guild_id=? AND name=?",(new,gid,t["name"]));await db.commit()
    await i.response.send_message(f"\U0001f4ca **{t['display']}** MMR: **{old} \u2192 {new}** (**{amount:+d} MMR**)\nRank: {get_rank(old)} \u2192 {get_rank(new)}")
    await refresh_leaderboard(i.guild)
bot.tree.add_command(mmr)

@bot.tree.command(name="recordadjust",description="Give or remove wins/losses (League Admin only)")
@app_commands.describe(team="Team name",wins="Wins to add (use negative to remove)",losses="Losses to add (use negative to remove)")
async def recordadjust(i,team:str,wins:int=0,losses:int=0):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id);t=await team_get(gid,team)
    if not t:await i.response.send_message("\u274c Team not found.",ephemeral=True);return
    if wins==0 and losses==0:await i.response.send_message("\u274c Nothing to change - set wins and/or losses.",ephemeral=True);return
    new_w=max(0,int(t["wins"])+int(wins))
    new_l=max(0,int(t["losses"])+int(losses))
    async with aiosqlite.connect(DB)as db:
        await db.execute("UPDATE teams SET wins=?,losses=? WHERE guild_id=? AND name=?",(new_w,new_l,gid,t["name"]));await db.commit()
    await i.response.send_message(f"\U0001f4ca **{t['display']}**: {t['wins']}W/{t['losses']}L \u2192 **{new_w}W/{new_l}L**")
    await refresh_leaderboard(i.guild)
    await refresh_rosters(i.guild)

test=app_commands.Group(name="test",description="Test (Admin)")
generatematch=app_commands.Group(name="generatematch",description="Generate and manage league match fixtures")

@generatematch.command(name="week",description="Generate matches for a specific week without changing the week counter")
@app_commands.describe(number="Week number to generate")
async def generate_week_cmd(i,number:app_commands.Range[int,1,SEASON_WEEKS]):
    if not await need_admin(i):return
    gid=str(i.guild_id)
    c=await cfg_get(gid)
    if not c:
        await i.response.send_message("❌ No league.",ephemeral=True)
        return

    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT weeks_done FROM season WHERE guild_id=?",
            (gid,)
        ) as cur:
            season=await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM matches WHERE guild_id=? AND week=?",
            (gid,number)
        ) as cur:
            existing=(await cur.fetchone())[0]

    if not season:
        await i.followup.send("❌ No active season found.",ephemeral=True)
        return
    if existing:
        await i.followup.send(
            f"❌ **Week {number}** already has **{existing}** match record(s). "
            "No new matches were generated. Use the reset command after it is added if you need to replace these fixtures.",
            ephemeral=True
        )
        return

    try:
        n=await gen_matches(i.guild,c,force=True,target_week=number)
    except Exception:
        log.exception("Specific-week match generation failed for Week %s",number)
        await i.followup.send(
            "❌ Match generation failed. Check the bot logs for details.",
            ephemeral=True
        )
        return

    if not n:
        await i.followup.send(
            f"❌ No matches were generated for **Week {number}**. "
            "Check that at least two eligible teams have 2–3 players.",
            ephemeral=True
        )
        return

    await i.followup.send(
        f"✅ Generated **Week {number}** — **{n}** match(es). "
        f"The season counter remains at **Week {int(season['weeks_done'])}/{SEASON_WEEKS}**.",
        ephemeral=True
    )

bot.tree.add_command(generatematch)

@test.command(name="generatematches",description="Force generate")
async def test_gen(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("\u274c No league.",ephemeral=True);return
    await i.response.defer();n=await gen_matches(i.guild,c,force=True)
    await i.followup.send(f"\u2705 {n or 0} matches in <#{c['matches_ch']}>.")
bot.tree.add_command(test)

@bot.tree.command(name="schedule",description="Schedule a match")
async def schedule_cmd(i):
    await _open_schedule_picker(i,"schedule")


create_grp=app_commands.Group(name="create",description="Create scrims")
@create_grp.command(name="mixedscrim",description="Create a mixed scrim")
@app_commands.describe(time="Start time HH:MM GMT (e.g. 20:00)")
async def create_mixedscrim(i,time:str):
    msc_ch=None
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            for ch in cat.text_channels:
                if ch.name=="mixed-scrims":msc_ch=ch;break
    if not msc_ch:await i.response.send_message("\u274c No #mixed-scrims channel. Run `/scrimbot setup`.",ephemeral=True);return
    if i.channel.id!=msc_ch.id:await i.response.send_message(f"\u274c Please use this in {msc_ch.mention}.",ephemeral=True);return
    # Accept: 20:00, 20.00, 8pm, 8:30pm, 8.30pm, 20, 8pm
    import re
    t=time.strip().lower()
    m24=re.match(r'^(\d{1,2})[:\.](\d{2})$',t)
    m12=re.match(r'^(\d{1,2})[:\.]?(\d{2})?(am|pm)$',t)
    try:
        if m12: h=int(m12.group(1));mi=int(m12.group(2)or 0);ap=m12.group(3);h=0 if h==12 and ap=='am'else(12 if h==12 and ap=='pm'else(h+(12 if ap=='pm'else 0)))
        elif m24: h=int(m24.group(1));mi=int(m24.group(2))
        else: raise ValueError
        utc_h=h%24;scrim_time=f"{h%24:02d}:{mi:02d}"
        dt=datetime.now(timezone.utc).replace(hour=utc_h,minute=mi,second=0,tzinfo=timezone.utc);unix=int(dt.timestamp())
        if unix <= int(datetime.now(timezone.utc).timestamp()):
            await i.response.send_message("\u274c Scrims cannot be scheduled in the past. Please choose a future time.",ephemeral=True)
            return
    except:await i.response.send_message("\u274c Try: 20:00, 20.00, 8pm, 8:30pm",ephemeral=True);return
    await i.response.defer(ephemeral=True)
    if not has_scrims(i.guild):await i.followup.send("\u274c Run `/scrimbot setup` first.",ephemeral=True);return
    gid=str(i.guild_id);max_p=4;today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title="Mixed Scrim - 2V2"
    msc=None
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            for ch in cat.text_channels:
                if ch.name=="mixed-scrims":msc=ch;break
    if not msc:await i.response.send_message("\u274c No #mixed-scrims channel.",ephemeral=True);return
    embed=discord.Embed(title=title,description=f"*{max_p} spots | Click Sign Up!*",color=0x5865F2)
    embed.add_field(name="Signed Up",value=f"0/{max_p}",inline=True)
    embed.add_field(name="\u23f0 Time (GMT)",value=scrim_time,inline=True)
    embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
    embed.set_footer(text="Click Sign Up to join")
    sid=str(uuid.uuid4())[:8]
    view=ScrimSignupView(gid,sid);msg=await msc.send(embed=embed,view=view)
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT OR REPLACE INTO scrim_v2(sid,guild_id,date,thread_id,msg_id,max_players,scrim_title,unix_time) VALUES(?,?,?,NULL,?,?,?,?)",(sid,gid,today,str(msg.id),max_p,title,unix));await db.commit()
    # Create the scrim thread
    try:
        th=await msc.create_thread(name=f"Scrim {scrim_time} GMT - 2V2",type=discord.ChannelType.public_thread,auto_archive_duration=1440)
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE scrim_v2 SET thread_id=? WHERE sid=?",(str(th.id),sid));await db.commit()
        await update_scrim_thread(sid)
    except Exception as e:log.warning("scrim thread create: %s",e)
    await i.followup.send(f"\u2705 Scrim created for **{scrim_time} GMT**!",ephemeral=True)


bot.tree.add_command(create_grp)

# ===== /leaderboard =====
leaderboard_msg_ids={}

async def refresh_leaderboard(guild):
    gid=str(guild.id)
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS leaderboard_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT)")
        async with db.execute("SELECT ch,msg FROM leaderboard_state WHERE guild_id=?",(gid,))as cur:
            r=await cur.fetchone()
    if not r:return
    ch=guild.get_channel(int(r[0]))
    if not ch:return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    medals=["\U0001f947","\U0001f948","\U0001f949"]
    rows=[]
    for n,t in enumerate(ts[:25]):
        pos=medals[n]if n<3 else f"`{n+1}.`"
        rows.append(f"{pos} **{t['display']}** \u00b7 {t['wins']}W {t['losses']}L \u00b7 `{t['mmr']}`")
    if not ts: rows=["*No teams are currently registered.*"]
    if len(ts)>25:rows.append(f"*...and {len(ts)-25} more teams*")
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4ca {season_name} Leaderboard",description="\n".join(rows),color=0x5865F2)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Max {MAX_TEAM} players per team \u00b7 Updates automatically")
    try:
        msg=await ch.fetch_message(int(r[1]))
        await msg.edit(embed=embed)
    except:pass

@bot.tree.command(name="leaderboard",description="Post/refresh the leaderboard in #leaderboard (League Admin only)")
async def leaderboard_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    lb_ch=None
    for cat in i.guild.categories:
        if cat.name=="Matches":
            for ch in cat.text_channels:
                if ch.name=="leaderboard":lb_ch=ch;break
    if not lb_ch:await i.response.send_message("\u274c No #leaderboard channel. Run `/setup run`.",ephemeral=True);return
    if i.channel.id!=lb_ch.id:await i.response.send_message(f"\u274c Use this in {lb_ch.mention}.",ephemeral=True);return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    if not ts:await i.response.send_message("No teams yet.",ephemeral=True);return
    medals=["\U0001f947","\U0001f948","\U0001f949"]
    rows=[]
    for n,t in enumerate(ts[:25]):
        pos=medals[n]if n<3 else f"{n+1:>2}."
        name=t['display'][:15]
        rec=f"{t['wins']}W {t['losses']}L"
        rows.append(f"{pos} {name:<16}{rec:>8}  {t['mmr']:>4}")
    if len(ts)>25:rows.append(f"...and {len(ts)-25} more")
    c=await cfg_get(gid)
    season_name=c["name"]if c else"EGL"
    embed=discord.Embed(title=f"\U0001f4ca {season_name} Leaderboard",description="```\n"+"\n".join(rows)+"\n```",color=0x5865F2)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Max {MAX_TEAM} players per team \u00b7 Updates automatically")
    # Delete old message if exists
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS leaderboard_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT)")
        async with db.execute("SELECT ch,msg FROM leaderboard_state WHERE guild_id=?",(gid,))as cur:
            old=await cur.fetchone()
    if old and old[0]:
        old_ch=i.guild.get_channel(int(old[0]))
        if old_ch and old[1]:
            try:
                old_msg=await old_ch.fetch_message(int(old[1]))
                await old_msg.delete()
            except:pass
    msg=await lb_ch.send(embed=embed)
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT OR REPLACE INTO leaderboard_state VALUES(?,?,?)",(gid,str(lb_ch.id),str(msg.id)))
        await db.commit()
    await i.response.send_message("\u2705 Leaderboard posted!",ephemeral=True)

async def refresh_rosters(guild):
    gid=str(guild.id)
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS roster_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT)")
        async with db.execute("SELECT ch,msg FROM roster_state WHERE guild_id=?",(gid,))as cur:
            r=await cur.fetchone()
    if not r:return
    ch=guild.get_channel(int(r[0]))
    if not ch:return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    lines=[]
    for t in ts[:25]:
        cap_id=t["captain_id"]
        sorted_uids=sorted(t["members"],key=lambda u:0 if u==cap_id else 1)
        roster_count=len(t["members"])
        roster_status=" ⚠️ OVER LIMIT — reduce to 3" if roster_count>MAX_TEAM else ""
        lines.append(f"**{t['display']}** ({roster_count}/{MAX_TEAM}) · {get_rank(t['mmr'])}{roster_status}")
        for uid in sorted_uids:
            m=guild.get_member(int(uid))
            nm=m.display_name if m else f"<@{uid}>"
            lines.append(f"\U0001f451 {nm}"if uid==cap_id else f"\u00b7 {nm}")
        lines.append("")
    if not ts: lines=["*No teams are currently registered.*"]
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4cb {season_name} Teams",description="\n".join(lines),color=0x57F287)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Max {MAX_TEAM} players per team \u00b7 Updates automatically")
    try:
        msg=await ch.fetch_message(int(r[1]))
        await msg.edit(embed=embed)
    except:pass

@bot.tree.command(name="teaminfoall",description="Post/refresh the team directory in #teams (League Admin only)")
async def teaminfoall_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    teams_ch=None
    c=await cfg_get(gid)
    if c and c.get("teams_ch"):
        teams_ch=i.guild.get_channel(int(c["teams_ch"]))
    if teams_ch is None:
        teams_ch=discord.utils.get(i.guild.text_channels,name="teams")
    if not teams_ch:await i.response.send_message("\u274c No #teams channel. Run `/setup run` first.",ephemeral=True);return
    if i.channel.id!=teams_ch.id:await i.response.send_message(f"\u274c Use this command in {teams_ch.mention}.",ephemeral=True);return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    lines=[]
    for t in ts[:25]:
        cap_id=t["captain_id"]
        sorted_uids=sorted(t["members"],key=lambda u:0 if u==cap_id else 1)
        roster_count=len(t["members"])
        roster_status=" ⚠️ OVER LIMIT — reduce to 3" if roster_count>MAX_TEAM else ""
        lines.append(f"**{t['display']}** ({roster_count}/{MAX_TEAM}) · {get_rank(t['mmr'])}{roster_status}")
        for uid in sorted_uids:
            m=i.guild.get_member(int(uid))
            nm=m.display_name if m else f"<@{uid}>"
            lines.append(f"\U0001f451 {nm}"if uid==cap_id else f"· {nm}")
        lines.append("")
    if not ts:lines=["*No teams are currently registered.*"]
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4cb {season_name} Teams",description="\n".join(lines),color=0x57F287)
    embed.set_footer(text=f"{len(ts)} teams · Max {MAX_TEAM} players per team · Updates automatically")
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS roster_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT)")
        async with db.execute("SELECT ch,msg FROM roster_state WHERE guild_id=?",(gid,))as cur:old=await cur.fetchone()
    if old and old[0]:
        old_ch=i.guild.get_channel(int(old[0]))
        if old_ch and old[1]:
            try:
                old_msg=await old_ch.fetch_message(int(old[1]))
                await old_msg.delete()
            except:pass
    msg=await teams_ch.send(embed=embed)
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT OR REPLACE INTO roster_state VALUES(?,?,?)",(gid,str(teams_ch.id),str(msg.id)))
        await db.commit()
    await i.response.send_message(f"\u2705 Team directory posted in {teams_ch.mention}.",ephemeral=True)

@tasks.loop(minutes=1)
async def leaderboard_refresh():
    for g in bot.guilds:
        await refresh_leaderboard(g)
        await refresh_rosters(g)
@leaderboard_refresh.before_loop
async def lb_bef():await bot.wait_until_ready()

@bot.tree.command(name="backup",description="Download league database backup (League Admin only)")
async def backup_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    await i.response.defer(ephemeral=True)
    try:
        file=discord.File(DB,filename="league_backup.db")
        await i.user.send("\U0001f4be **EGL League Database Backup**\nThe attached file contains all your league data: teams, ranks, matches, and settings.\n\n**How to restore after a Railway redeploy:**\n1. Wait for the bot to come back online\n2. Use `/restore` and attach this file\n3. Everything will be restored instantly!\n\n**Tip:** Always `/backup` before pushing new code to GitHub.",file=file)
        await i.followup.send("\u2705 Backup sent to your DMs!",ephemeral=True)
    except:await i.followup.send("\u274c Couldn\u2019t DM you. Enable DMs from server members.",ephemeral=True)

@bot.tree.command(name="restore",description="Restore the league database (League Admin only)")
@app_commands.describe(file="The league_backup.db file")
async def restore_cmd(i,file:discord.Attachment):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    if not file.filename.endswith(".db"):await i.response.send_message("\u274c Must be a .db file.",ephemeral=True);return
    await i.response.defer(ephemeral=True)
    await file.save(DB)
    await init_db()
    await i.followup.send("\u2705 Database restored!",ephemeral=True)

# ===== Scheduler =====
@tasks.loop(hours=1)
async def weekly_check():
    now=datetime.now(timezone.utc)

    # Normal target: Sunday 22:00 GMT.
    # Recovery window: Sunday 22:00 through Monday 04:00 GMT.
    if now.weekday()==6 and now.hour>=22:
        cycle_date=now.date()
    elif now.weekday()==0 and now.hour<=4:
        cycle_date=now.date()-timedelta(days=1)
    else:
        return

    cycle_key=cycle_date.isoformat()

    for g in bot.guilds:
        gid=str(g.id)
        c=await cfg_get(gid)
        if not c:continue

        async with aiosqlite.connect(DB) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute(
                "SELECT weeks_done,last_generation_cycle FROM season WHERE guild_id=?",
                (gid,)
            ) as cur:
                season=await cur.fetchone()

        if not season:continue

        # If this Sunday's generation has already happened, do nothing.
        if season["last_generation_cycle"]==cycle_key:
            continue

        await gen_matches(g,c,force=False)
@weekly_check.before_loop
async def bef():await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def scrim_check():
    now=datetime.now(timezone.utc)
    hour=now.hour;minute=now.minute
    for g in bot.guilds:
        gid=str(g.id)
        # Expire timed scrim bans even when the guild currently has no Scrims category.
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id FROM scrim_bans WHERE guild_id=? AND expires_at IS NOT NULL AND expires_at<=?",
                (gid,int(now.timestamp()))
            ) as cur:
                expired=[row[0] for row in await cur.fetchall()]
        if expired:
            ban_role=find_role(g,SCRIM_BAN_ROLE)
            for expired_uid in expired:
                member=g.get_member(int(expired_uid))
                if member and ban_role and ban_role in member.roles:
                    try:
                        await member.remove_roles(ban_role,reason="Timed scrim ban expired")
                    except Exception as e:
                        log.warning("Could not expire scrim ban for %s in %s: %s",expired_uid,gid,e)
                        continue
                async with aiosqlite.connect(DB) as db:
                    await db.execute("DELETE FROM scrim_bans WHERE guild_id=? AND user_id=?",(gid,expired_uid))
                    await db.commit()
        msc=None
        for cat in g.categories:
            if cat.name=="Scrims":
                for ch in cat.text_channels:
                    if ch.name=="mixed-scrims":msc=ch;break
        if not msc:continue
        if not has_scrims(g):continue

        # Every 5 min - refresh embeds for every still-active scrim.
        # Do not filter by calendar date: a scrim's SID/unix_time is its identity.
        if minute%5==0:
            for s in await all_scrims(gid):
                await update_scrim_embed(s["sid"])

        # 5-min ping and auto-close, bound to each scrim's unique SID/time.
        for sess in await all_scrims(gid):
            sid=sess["sid"]
            if not sess.get("unix_time"):continue
            dt=datetime.fromtimestamp(sess["unix_time"],tz=timezone.utc)
            diff=(dt-now).total_seconds()
            if 0<diff<=300 and not sess.get("pinged"):
                members=await scrim_signups_active(sid)
                if members:
                    pings=" ".join(f"<@{uid}>"for uid in members)
                    txt=f"\U0001f514 **Scrim starting in 5 minutes!**\n\nGet a lobby ready and drop the code in this chat.\n{pings}"
                    th=g.get_thread(int(sess["thread_id"]))if sess.get("thread_id")else None
                    if th:
                        try:await th.send(txt)
                        except:pass
                    else:
                        await msc.send(txt)
                async with aiosqlite.connect(DB)as db:
                    await db.execute("UPDATE scrim_v2 SET pinged=1 WHERE sid=?",(sid,));await db.commit()
            retry_at=sess.get("cleanup_retry_at")
            if diff<=-5400 and (not retry_at or int(retry_at)<=int(now.timestamp())):
                await close_scrim(sid,sess)
                continue
        # Midnight cleanup
        if hour==0 and minute==0:
            async with aiosqlite.connect(DB)as db:
                await db.execute("DELETE FROM scrim_signups_v2 WHERE guild_id=? AND sid IN (SELECT sid FROM scrim_v2 WHERE guild_id=? AND date<?)",(gid,gid,today))
                await db.execute("DELETE FROM scrim_v2 WHERE guild_id=? AND date<?",(gid,today));await db.commit()

@scrim_check.before_loop
async def scrim_bef():await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def match_reminders():
    now=datetime.now(timezone.utc)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE scheduled IS NOT NULL "
            "AND winner IS NULL AND score IS NULL"
        )as c:
            rows=await c.fetchall()

    for row in rows:
        row=dict(row)
        sched=row.get("scheduled")
        if not sched:continue

        try:
            # Schedules are stored as "DD Mon HH:MM GMT". Include the year
            # explicitly so Python 3.13 does not warn about yearless dates.
            dt=datetime.strptime(
                f"{now.year} {sched.replace(' GMT','')}",
                "%Y %d %b %H:%M"
            ).replace(tzinfo=timezone.utc)
        except Exception as e:
            log.warning("Could not parse scheduled match %s (%s): %s",row.get("id"),sched,e)
            continue

        diff=(dt-now).total_seconds()

        # Normal case: send around 1 hour before the match.
        # Restart-safe case: if the bot was offline during that exact window,
        # send the reminder when it comes back online, as long as the match
        # has not started and is no more than 1 hour overdue.
        if not (0 <= diff <= 3660 or -3600 < diff < 0):
            continue
        if row.get("reminder_sent",0):
            continue

        g=bot.get_guild(int(row["guild_id"]))
        if not g:continue

        th=None
        if row.get("thread_id"):
            try:th=g.get_thread(int(row["thread_id"]))
            except:th=None
            if th is None:
                try:th=await g.fetch_channel(int(row["thread_id"]))
                except Exception as e:
                    log.warning("Could not fetch match thread %s: %s",row["thread_id"],e)
                    th=None
        if not th:continue

        unix=int(dt.timestamp())
        overseer_ping=""
        overseer_id=row.get("overseer_id")
        if overseer_id:
            try:
                overseer_member=g.get_member(int(overseer_id))
                if overseer_member:
                    overseer_ping=f" {overseer_member.mention}"
            except Exception:
                pass

        try:
            minutes_remaining=max(1,int(diff // 60))
            await th.send(
                f"⏰ **Match starts in {minutes_remaining} minutes!** <t:{unix}:f>{overseer_ping}\n\n"
                "Both teams be ready! Use `/match result` after the match."
            )
            async with aiosqlite.connect(DB)as db2:
                await db2.execute(
                    "UPDATE matches SET reminder_sent=1 WHERE id=? AND reminder_sent=0",
                    (row["id"],)
                )
                await db2.commit()
        except Exception as e:
            log.warning("Match reminder failed for %s: %s",row["id"],e)

@match_reminders.before_loop
async def mr_bef():await bot.wait_until_ready()

@bot.event
async def on_guild_join(guild):
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    required=[
        (ADMIN_ROLE,discord.Color.red()),
        ("Captain",discord.Color.yellow()),
        ("Player",discord.Color.green()),
        ("TeamScrims",discord.Color.orange()),
        (TESTER_ROLE,discord.Color.from_rgb(255,105,180)),
        (SCRIM_BAN_ROLE,discord.Color.red()),
    ]
    for role_name,color in required:
        if not find_role(guild,role_name):
            try:
                r=await guild.create_role(name=role_name,color=color)
            except:pass
    # Spoon role: pingable by everyone
    if not find_role(guild,SPOON_ROLE):
        try:await guild.create_role(name=SPOON_ROLE,color=discord.Color.from_rgb(200,200,200),mentionable=True)
        except:pass
    else:
        try:
            sr=find_role(guild,SPOON_ROLE)
            if not sr.mentionable:await sr.edit(mentionable=True)
        except:pass
    log.info("\u2705 Joined %s, commands synced, roles created",guild.name)

@bot.event
async def on_member_remove(member):
    gid=str(member.guild.id);uid=str(member.id)
    t=await team_by_player(gid,uid)
    if not t:return
    was_cap=t["captain_id"]==uid
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM members WHERE guild_id=? AND team_name=? AND user_id=?",(gid,t["name"],uid))
        await db.commit()
    # If they were captain, promote the next member
    if was_cap:
        t2=await team_get(gid,t["name"])
        if t2 and t2["members"]:
            ncap=str(t2["members"][0])
            async with aiosqlite.connect(DB)as db:
                await db.execute("UPDATE teams SET captain_id=? WHERE guild_id=? AND name=?",(ncap,gid,t["name"]));await db.commit()
            nm=member.guild.get_member(int(ncap))
            cr=find_role(member.guild,"Captain")
            if nm and cr:
                try:await nm.add_roles(cr)
                except:pass
        else:
            async with aiosqlite.connect(DB)as db:
                await db.execute("DELETE FROM teams WHERE guild_id=? AND name=?",(gid,t["name"]));await db.commit()
    await refresh_leaderboard(member.guild)
    await refresh_rosters(member.guild)

@bot.event
async def on_ready():
    await init_db()
    await g_init_db()
    # Restore only still-open, not-yet-confirmed Underdog menus. The DB match
    # row is checked again on every interaction, so stale/deleted matches fail closed.
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(
            "SELECT u.match_id,u.announcement_message_id,u.selected_ultimate "
            "FROM underdog_bans u JOIN matches m ON m.id=u.match_id AND m.guild_id=u.guild_id "
            "WHERE u.banned_ultimate IS NULL AND u.announcement_message_id IS NOT NULL AND m.winner IS NULL"
        ) as cur:
            pending_underdogs = await cur.fetchall()
    for item in pending_underdogs:
        mid = str(item["match_id"])
        if mid not in _underdog_views_registered:
            bot.add_view(UnderdogBanView(mid,item["selected_ultimate"]), message_id=int(item["announcement_message_id"]))
            _underdog_views_registered.add(mid)
    bot.add_view(GRegistrationView())
    pending_g=await g_db("SELECT match_no,winner,score,reporter FROM gounament_matches WHERE status='awaiting_confirmation'",fetch=True,many=True)
    for gm in pending_g:
        if gm['winner'] and gm['score'] and gm['reporter']:
            bot.add_view(GConfirmResultView(gm['match_no'],gm['winner'],gm['score'],gm['reporter']))
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    log.info("\u2705 %s | synced to %d guild(s)",bot.user,len(bot.guilds))
    weekly_check.start()
    scrim_check.start()
    match_reminders.start()
    leaderboard_refresh.start()


# ========================= GOUNAMENT (1v1 TOURNAMENT) =========================
# Isolated tournament data: never touches teams, MMR, season or regular matches.
import math
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    # Pillow is optional. The Gounament bracket falls back to a text embed.
    Image = ImageDraw = ImageFont = None
    log.warning("Pillow is not installed; Gounament brackets will use a text-only layout.")

# Gounament tables are initialized once per process. The lazy check in g_db
# also protects against startup-order differences and older deployments.
_g_db_initialized=False
_g_db_init_lock=asyncio.Lock()

async def g_db(sql, args=(), fetch=False, many=False):
    # Ensure the Gounament schema on the SAME SQLite connection used by the
    # query. This avoids stale process-level initialization flags and protects
    # against a missing table if the database file was recreated/replaced.
    async with aiosqlite.connect(DB) as db:
        db.row_factory=aiosqlite.Row
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS gounament_state(guild_id TEXT PRIMARY KEY,name TEXT,prize1 TEXT,prize2 TEXT,prize3 TEXT,info TEXT,status TEXT,category_id TEXT,role_id TEXT,registration_ch TEXT,host_ch TEXT,chat_ch TEXT,bracket_ch TEXT,players_ch TEXT,results_ch TEXT,registration_msg TEXT,bracket_msg TEXT,current_round INTEGER DEFAULT 0,created_at TEXT);
        CREATE TABLE IF NOT EXISTS gounament_players(guild_id TEXT,user_id TEXT,signup_msg TEXT,seed INTEGER,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS gounament_matches(guild_id TEXT,match_no INTEGER,round_no INTEGER,p1 TEXT,p2 TEXT,winner TEXT,score TEXT,status TEXT DEFAULT 'pending',thread_id TEXT,reporter TEXT,dispute_reason TEXT,bracket_pos INTEGER,PRIMARY KEY(guild_id,match_no));
        CREATE TABLE IF NOT EXISTS gounament_slots(guild_id TEXT,round_no INTEGER,slot_no INTEGER,user_id TEXT,PRIMARY KEY(guild_id,round_no,slot_no));
        """)
        match_columns = await (await db.execute("PRAGMA table_info(gounament_matches)")).fetchall()
        if not any(row[1] == 'bracket_pos' for row in match_columns):
            await db.execute("ALTER TABLE gounament_matches ADD COLUMN bracket_pos INTEGER")
        cur=await db.execute(sql,args)
        rows=await cur.fetchall() if fetch and many else ([await cur.fetchone()] if fetch else None)
        await db.commit()
        return [dict(x) for x in rows] if many and fetch else (dict(rows[0]) if fetch and rows and rows[0] else None)

async def g_init_db():
    global _g_db_initialized
    if _g_db_initialized:
        return
    async with _g_db_init_lock:
        if _g_db_initialized:
            return
        async with aiosqlite.connect(DB) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS gounament_state(guild_id TEXT PRIMARY KEY,name TEXT,prize1 TEXT,prize2 TEXT,prize3 TEXT,info TEXT,status TEXT,category_id TEXT,role_id TEXT,registration_ch TEXT,host_ch TEXT,chat_ch TEXT,bracket_ch TEXT,players_ch TEXT,results_ch TEXT,registration_msg TEXT,bracket_msg TEXT,current_round INTEGER DEFAULT 0,created_at TEXT);
            CREATE TABLE IF NOT EXISTS gounament_players(guild_id TEXT,user_id TEXT,signup_msg TEXT,seed INTEGER,PRIMARY KEY(guild_id,user_id));
            CREATE TABLE IF NOT EXISTS gounament_matches(guild_id TEXT,match_no INTEGER,round_no INTEGER,p1 TEXT,p2 TEXT,winner TEXT,score TEXT,status TEXT DEFAULT 'pending',thread_id TEXT,reporter TEXT,dispute_reason TEXT,bracket_pos INTEGER,PRIMARY KEY(guild_id,match_no));
            CREATE TABLE IF NOT EXISTS gounament_slots(guild_id TEXT,round_no INTEGER,slot_no INTEGER,user_id TEXT,PRIMARY KEY(guild_id,round_no,slot_no));
            """)
            match_columns = await (await db.execute("PRAGMA table_info(gounament_matches)")).fetchall()
            if not any(row[1] == 'bracket_pos' for row in match_columns):
                await db.execute("ALTER TABLE gounament_matches ADD COLUMN bracket_pos INTEGER")
            await db.commit()
        _g_db_initialized=True

async def g_state(gid): return await g_db("SELECT * FROM gounament_state WHERE guild_id=?",(str(gid),),True)
async def g_admin(i):
    if not is_admin(i.user):
        await i.response.send_message(f"❌ Need **{ADMIN_ROLE}**.",ephemeral=True); return False
    return True
async def g_channel(guild, field):
    s=await g_state(guild.id)
    return guild.get_channel(int(s[field])) if s and s.get(field) else None
async def g_host_announce(guild, message):
    """Post tournament lifecycle updates in the configured #host channel."""
    ch = await g_channel(guild, 'host_ch')
    if ch:
        try:
            await ch.send(message, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            log.warning('Could not post Gounament host announcement: %s', e)

async def g_update_registration(guild):
    s=await g_state(guild.id)
    if not s:return
    ch=guild.get_channel(int(s['registration_ch'])) if s['registration_ch'] else None
    if not ch or not s['registration_msg']:return
    players=await g_db("SELECT COUNT(*) AS n FROM gounament_players WHERE guild_id=?",(str(guild.id),),True)
    try:
        msg=await ch.fetch_message(int(s['registration_msg']))
        emb=msg.embeds[0] if msg.embeds else discord.Embed(title='Gounament Registration')
        emb.description=f"**Gounament:** {s['name']}\n**1st Prize:** {s['prize1']}\n**2nd Prize:** {s['prize2']}\n**3rd Prize:** {s['prize3']}\n\n**Rules / Info**\n{s['info']}\n\n**Participants:** {players['n']}"
        await msg.edit(embed=emb,view=GRegistrationView(s['status']=='registration'))
    except Exception as e:log.warning('Gounament registration refresh: %s',e)

class GRegistrationView(discord.ui.View):
    def __init__(self, enabled=True):
        super().__init__(timeout=None)
        for c in self.children:c.disabled=not enabled
    @discord.ui.button(label='Register',style=discord.ButtonStyle.success,custom_id='gounament:register')
    async def register(self,i,b):
        s=await g_state(i.guild_id)
        if not s or s['status']!='registration':return await i.response.send_message('Registration is closed.',ephemeral=True)
        role=i.guild.get_role(int(s['role_id'])) if s['role_id'] else None
        if not role:return await i.response.send_message('Gourny role missing. Ask a League Admin.',ephemeral=True)
        old=await g_db('SELECT user_id FROM gounament_players WHERE guild_id=? AND user_id=?',(str(i.guild_id),str(i.user.id)),True)
        if old:return await i.response.send_message('You are already registered.',ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:await i.user.add_roles(role,reason='Gounament registration')
        except Exception as e:return await i.followup.send(f'Could not assign Gourny role: {e}',ephemeral=True)
        pch=await g_channel(i.guild,'players_ch')
        try:sm=await pch.send(f'{i.user.mention} has signed up')
        except Exception as e:
            await i.user.remove_roles(role,reason='Gounament signup rollback')
            return await i.followup.send(f'Could not save signup message: {e}',ephemeral=True)
        await g_db('INSERT INTO gounament_players(guild_id,user_id,signup_msg) VALUES(?,?,?)',(str(i.guild_id),str(i.user.id),str(sm.id)))
        await g_update_registration(i.guild)
        await i.followup.send('You are registered for the Gounament.',ephemeral=True)
    @discord.ui.button(label='Leave',style=discord.ButtonStyle.danger,custom_id='gounament:leave')
    async def leave(self,i,b):
        s=await g_state(i.guild_id)
        if not s:return await i.response.send_message('No active Gounament.',ephemeral=True)
        p=await g_db('SELECT signup_msg FROM gounament_players WHERE guild_id=? AND user_id=?',(str(i.guild_id),str(i.user.id)),True)
        if not p:return await i.response.send_message('You are not registered.',ephemeral=True)
        await i.response.defer(ephemeral=True)
        role=i.guild.get_role(int(s['role_id'])) if s['role_id'] else None
        if role:
            try:await i.user.remove_roles(role,reason='Gounament leave')
            except Exception:pass
        ch=await g_channel(i.guild,'players_ch')
        try:await ch.get_partial_message(int(p['signup_msg'])).delete()
        except Exception:pass
        await g_db('DELETE FROM gounament_players WHERE guild_id=? AND user_id=?',(str(i.guild_id),str(i.user.id)))
        await g_update_registration(i.guild)
        await i.followup.send('You have left the Gounament.',ephemeral=True)

class GSetupModal(discord.ui.Modal,title='Set up a Gounament'):
    name=discord.ui.TextInput(label='Gounament Name',max_length=80)
    prize1=discord.ui.TextInput(label='1st Prize',max_length=150)
    prize2=discord.ui.TextInput(label='2nd Prize',max_length=150)
    prize3=discord.ui.TextInput(label='3rd Prize',max_length=150)
    info=discord.ui.TextInput(label='Gounament Rules / Info',style=discord.TextStyle.paragraph,max_length=1800)
    async def on_submit(self,i):
        if not is_admin(i.user):return await i.response.send_message('League Admin only.',ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        gid=str(i.guild_id)
        old=await g_state(gid)
        if old and old['status'] not in ('ended','reset'):
            return await i.followup.send('There is already an active Gounament. Use `/tournament end` first.',ephemeral=True)
        gcat=discord.utils.get(i.guild.categories,name='Gounament')
        if gcat: await gcat.delete(reason='Replacing previous Gounament category')
        role=discord.utils.get(i.guild.roles,name='Gourny')
        if role: await role.delete(reason='Replacing previous Gounament role')
        role=await i.guild.create_role(name='Gourny',reason='Gounament participant role')
        everyone=i.guild.default_role; admin=discord.utils.get(i.guild.roles,name=ADMIN_ROLE); goon=find_role(i.guild,'Regular Goon'); me=i.guild.me
        base={everyone:discord.PermissionOverwrite(view_channel=False,create_public_threads=False,create_private_threads=False),me:discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True,manage_messages=True,create_private_threads=True,create_public_threads=True)}
        # Regular Goon must be explicitly denied: a previous explicit allow
        # on this role overrides the @everyone denial and exposes the channels.
        if goon:base[goon]=discord.PermissionOverwrite(view_channel=False,send_messages=False,read_message_history=False,create_public_threads=False,create_private_threads=False)
        if admin:base[admin]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_messages=True,create_public_threads=True,create_private_threads=True)
        cat=await i.guild.create_category('Gounament',overwrites=base)
        def perms(kind):
            d=dict(base);d[role]=discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=kind=='chat',send_messages_in_threads=True,create_public_threads=False,create_private_threads=False)
            if goon:
                if kind in ('registration','host'):
                    # Regular Goons can read registration and host history,
                    # but cannot post or create threads there.
                    d[goon]=discord.PermissionOverwrite(view_channel=True,send_messages=False,read_message_history=True,create_public_threads=False,create_private_threads=False)
                else:
                    d[goon]=discord.PermissionOverwrite(view_channel=False,send_messages=False,read_message_history=False,create_public_threads=False,create_private_threads=False)
            if admin:d[admin]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_messages=True,create_public_threads=True,create_private_threads=True)
            return d
        channels={}
        for nm,kind in [('registration','registration'),('host','host'),('chat','chat'),('bracket','readonly'),('players','readonly'),('results','readonly')]:
            channels[nm]=await i.guild.create_text_channel(nm,category=cat,overwrites=perms(kind))
        # Clear previous results only after new channel is ready.
        if old and old.get('results_ch'):
            prev=i.guild.get_channel(int(old['results_ch']))
            if prev:
                try:
                    async for m in prev.history(limit=None):await m.delete()
                except Exception as e:log.warning('Could not clear old Gounament results: %s',e)
        await g_db('DELETE FROM gounament_players WHERE guild_id=?',(gid,))
        await g_db('DELETE FROM gounament_matches WHERE guild_id=?',(gid,))
        await g_db('DELETE FROM gounament_slots WHERE guild_id=?',(gid,))
        await g_db('''INSERT OR REPLACE INTO gounament_state(guild_id,name,prize1,prize2,prize3,info,status,category_id,role_id,registration_ch,host_ch,chat_ch,bracket_ch,players_ch,results_ch,registration_msg,bracket_msg,current_round,created_at) VALUES(?,?,?,?,?,?,'registration',?,?,?,?,?,?,?,?,NULL,NULL,0,?)''',(gid,str(self.name),str(self.prize1),str(self.prize2),str(self.prize3),str(self.info),str(cat.id),str(role.id),str(channels['registration'].id),str(channels['host'].id),str(channels['chat'].id),str(channels['bracket'].id),str(channels['players'].id),str(channels['results'].id),datetime.now(timezone.utc).isoformat()))
        s=await g_state(gid)
        emb=discord.Embed(title='Gounament Registration',color=0x5865F2,description=f"**Gounament:** {s['name']}\n**1st Prize:** {s['prize1']}\n**2nd Prize:** {s['prize2']}\n**3rd Prize:** {s['prize3']}\n\n**Rules / Info**\n{s['info']}\n\n**Participants:** 0")
        msg=await channels['registration'].send(embed=emb,view=GRegistrationView())
        await g_db('UPDATE gounament_state SET registration_msg=? WHERE guild_id=?',(str(msg.id),gid))
        await i.followup.send('Gounament created. Registration is open. Channels and Gourny role are ready.',ephemeral=True)

# Persistent tournament UI is restored after restart.

def g_bracket_name(value):
    """Normalize decorative Unicode names and remove invisible formatting controls."""
    value=unicodedata.normalize('NFKC',str(value or ''))
    cleaned=[]
    for char in value:
        category=unicodedata.category(char)
        # Strip zero-width joiners, bidi controls, variation selectors and other
        # invisible format characters that can render as tofu or affect layout.
        if category in ('Cf','Cc') or 0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF:
            continue
        cleaned.append(char)
    value=''.join(cleaned).strip()
    return value or 'Unknown player'


async def g_publish_bracket(guild):
    s=await g_state(guild.id)
    if not s:return
    rows=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? ORDER BY round_no,match_no',(str(guild.id),),True,True)
    if not rows:return
    rounds=max(r['round_no'] for r in rows)
    ch=guild.get_channel(int(s['bracket_ch']))
    if not ch:return

    byround={}
    for r in rows:
        if r['round_no']==1 and (not r['p1'] or not r['p2']):
            continue
        byround.setdefault(r['round_no'],[]).append(r)
    for rn in byround:
        byround[rn].sort(key=lambda row: (row['bracket_pos'] if row['bracket_pos'] is not None else row['match_no']))

    def bracket_names(rn, match_index, m):
        p1=guild.get_member(int(m['p1'])) if m['p1'] else None
        p2=guild.get_member(int(m['p2'])) if m['p2'] else None
        previous=byround.get(rn-1, [])
        # Every match is fed by the winners of two adjacent matches in the
        # previous round. Bye advances occupy their original feeder position.
        bracket_index=int(m['bracket_pos'] if m['bracket_pos'] is not None else match_index)
        source_indexes=(bracket_index*2, bracket_index*2+1)
        def incoming_name(player_id, source_index):
            if player_id:
                member=guild.get_member(int(player_id))
                return g_bracket_name(member.display_name if member else f"Player {player_id}")
            if rn > 1:
                source=next((row for row in previous if int(row['bracket_pos'] or 0)==source_index),None)
                if source:
                    return f"Winner of match #{int(source['match_no']):02d}"
            return "TBD"
        a=incoming_name(m['p1'], source_indexes[0])
        b=incoming_name(m['p2'], source_indexes[1])
        loser=None
        if m['winner']:
            loser=m['p2'] if str(m['winner'])==str(m['p1']) else m['p1']
        return a,b

    # Keep the bot operational on hosts that do not have Pillow installed.
    if Image is None:
        lines=[]
        for rn, matches in sorted(byround.items()):
            lines.append(f"**Round {rn}**")
            for ix,m in enumerate(matches):
                a,b=bracket_names(rn,ix,m)
                if m['winner']:
                    if str(m['winner'])==str(m['p1']): b=f"~~{b}~~"
                    elif str(m['winner'])==str(m['p2']): a=f"~~{a}~~"
                winner=guild.get_member(int(m['winner'])) if m['winner'] else None
                status=f" — **{winner.display_name} wins ({m['score']})**" if winner else (" — **DISPUTED**" if m['status']=='disputed' else " — Pending")
                lines.append(f"`#{m['match_no']:02d}` {a} vs {b}{status}")
            lines.append("")
        embed=discord.Embed(title="🏆 Gounament Bracket",description="\n".join(lines)[:4000],color=0x5865F2)
        try:
            if s['bracket_msg']:
                msg=await ch.fetch_message(int(s['bracket_msg']))
                await msg.edit(content=None,embed=embed,attachments=[])
            else:
                msg=await ch.send(embed=embed)
                await g_db('UPDATE gounament_state SET bracket_msg=? WHERE guild_id=?',(str(msg.id),str(guild.id)))
        except discord.NotFound:
            msg=await ch.send(embed=embed)
            await g_db('UPDATE gounament_state SET bracket_msg=? WHERE guild_id=?',(str(msg.id),str(guild.id)))
        return

    # Larger, heavier typography so player names remain readable in Discord previews.
    # DejaVu Sans Bold is the primary font because it is commonly available on Linux hosts.
    try:
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)
        small_font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',16)
        header_font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',20)
    except Exception:
        try:
            font=ImageFont.truetype('/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',28)
            small_font=ImageFont.truetype('/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',16)
            header_font=ImageFont.truetype('/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',20)
        except Exception:
            font=small_font=header_font=ImageFont.load_default()

    # Detect missing glyphs in the selected font. Unsupported symbols are
    # transliterated where possible, otherwise replaced with a visible '?'
    # instead of Discord previews showing unreadable tofu squares.
    missing_glyph=bytes(font.getmask(chr(0x10FFFF)))
    def renderable_name(value):
        value=g_bracket_name(value)
        output=[]
        for char in value:
            if bytes(font.getmask(char)) != missing_glyph:
                output.append(char)
                continue
            decomposed=unicodedata.normalize('NFKD',char)
            replacement=''.join(c for c in decomposed
                                if not unicodedata.combining(c)
                                and bytes(font.getmask(c)) != missing_glyph)
            output.append(replacement or '?')
        return ''.join(output)

    card_w=430
    col_gap=65
    width=max(980, 90 + rounds*(card_w+col_gap))
    max_matches=max(len(v) for v in byround.values())
    card_h=92
    vertical_gap=24
    height=max(460, 105 + max_matches*(card_h+vertical_gap))
    im=Image.new('RGB',(width,height),(25,27,33))
    d=ImageDraw.Draw(im)

    def fit_to_width(value, available, active_font):
        value=renderable_name(value)
        if d.textlength(value,font=active_font)<=available:
            return value
        ellipsis='…'
        while value and d.textlength(value+ellipsis,font=active_font)>available:
            value=value[:-1]
        return value.rstrip()+ellipsis

    def draw_player_line(x0,y0,text_value,uid,match,slot):
        is_loser=bool(match['winner']) and uid is not None and str(uid)!=str(match['winner'])
        color=(155,160,172) if is_loser else (244,246,250)
        available=card_w-100
        shown=fit_to_width(text_value,available,font)
        d.text((x0,y0),shown,font=font,fill=color)
        if is_loser:
            # Strike through the visual center of the rendered glyphs, not a fixed y offset.
            bbox=d.textbbox((x0,y0),shown,font=font)
            strike_y=(bbox[1]+bbox[3])//2
            d.line((bbox[0],strike_y,bbox[2],strike_y),fill=(239,105,105),width=4)

    for rn,ms in sorted(byround.items()):
        x=45+(rn-1)*(card_w+col_gap)
        d.text((x,22),f'ROUND {rn}',font=header_font,fill=(239,242,248))
        step=(height-115)/(len(ms)+1)
        for ix,m in enumerate(ms):
            y=int(55+step*(ix+1))
            a,b=bracket_names(rn,ix,m)
            # Resolve IDs independently so a bye placeholder is never crossed out.
            uid_a=m['p1']
            uid_b=m['p2']
            d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=9,outline=(112,126,153),width=2,fill=(34,37,46))
            d.text((x+12,y+14),f"#{int(m['match_no']):02d}",font=small_font,fill=(166,181,210))
            draw_player_line(x+78,y+8,a,uid_a,m,'p1')
            draw_player_line(x+78,y+50,b,uid_b,m,'p2')
            if m['status']=='disputed':
                tag='DISPUTED'
                tag_w=d.textlength(tag,font=small_font)
                d.text((x+card_w-tag_w-12,y+17),tag,font=small_font,fill=(255,184,92))
    import io
    buf=io.BytesIO();im.save(buf,format='PNG');buf.seek(0)
    file=discord.File(buf,filename='gounament-bracket.png')
    try:
        if s['bracket_msg']:
            msg=await ch.fetch_message(int(s['bracket_msg']))
            await msg.edit(content='🏆 **Gounament Bracket**',attachments=[file])
        else:
            msg=await ch.send('🏆 **Gounament Bracket**',file=file)
            await g_db('UPDATE gounament_state SET bracket_msg=? WHERE guild_id=?',(str(msg.id),str(guild.id)))
    except discord.NotFound:
        msg=await ch.send('🏆 **Gounament Bracket**',file=file)
        await g_db('UPDATE gounament_state SET bracket_msg=? WHERE guild_id=?',(str(msg.id),str(guild.id)))

async def g_make_thread(guild,m):
    s=await g_state(guild.id)
    if not s or m['thread_id']:return
    ch=guild.get_channel(int(s['bracket_ch']))
    if not ch:return
    a=guild.get_member(int(m['p1']));b=guild.get_member(int(m['p2']))
    if not a or not b:return
    stage = await g_round_label(guild.id, m['round_no'])
    th=await ch.create_thread(name=f"{stage} - Match {m['match_no']:02d} - {a.display_name} vs {b.display_name}",type=discord.ChannelType.private_thread,auto_archive_duration=10080)
    admin_ids=[x.id for x in guild.members if is_admin(x)]
    for uid in [a.id,b.id]+admin_ids:
        member=guild.get_member(uid)
        if member:
            try:await add_thread_member_safely(th,member)
            except Exception:pass
    rules=discord.Embed(
        title=f'{stage} — Gounament Match #{m["match_no"]:02d}',
        color=0x5865F2,
        description=(
            f'{a.mention} vs {b.mention}\n\n'
            '**Match rules**\n'
            '• Map: Chessboard\n'
            '• Format: BO5 — first to 3 rounds wins\n'
            '• Capture Point Zone: OFF\n'
            '• Powerups: OFF\n\n'
            'Use `/1v1 result` to report the result. League Admins can use `/tournament closematch`.'
        )
    )
    await th.send(embed=rules)
    await g_db('UPDATE gounament_matches SET thread_id=? WHERE guild_id=? AND match_no=?',(str(th.id),str(guild.id),m['match_no']))

async def g_round_label(guild_id, round_no):
    """Return a player-friendly stage name based on the number of matches."""
    row = await g_db(
        "SELECT COUNT(*) AS n FROM gounament_matches WHERE guild_id=? AND round_no=? AND p1 IS NOT NULL AND p2 IS NOT NULL",
        (str(guild_id), int(round_no)), True
    )
    count = int(row['n'] or 0) if row else 0
    if count == 1:
        return 'Final'
    if count == 2:
        return 'Semi-Finals'
    return f'Round {round_no}'

async def g_delete_match_thread(guild, match):
    """Permanently delete a completed Gounament match thread."""
    raw_id = match.get('thread_id')
    if not raw_id:
        log.warning('Gounament match #%s has no saved thread_id; cannot delete its thread.', match.get('match_no'))
        return False
    thread_id = int(raw_id)
    thread = bot.get_channel(thread_id) or guild.get_thread(thread_id)
    if thread is None:
        try:
            thread = await bot.fetch_channel(thread_id)
        except discord.NotFound:
            return True  # Already deleted.
        except discord.Forbidden as e:
            log.error('Missing access to fetch completed Gounament thread %s: %s', thread_id, e)
            return False
        except Exception:
            log.exception('Could not fetch completed Gounament thread %s', thread_id)
            return False
    try:
        await thread.delete(reason=f'Gounament match #{int(match["match_no"]):02d} result finalized')
        log.info('Deleted completed Gounament match thread %s (match #%s).', thread_id, match.get('match_no'))
        return True
    except discord.NotFound:
        return True
    except discord.Forbidden as e:
        log.error('Cannot delete Gounament thread %s. Give the bot Manage Threads permission: %s', thread_id, e)
        return False
    except Exception:
        log.exception('Failed to delete completed Gounament thread %s', thread_id)
        return False

async def g_publish_final_standings(guild, final_match, champion_id):
    """Publish a clean top-10 leaderboard and announce the podium/prizes in #host."""
    s = await g_state(guild.id)
    if not s:
        return
    gid = str(guild.id)
    matches = await g_db(
        "SELECT * FROM gounament_matches WHERE guild_id=? AND p1 IS NOT NULL AND p2 IS NOT NULL AND winner IS NOT NULL ORDER BY round_no DESC, match_no",
        (gid,), True, True
    )
    if not matches:
        return

    champion_id = int(champion_id)
    final_loser = int(final_match['p2'] if str(champion_id) == str(final_match['p1']) else final_match['p1'])
    placements = [(champion_id, 1), (final_loser, 2)]
    seen = {champion_id, final_loser}
    rank = 3
    # Players eliminated in the same last non-final round share third place.
    elimination_rounds = sorted({int(m['round_no']) for m in matches if int(m['round_no']) < int(final_match['round_no'])}, reverse=True)
    for rnd in elimination_rounds:
        eliminated = []
        for match in matches:
            if int(match['round_no']) != rnd:
                continue
            loser_id = int(match['p2'] if str(match['winner']) == str(match['p1']) else match['p1'])
            if loser_id not in seen:
                seen.add(loser_id)
                eliminated.append(loser_id)
        if not eliminated:
            continue
        for uid in eliminated:
            if len(placements) >= 10:
                break
            placements.append((uid, rank))
        rank += len(eliminated)
        if len(placements) >= 10:
            break

    async def player_name(uid):
        member = guild.get_member(int(uid))
        if member is None:
            try:
                member = await guild.fetch_member(int(uid))
            except Exception:
                member = None
        return member.display_name if member else f'Unknown player ({uid})'

    names = {uid: await player_name(uid) for uid, _ in placements}
    medal = {1: '🥇', 2: '🥈', 3: '🥉'}
    leaderboard_lines = []
    for uid, position in placements:
        prefix = medal.get(position, f'**{position}.**')
        leaderboard_lines.append(f'{prefix} **{discord.utils.escape_markdown(names[uid])}**')

    embed = discord.Embed(
        title=f'🏆 {s["name"]} — Final Standings',
        description='\n'.join(leaderboard_lines),
        color=0xF1C40F
    )
    results_ch = await g_channel(guild, 'results_ch')
    if results_ch:
        await results_ch.send(embed=embed)

    # Announce the actual podium and configured prizes in the public host channel.
    third_place_ids = [uid for uid, pos in placements if pos == 3]
    first_name = discord.utils.escape_markdown(names.get(champion_id, await player_name(champion_id)))
    second_name = discord.utils.escape_markdown(names.get(final_loser, await player_name(final_loser)))
    third_names = [discord.utils.escape_markdown(names.get(uid, await player_name(uid))) for uid in third_place_ids]
    third_label = ', '.join(f'**{name}**' for name in third_names) if third_names else '—'
    host_message = (
        f'🏆 **{discord.utils.escape_markdown(s["name"])} — Tournament Results**\n\n'
        f'🥇 **1st place:** {first_name} — **{s.get("prize1") or "No prize listed"}**\n'
        f'🥈 **2nd place:** {second_name} — **{s.get("prize2") or "No prize listed"}**\n'
        f'🥉 **3rd place:** {third_label} — **{s.get("prize3") or "No prize listed"}**'
    )
    host_ch = await g_channel(guild, 'host_ch')
    if host_ch:
        await host_ch.send(host_message, allowed_mentions=discord.AllowedMentions.none())

async def g_advance(guild, m, winner, score):
    gid = str(guild.id)
    winner = str(winner)
    loser = m['p2'] if winner == str(m['p1']) else m['p1']
    if winner not in (str(m['p1']), str(m['p2'])):
        raise ValueError('Winner must be one of the two players in this match.')

    # Resolve once only. Both /1v1 result confirmation and /tournament closematch
    # use this function, so thread cleanup is shared by both result paths.
    await g_db(
        "UPDATE gounament_matches SET winner=?,score=?,status='confirmed' WHERE guild_id=? AND match_no=? AND status!='confirmed'",
        (winner, score, gid, m['match_no'])
    )
    resolved = await g_db(
        'SELECT * FROM gounament_matches WHERE guild_id=? AND match_no=?',
        (gid, m['match_no']), True
    )
    if not resolved or resolved['status'] != 'confirmed':
        raise RuntimeError(f'Could not confirm Gounament match #{m["match_no"]}.')

    # Delete before publishing anything else. A failed bracket/announcement edit
    # must never prevent the completed private thread from being removed.
    await g_delete_match_thread(guild, resolved)

    round_state = await g_db(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS done FROM gounament_matches WHERE guild_id=? AND round_no=? AND p1 IS NOT NULL AND p2 IS NOT NULL",
        (gid, m['round_no']), True
    )
    next_state = await g_db(
        'SELECT COUNT(*) AS n FROM gounament_matches WHERE guild_id=? AND round_no=?',
        (gid, int(m['round_no']) + 1), True
    )
    if round_state and round_state['total'] and round_state['total'] == (round_state['done'] or 0):
        if next_state and next_state['n']:
            await g_host_announce(guild, f'✅ **{await g_round_label(guild.id, m["round_no"])} completed.** All matches are confirmed. Waiting for a League Admin to start the next round with `/tournament nextround`.')
        else:
            await g_host_announce(guild, '🏆 **The final match is complete!** Final tournament placements and prizes are being announced below.')

    next_round = int(m['round_no']) + 1
    next_rows = await g_db(
        'SELECT match_no,p1,p2,bracket_pos FROM gounament_matches WHERE guild_id=? AND round_no=? ORDER BY bracket_pos',
        (gid, next_round), True, True
    )
    if next_rows:
        source_index = int(m['bracket_pos'] if m['bracket_pos'] is not None else 0)
        target_index = source_index // 2
        target = next((row for row in next_rows if int(row['bracket_pos'] if row['bracket_pos'] is not None else -1) == target_index), None)
        if target:
            slot = 'p1' if source_index % 2 == 0 else 'p2'
            await g_db(f'UPDATE gounament_matches SET {slot}=? WHERE guild_id=? AND match_no=?', (winner, gid, target['match_no']))

    results_ch = await g_channel(guild, 'results_ch')
    if results_ch:
        player_a = guild.get_member(int(m['p1']))
        player_b = guild.get_member(int(m['p2']))
        winner_member = guild.get_member(int(winner))
        loser_member = guild.get_member(int(loser))
        round_label = await g_round_label(guild.id, m['round_no'])
        await results_ch.send(
            f'**Match #{int(m["match_no"]):02d} — {round_label}**\n'
            f'{winner_member.mention if winner_member else winner} defeated '
            f'{loser_member.mention if loser_member else loser} ({score})'
        )

    await g_publish_bracket(guild)
    if not (next_state and next_state['n']):
        await g_publish_final_standings(guild, resolved, winner)
        await g_db("UPDATE gounament_state SET status='ended' WHERE guild_id=?", (gid,))

async def g_player_labels(guild, match):
    labels=[]
    for uid in (match['p1'],match['p2']):
        member=guild.get_member(int(uid))
        if member is None:
            try: member=await guild.fetch_member(int(uid))
            except Exception: member=None
        labels.append(member.display_name if member else f'User {uid}')
    return labels

class GWinnerSelect(discord.ui.Select):
    def __init__(self,match,labels):
        self.match=match
        super().__init__(placeholder='Choose the winner',options=[discord.SelectOption(label=labels[0][:100],value=match['p1']),discord.SelectOption(label=labels[1][:100],value=match['p2'])])
    async def callback(self,i):
        if str(i.user.id) not in (self.match['p1'],self.match['p2']):return await i.response.send_message('Only match participants can report.',ephemeral=True)
        winner=self.values[0]
        await i.response.edit_message(content='Winner selected. Now choose the score (winner first).',view=GScoreSelectView(self.match,winner))
class GScoreSelect(discord.ui.Select):
    def __init__(self,match,winner):
        self.match=match;self.winner=winner
        super().__init__(placeholder='Choose the score (winner first)',options=[discord.SelectOption(label=x,value=x) for x in ['3-0','3-1','3-2']])
    async def callback(self,i):
        score=self.values[0]
        # Confirmation must go to the other participant, not the loser.
        # The reporter is the person who invoked /1v1 result; the opponent
        # is determined from reporter identity, regardless of who won.
        other=self.match['p2'] if str(i.user.id)==str(self.match['p1']) else self.match['p1']
        cur=await g_db('SELECT status FROM gounament_matches WHERE guild_id=? AND match_no=?',(str(i.guild_id),self.match['match_no']),True)
        if not cur or cur['status']!='pending':
            return await i.response.edit_message(content='This match is no longer accepting a result.',view=None)
        await g_db("UPDATE gounament_matches SET reporter=?,score=?,winner=?,status='awaiting_confirmation' WHERE guild_id=? AND match_no=? AND status='pending'",(str(i.user.id),score,self.winner,str(i.guild_id),self.match['match_no']))
        member=i.guild.get_member(int(other))
        if member is None:
            try: member=await i.guild.fetch_member(int(other))
            except Exception: member=None
        view=GConfirmResultView(self.match['match_no'],self.winner,score,str(i.user.id))
        # Resolve readable names for the proposed result so the opponent knows
        # exactly what they are confirming. The score is always winner-first.
        winner_member=i.guild.get_member(int(self.winner))
        if winner_member is None:
            try: winner_member=await i.guild.fetch_member(int(self.winner))
            except Exception: winner_member=None
        loser_id=self.match['p1'] if str(self.winner)==str(self.match['p2']) else self.match['p2']
        loser_member=i.guild.get_member(int(loser_id))
        if loser_member is None:
            try: loser_member=await i.guild.fetch_member(int(loser_id))
            except Exception: loser_member=None
        winner_name=winner_member.display_name if winner_member else f'User {self.winner}'
        loser_name=loser_member.display_name if loser_member else f'User {loser_id}'
        proposed=(f'**Proposed result — Match #{int(self.match["match_no"]):02d}**\n'
                  f'**{winner_name}** defeated **{loser_name}** ({score}, winner first).\n\n'
                  f'{member.mention if member else f"<@{other}>"}: please confirm or dispute this result.')
        await i.response.edit_message(content='Result submitted. The other player has been asked to confirm or dispute it.',view=None)
        await i.followup.send(proposed,view=view,ephemeral=False,allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False))
class GScoreSelectView(discord.ui.View):
    def __init__(self,m,w):super().__init__(timeout=300);self.add_item(GScoreSelect(m,w))
class GResultView(discord.ui.View):
    def __init__(self,m,labels):super().__init__(timeout=300);self.add_item(GWinnerSelect(m,labels))
class GConfirmResultView(discord.ui.View):
    def __init__(self,no,w,score,reporter):
        super().__init__(timeout=None);self.no=no;self.w=w;self.score=score;self.reporter=reporter
        yes=discord.ui.Button(label='Confirm',style=discord.ButtonStyle.success,custom_id=f'gounament:confirm:{no}')
        no_btn=discord.ui.Button(label='Dispute',style=discord.ButtonStyle.danger,custom_id=f'gounament:dispute:{no}')
        yes.callback=self.confirm;no_btn.callback=self.dispute
        self.add_item(yes);self.add_item(no_btn)
    async def interaction_check(self,i):
        m=await g_db('SELECT p1,p2 FROM gounament_matches WHERE guild_id=? AND match_no=?',(str(i.guild_id),self.no),True)
        if not m:return False
        if str(i.user.id)==self.reporter or str(i.user.id) not in (m['p1'],m['p2']):
            await i.response.send_message('Only the opponent can confirm or dispute.',ephemeral=True);return False
        return True
    async def confirm(self,i):
        m=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND match_no=?',(str(i.guild_id),self.no),True)
        if not m or m['status']!='awaiting_confirmation':return await i.response.send_message('This result is no longer awaiting confirmation.',ephemeral=True)
        await i.response.defer();await g_advance(i.guild,m,self.w,self.score)
        try:await i.message.edit(content='Result confirmed.',view=None)
        except Exception:pass
    async def dispute(self,i):await i.response.send_modal(GDisputeModal(self.no))

class GDisputeModal(discord.ui.Modal,title='Dispute result'):
    reason=discord.ui.TextInput(label='Reason',style=discord.TextStyle.paragraph,max_length=1000,required=True)
    def __init__(self,no):super().__init__();self.no=no
    async def on_submit(self,i):
        m=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND match_no=?',(str(i.guild_id),self.no),True)
        if not m or m['status']!='awaiting_confirmation':return await i.response.send_message('This result cannot be disputed now.',ephemeral=True)
        await g_db("UPDATE gounament_matches SET status='disputed',dispute_reason=? WHERE guild_id=? AND match_no=?",(str(self.reason),str(i.guild_id),self.no))
        th=i.guild.get_thread(int(m['thread_id'])) if m['thread_id'] else None
        admin_role=find_role(i.guild,ADMIN_ROLE)
        pings=admin_role.mention if admin_role else '**League Admin role not found.**'
        allowed=discord.AllowedMentions(roles=True,users=False,everyone=False)
        if th:await th.send(f'⚠️ **Result disputed by {i.user.mention}.**\nReason: {self.reason}\n{pings}',allowed_mentions=allowed)
        await i.response.send_message('The result is disputed. League Admins have been notified.',ephemeral=True)

# Subcommands are grouped so they appear as /tournament ...
tournament=app_commands.Group(name='tournament',description='Manage the 1v1 Gounament')
@tournament.command(name='setup',description='Create a new Gounament and open registration')
async def tournament_setup(i):
    if not await g_admin(i):return
    s=await g_state(i.guild_id)
    if s and s['status'] not in ('ended','reset'):return await i.response.send_message('An active Gounament already exists. End it first.',ephemeral=True)
    await i.response.send_modal(GSetupModal())

@tournament.command(name='close-reg',description='Close Gounament registration')
async def tournament_close_reg(i):
    if not await g_admin(i):return
    s=await g_state(i.guild_id)
    if not s or s['status']!='registration':return await i.response.send_message('Registration is not open.',ephemeral=True)
    await g_db("UPDATE gounament_state SET status='closed' WHERE guild_id=?",(str(i.guild_id),))
    await g_update_registration(i.guild)
    await g_host_announce(i.guild, '🔒 **Registration is closed.** The player list is locked. Waiting for a League Admin to start the tournament.')
    await i.response.send_message('Gounament registration is closed. Use `/tournament start` when ready.',ephemeral=True)

@tournament.command(name='start',description='Generate the Gounament bracket and first-round matches')
async def tournament_start(i):
    if not await g_admin(i):return
    await i.response.defer(ephemeral=False, thinking=True)
    s=await g_state(i.guild_id)
    if not s or s['status']!='closed':return await i.followup.send('Close registration first.',ephemeral=True)
    players=await g_db('SELECT user_id FROM gounament_players WHERE guild_id=? ORDER BY user_id',(str(i.guild_id),),True,True)
    ids=[p['user_id'] for p in players]
    if len(ids)<2:return await i.followup.send('At least two participants are required.',ephemeral=True)

    random.shuffle(ids)
    size=1<<(len(ids)-1).bit_length()
    bye_count=size-len(ids)
    first_round_positions=size//2
    rounds=int(math.log2(size))

    # Select the bye players first. Every other entrant must be paired in
    # Round 1. A Round 1 feeder position either contains one bye player
    # (who enters Round 2 directly) or one real match (two players).
    # Round 2 match slots are fed by pairs of adjacent Round 1 positions.
    target_count=size//4
    if bye_count >= target_count:
        bye_positions=set()
        # Ensure every Round 2 match receives at least one direct bye entrant.
        for target_index in range(target_count):
            bye_positions.add(target_index*2 + random.randrange(2))
        remaining_byes=bye_count-len(bye_positions)
        available=[pos for pos in range(first_round_positions) if pos not in bye_positions]
        if remaining_byes:
            bye_positions.update(random.sample(available,remaining_byes))
    else:
        bye_positions=set(random.sample(range(first_round_positions),bye_count))

    bye_ids=set(random.sample(ids,bye_count)) if bye_count else set()
    bye_players=list(bye_ids)
    random.shuffle(bye_players)
    round1_players=[uid for uid in ids if uid not in bye_ids]
    random.shuffle(round1_players)

    round1_pairs=[]
    bye_advances={}
    bye_iter=iter(bye_players)
    player_iter=iter(round1_players)
    for match_pos in range(first_round_positions):
        target_index=match_pos//2
        target_slot='p1' if match_pos%2==0 else 'p2'
        if match_pos in bye_positions:
            bye_advances[(target_index,target_slot)]=next(bye_iter)
        else:
            a=next(player_iter)
            b=next(player_iter)
            round1_pairs.append((match_pos,a,b))

    # Safety checks: every registered player is placed exactly once, and
    # no Round 1 match contains an empty/bye slot.
    placed=[uid for _,a,b in round1_pairs for uid in (a,b)] + list(bye_advances.values())
    assert len(placed)==len(ids) and set(placed)==set(ids)
    assert all(a and b for _,a,b in round1_pairs)

    async with aiosqlite.connect(DB) as db:
        await db.execute('DELETE FROM gounament_matches WHERE guild_id=?',(str(i.guild_id),))
        await db.execute('DELETE FROM gounament_slots WHERE guild_id=?',(str(i.guild_id),))
        for n,uid in enumerate(ids,1):
            await db.execute(
                'INSERT INTO gounament_players(guild_id,user_id,seed) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET seed=excluded.seed',
                (str(i.guild_id),uid,n)
            )

        next_match_no=1
        for bracket_pos,a,b in round1_pairs:
            await db.execute(
                "INSERT INTO gounament_matches(guild_id,match_no,round_no,p1,p2,status,bracket_pos) VALUES(?,?,1,?,?,'pending',?)",
                (str(i.guild_id),next_match_no,a,b,bracket_pos)
            )
            next_match_no+=1

        # Create later-round match cards. Round 2 receives bye players directly;
        # all other positions are populated only when their feeder match wins.
        for r in range(2,rounds+1):
            count=size//(2**r)
            for slot_no in range(count):
                p1=p2=None
                if r==2:
                    p1=bye_advances.get((slot_no,'p1'))
                    p2=bye_advances.get((slot_no,'p2'))
                await db.execute(
                    "INSERT INTO gounament_matches(guild_id,match_no,round_no,p1,p2,status,bracket_pos) VALUES(?,?,?,?,?,'pending',?)",
                    (str(i.guild_id),next_match_no,r,p1,p2,slot_no)
                )
                next_match_no+=1

        await db.execute("UPDATE gounament_state SET status='running',current_round=1 WHERE guild_id=?",(str(i.guild_id),))
        await db.commit()

    await g_publish_bracket(i.guild)
    ms=await g_db("SELECT * FROM gounament_matches WHERE guild_id=? AND round_no=1 ORDER BY bracket_pos",(str(i.guild_id),),True,True)
    for match in ms:
        await g_make_thread(i.guild,match)
    total_matches = len(ids) - 1
    bye_names = []
    for uid in bye_advances.values():
        member = i.guild.get_member(int(uid))
        bye_names.append(member.display_name if member else f'Player {uid}')
    bye_text = ', '.join(f'**{discord.utils.escape_markdown(name)}**' for name in bye_names) if bye_names else 'None'

    host_message = (
        f'🏁 **{discord.utils.escape_markdown(s.get("name") or "The Gounament")} has started!**\n'
        f'**Participants:** {len(ids)}\n'
        f'**Byes awarded:** {bye_count}\n'
        f'**Round 1 matches:** {len(ms)}\n'
        f'**Total matches in the tournament:** {total_matches}\n\n'
        f'**Why are there byes?** A single-elimination bracket needs a power-of-two number of slots '
        f'(2, 4, 8, 16, etc.). Because {len(ids)} players registered, {bye_count} player(s) were '
        f'randomly given a bye so the bracket fits correctly.\n'
        f'**Players with a bye:** {bye_text}. They are already placed in Round 2 and do not play in Round 1.\n\n'
        f'**All other players:** Please complete your Round 1 match and wait. Round 2 will only begin '
        f'when every Round 1 match is finished and a League Admin starts the next round.'
    )
    await g_host_announce(i.guild, host_message)
    await i.followup.send(
        f'Gounament started: **{len(ids)} players**, **{bye_count} bye(s)**, '
        f'**{len(ms)} Round 1 matches** and **{total_matches} total matches**.',
        ephemeral=False
    )

@tournament.command(name='nextround',description='Open matches for the next Gounament round')
async def tournament_nextround(i):
    if not await g_admin(i):return
    await i.response.defer(ephemeral=False, thinking=True)
    s=await g_state(i.guild_id)
    if not s or s['status']!='running':return await i.followup.send('No running Gounament.',ephemeral=True)
    current=s['current_round']
    openm=await g_db("SELECT COUNT(*) AS n FROM gounament_matches WHERE guild_id=? AND round_no=? AND status!='confirmed' AND p1 IS NOT NULL AND p2 IS NOT NULL",(str(i.guild_id),current),True)
    if openm and openm['n']:return await i.followup.send(f'{openm["n"]} match(es) are not confirmed yet, including disputed matches.',ephemeral=True)
    total=await g_db('SELECT COUNT(*) AS n FROM gounament_matches WHERE guild_id=? AND round_no=?',(str(i.guild_id),current+1),True)
    if not total or total['n']==0:
        return await i.followup.send('The Gounament is finished. No next round remains.',ephemeral=True)
    ms=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND round_no=?',(str(i.guild_id),current+1),True,True)
    if any(not m['p1'] or not m['p2'] for m in ms):return await i.followup.send('Some next-round slots are not filled yet. Resolve the current round first.',ephemeral=True)
    await g_db('UPDATE gounament_state SET current_round=? WHERE guild_id=?',(current+1,str(i.guild_id)))
    for m in ms:await g_make_thread(i.guild,m)
    stage = await g_round_label(i.guild_id, current+1)
    await g_host_announce(i.guild, f'▶️ **{stage} have started!** {len(ms)} match thread(s) have been created. Players can now play their matches.')
    await i.followup.send(f'{stage} opened. {len(ms)} match thread(s) created.',ephemeral=False)

@tournament.command(name='end',description='End the active Gounament and remove Gourny roles')
async def tournament_end(i):
    if not await g_admin(i):return
    s=await g_state(i.guild_id)
    if not s:return await i.response.send_message('No Gounament exists.',ephemeral=True)
    view=GConfirmAdminView('end')
    await i.response.send_message('End this Gounament? All Gourny roles will be removed. Channels and history remain.',view=view,ephemeral=True)

@tournament.command(name='reset',description='Permanently delete all Gounament data and channels')
async def tournament_reset(i):
    if not await g_admin(i):return
    await i.response.send_message('Permanently delete the Gounament category, Gourny role, participants, bracket and all Gounament history? Regular Goon roles and EGL league data will not be touched.',view=GConfirmAdminView('reset'),ephemeral=True)

class GConfirmAdminView(discord.ui.View):
    def __init__(self,action):super().__init__(timeout=60);self.action=action
    @discord.ui.button(label='Confirm',style=discord.ButtonStyle.danger)
    async def confirm(self,i,b):
        if not is_admin(i.user):return await i.response.send_message('League Admin only.',ephemeral=True)
        s=await g_state(i.guild_id)
        if not s:return await i.response.send_message('No Gounament data found.',ephemeral=True)
        # Acknowledge while the interaction's channel still exists. Reset deletes
        # that channel/category, so no response/edit may be attempted afterwards.
        if self.action=='end':
            await i.response.send_message('Ending Gounament and removing Gourny roles...',ephemeral=True)
        else:
            await i.response.send_message('Resetting Gounament and deleting its channels, role and data...',ephemeral=True)
        players=await g_db('SELECT user_id FROM gounament_players WHERE guild_id=?',(str(i.guild_id),),True,True)
        role=i.guild.get_role(int(s['role_id'])) if s.get('role_id') else None
        for p in players:
            m=i.guild.get_member(int(p['user_id']))
            if m and role:
                try:await m.remove_roles(role,reason='Gounament ended/reset')
                except Exception as e:log.warning('Could not remove Gourny role from %s: %s',p['user_id'],e)
        if self.action=='end':
            # Remove the actual registration post before marking the tournament ended.
            # The channel remains in place; only the rules/prizes/register message is deleted.
            reg_channel = i.guild.get_channel(int(s['registration_ch'])) if s.get('registration_ch') else None
            reg_message_id = s.get('registration_msg')
            if reg_channel and reg_message_id:
                try:
                    reg_message = await reg_channel.fetch_message(int(reg_message_id))
                    await reg_message.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden as e:
                    log.warning('Cannot delete Gounament registration message %s: %s', reg_message_id, e)
                except Exception as e:
                    log.warning('Could not delete Gounament registration message %s: %s', reg_message_id, e)
            await g_db("UPDATE gounament_state SET status='ended', registration_msg=NULL WHERE guild_id=?",(str(i.guild_id),))
            for match in await g_db('SELECT thread_id FROM gounament_matches WHERE guild_id=?',(str(i.guild_id),),True,True):
                th=i.guild.get_thread(int(match['thread_id'])) if match['thread_id'] else None
                if th is None and match['thread_id']:
                    try:
                        th=await i.guild.fetch_channel(int(match['thread_id']))
                    except discord.NotFound:
                        th=None
                    except Exception as e:
                        log.warning('Could not fetch Gounament match thread %s: %s',match['thread_id'],e)
                if th:
                    try:await th.delete(reason='Gounament ended')
                    except discord.NotFound:pass
                    except Exception as e:log.warning('Could not delete Gounament match thread: %s',e)
            # Announce the completed tournament in #host and update the
            # confirmation response so the interaction does not appear stuck.
            await g_host_announce(i.guild, f'🏁 **{s.get("name") or "The Gounament"} has ended.** All Gourny roles have been removed. Tournament channels and history remain.')
            try:
                await i.edit_original_response(content=f'**{s.get("name") or "Gounament"}** has ended. Gourny roles removed, registration message and match threads deleted. A completion announcement was sent to #host.',view=None)
            except Exception as e:
                log.warning('Could not update Gounament end confirmation response: %s',e)
        else:
            cat=i.guild.get_channel(int(s['category_id'])) if s.get('category_id') else None
            if cat:
                for ch in list(cat.channels):
                    try:await ch.delete(reason='Gounament reset')
                    except Exception as e:log.warning('Could not delete Gounament channel %s: %s',ch.id,e)
                try:await cat.delete(reason='Gounament reset')
                except Exception as e:log.warning('Could not delete Gounament category: %s',e)
            if role:
                try:await role.delete(reason='Gounament reset')
                except Exception as e:log.warning('Could not delete Gourny role: %s',e)
            for table in ['gounament_players','gounament_matches','gounament_slots','gounament_state']:
                await g_db(f'DELETE FROM {table} WHERE guild_id=?',(str(i.guild_id),))
    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,i,b):await i.response.edit_message(content='Cancelled.',view=None)

@tournament.command(name='closematch',description='League Admin: manually resolve a Gounament match')
@app_commands.describe(match_number='The Gounament match number')
async def tournament_closematch(i,match_number:int):
    if not await g_admin(i):return
    m=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND CAST(match_no AS INTEGER)=?',(str(i.guild_id),int(match_number)),True)
    if not m or m['status']=='confirmed':return await i.response.send_message('Match not found or already confirmed.',ephemeral=True)
    if not m['p1'] or not m['p2']:return await i.response.send_message('This match does not have two players assigned yet.',ephemeral=True)
    labels=await g_player_labels(i.guild,m)
    await i.response.send_message('Choose the winner and score (winner first) in this admin-only resolution panel.',view=GAdminResolveView(m,labels),ephemeral=True)

class GAdminResolveView(discord.ui.View):
    def __init__(self,m,labels):
        super().__init__(timeout=300);self.m=m;self.winner=None
        self.add_item(GAdminWinnerSelect(self,labels))
        for score in ('3-0','3-1','3-2'):
            self.add_item(GAdminScoreButton(self,score))

class GAdminWinnerSelect(discord.ui.Select):
    def __init__(self,owner_view,labels):
        # discord.ui.Item already defines a read-only `parent` property.
        # Keep our owning View under a separate attribute.
        self.owner_view=owner_view
        super().__init__(placeholder='Choose the winner',options=[discord.SelectOption(label=labels[0][:100],value=owner_view.m['p1']),discord.SelectOption(label=labels[1][:100],value=owner_view.m['p2'])])
    async def callback(self,i):
        if not is_admin(i.user):return await i.response.send_message('League Admin only.',ephemeral=True)
        self.owner_view.winner=self.values[0]
        self.owner_view.remove_item(self)
        await i.response.edit_message(content='Winner selected. Choose the winner’s score below.',view=self.owner_view)

class GAdminScoreButton(discord.ui.Button):
    def __init__(self,owner_view,score):
        self.owner_view=owner_view;self.score=score
        super().__init__(label=score,style=discord.ButtonStyle.primary)
    async def callback(self,i):
        if not is_admin(i.user):return await i.response.send_message('League Admin only.',ephemeral=True)
        if not self.owner_view.winner:return await i.response.send_message('Choose a winner first.',ephemeral=True)
        m=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND match_no=?',(str(i.guild_id),self.owner_view.m['match_no']),True)
        if not m or m['status']=='confirmed':return await i.response.edit_message(content='This match has already been resolved.',view=None)
        await i.response.send_message('Gounament match resolved. Winner advanced and bracket updated.',ephemeral=True)
        await g_advance(i.guild,m,self.owner_view.winner,self.score)

onevone=app_commands.Group(name='1v1',description='Gounament 1v1 match commands')
@onevone.command(name='result',description='Report a result in a Gounament match thread')
async def g_1v1_result(i):
    s=await g_state(i.guild_id)
    if not s or s['status']!='running':return await i.response.send_message('No running Gounament.',ephemeral=True)
    m=await g_db('SELECT * FROM gounament_matches WHERE guild_id=? AND thread_id=?',(str(i.guild_id),str(i.channel_id)),True)
    if not m or str(i.user.id) not in (m['p1'],m['p2']):return await i.response.send_message('This command is only available to participants in their Gounament match thread.',ephemeral=True)
    if m['status']!='pending':return await i.response.send_message('This match is not accepting a new result.',ephemeral=True)
    labels=await g_player_labels(i.guild,m)
    await i.response.send_message('Select the winner and BO5 score.',view=GResultView(m,labels),ephemeral=True)

bot.tree.add_command(onevone)
bot.tree.add_command(overseer)

bot.tree.add_command(tournament)


bot.tree.add_command(scrimbot_grp)
bot.tree.add_command(setup)

if __name__=="__main__":
    if not TOKEN:print("\n\u274c Set DISCORD_BOT_TOKEN!\n")
    else:bot.run(TOKEN)
