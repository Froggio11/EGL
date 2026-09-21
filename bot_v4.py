# Elements Divided League Bot v5 — Elite Goon League
# pip install discord.py aiosqlite
# 3v3 league, max 4 players/team, rank-based system
import asyncio,logging,os,uuid,random
from datetime import datetime,timezone,timedelta
import aiosqlite,discord
from discord import app_commands
from discord.ext import commands,tasks
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)
TOKEN=os.environ.get("DISCORD_BOT_TOKEN","")
DB="league.db";DEFAULT_MMR=1000;MAX_TEAM=5;SEASON_WEEKS=8
ADMIN_ROLE="League Admin";TESTER_ROLE="EGL Tester";SPOON_ROLE="Spoon"
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
intents=discord.Intents.default();intents.members=True;intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)

async def init_db():
    async with aiosqlite.connect(DB)as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS config(guild_id TEXT PRIMARY KEY,name TEXT,admin_id TEXT,announcements_ch TEXT,matches_ch TEXT,results_ch TEXT,general_ch TEXT,fa_ch TEXT,teams_ch TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS teams(guild_id TEXT,name TEXT,display TEXT,captain_id TEXT,wins INT DEFAULT 0,losses INT DEFAULT 0,mmr INT DEFAULT 1000,thread_id TEXT,role_id TEXT,created_at TEXT,clantag TEXT,PRIMARY KEY(guild_id,name));
        CREATE TABLE IF NOT EXISTS members(guild_id TEXT,team_name TEXT,user_id TEXT,PRIMARY KEY(guild_id,team_name,user_id));
        CREATE TABLE IF NOT EXISTS fa(guild_id TEXT,user_id TEXT,username TEXT,joined_at TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,guild_id TEXT,week INT,team1 TEXT,team2 TEXT,score TEXT,winner TEXT,reporter TEXT,created_at TEXT,thread_id TEXT,is_finals INT DEFAULT 0,map TEXT,scheduled TEXT,reschedule_by TEXT,reschedule_to TEXT);
        CREATE TABLE IF NOT EXISTS season(guild_id TEXT PRIMARY KEY,weeks_done INT DEFAULT 0,finals_generated INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS player_history(guild_id TEXT,user_id TEXT,last_mmr INT DEFAULT 1000,cooldown_until TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS guild_settings(guild_id TEXT PRIMARY KEY,teams_ch TEXT);
        CREATE TABLE IF NOT EXISTS leaderboard_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT);
        CREATE TABLE IF NOT EXISTS setup_data(guild_id TEXT PRIMARY KEY,league_name TEXT,league_category_id TEXT,matches_category_id TEXT,announcements_ch TEXT,general_ch TEXT,teams_ch TEXT,fa_ch TEXT,matches_ch TEXT,results_ch TEXT);
        CREATE TABLE IF NOT EXISTS scrim_sessions(guild_id TEXT,date TEXT,thread_id TEXT,msg_id TEXT,max_players INT DEFAULT 6,PRIMARY KEY(guild_id,date));
        CREATE TABLE IF NOT EXISTS scrim_signups(guild_id TEXT,date TEXT,user_id TEXT,position INT,PRIMARY KEY(guild_id,date,user_id));
        """)
        # Migrate: add clantag column if missing
        try:await db.execute("ALTER TABLE teams ADD COLUMN clantag TEXT")
        except:pass
        # Migrate: add new columns to scrim_sessions if missing
        for col,typ in [("max_players","INT DEFAULT 6"),("scrim_title","TEXT"),("unix_time","INT"),("thread_id","TEXT"),("thread_msg_id","TEXT"),("pinged","INT DEFAULT 0")]:
            try:await db.execute(f"ALTER TABLE scrim_sessions ADD COLUMN {col} {typ}")
            except:pass
        await db.commit()

async def cfg_get(gid):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM config WHERE guild_id=?",(gid,))as c:
            r=await c.fetchone();return dict(r)if r else None

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

async def get_scrim_session(gid,date):
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM scrim_sessions WHERE guild_id=? AND date=?",(gid,date))as c:
            r=await c.fetchone();return dict(r)if r else None

async def scrim_signup_count(gid,date):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT COUNT(*) FROM scrim_signups WHERE guild_id=? AND date=?",(gid,date))as c:
            r=await c.fetchone();return r[0]if r else 0

async def scrim_max_players(gid,date):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT max_players FROM scrim_sessions WHERE guild_id=? AND date=?",(gid,date))as c:
            r=await c.fetchone();return r[0]if r else 6

async def scrim_signups_active(gid,date):
    max_p=await scrim_max_players(gid,date)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups WHERE guild_id=? AND date=? ORDER BY position ASC LIMIT ?",(gid,date,max_p))as c:
            return[r[0]for r in await c.fetchall()]

async def scrim_queue_list(gid,date):
    max_p=await scrim_max_players(gid,date)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups WHERE guild_id=? AND date=? AND position>=? ORDER BY position ASC",(gid,date,max_p))as c:
            return[r[0]for r in await c.fetchall()]

async def scrim_next_queue(gid,date):
    max_p=await scrim_max_players(gid,date)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups WHERE guild_id=? AND date=? AND position>=? ORDER BY position ASC LIMIT 1",(gid,date,max_p))as c:
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

async def need_captain(i):
    t=await team_by_player(str(i.guild_id),str(i.user.id))
    if not t:await i.response.send_message("\u274c Not on team.",ephemeral=True);return False
    if t["captain_id"]!=str(i.user.id):await i.response.send_message("\u274c Captain only.",ephemeral=True);return False
    return t["display"]

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
        caps=[t1["captain_id"]if t1 else"",t2["captain_id"]if t2 else""]
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

class CounterModal(discord.ui.Modal,title="Counter-proposal"):
    def __init__(self,view):
        super().__init__();self.view=view
        self.t=discord.ui.TextInput(label="New time (e.g. 12 Sep 20:00 or 8pm)",style=discord.TextStyle.short)
        self.add_item(self.t)
    async def on_submit(self,i):
        try:
            dt=parse_schedule(self.t.value)
            ns=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
        except:
            await i.response.send_message("Invalid time format.",ephemeral=True);return
        gid=str(i.guild_id)
        team=await captain_team(gid,self.view.actor_id)
        await record_sched_event(gid,self.view.mid,team or "?","counter",ns)
        self.view.done=True
        other_member=i.guild.get_member(int(self.view.proposer_id))
        nv=ScheduleConfirmView(self.view.mid,self.view.proposer_id,self.view.actor_id,ns,unix,self.view.thread_id)
        await i.response.send_message(f"\U0001f504 {i.user.mention} counter-proposes **{ns}** (<t:{unix}:f>).\n{other_member.mention if other_member else ''} please reply:",view=nv)

class ScheduleConfirmView(discord.ui.View):
    def __init__(self,mid,actor_id,proposer_id,sched,unix,thread_id):
        super().__init__(timeout=86400);self.mid=mid;self.actor_id=actor_id;self.proposer_id=proposer_id;self.sched=sched;self.unix=unix;self.thread_id=thread_id;self.done=False
    async def _check(self,i):
        if self.done:await i.response.send_message("Stale proposal.",ephemeral=True);return False
        if str(i.user.id)!=self.actor_id:await i.response.send_message("Other captain only.",ephemeral=True);return False
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
        async with aiosqlite.connect(DB)as db:await db.execute("UPDATE matches SET scheduled=? WHERE thread_id=?",(self.sched,self.thread_id));await db.commit()
        self.done=True
        for c in self.children:c.disabled=True
        await i.response.edit_message(content=f"\U0001f4c5 **Confirmed!** Match scheduled: **{self.sched}**\n\U0001f550 Your time: <t:{self.unix}:f>",view=None)
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
        await i.response.send_modal(CounterModal(self))

class ResultConfirmView(discord.ui.View):
    def __init__(self,ocid,d,opp_name,score,won,winner,delta,gid,cfg):
        super().__init__(timeout=86400)
        self.ocid=ocid;self.d=d;self.opp=opp_name;self.score=score
        self.won=won;self.winner=winner;self.delta=delta;self.gid=gid;self.cfg=cfg
    @discord.ui.button(label="\u2705 Confirm Result",style=discord.ButtonStyle.green)
    async def confirm(self,i,btn):
        if str(i.user.id)!=self.ocid:await i.response.send_message("Other captain only.",ephemeral=True);return
        d=self.d;opp_name=self.opp;score=self.score;won=self.won;winner=self.winner;delta=self.delta;gid=self.gid
        async with aiosqlite.connect(DB)as db:
            if won:
                await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,d.lower()))
                await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,opp_name))
            else:
                await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,d.lower()))
                await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,opp_name))
            await db.execute("INSERT INTO matches VALUES(?,?,0,?,?,?,?,?,?,NULL,0,NULL,NULL,NULL,NULL)",(str(uuid.uuid4())[:8],gid,d,opp_name,score,winner,str(i.user.id),datetime.now(timezone.utc).isoformat()))
            await db.commit()
        c=self.cfg
        if c and c.get("results_ch"):
            rc=i.guild.get_channel(int(c["results_ch"]))
            if rc:await rc.send(f"\u26a1 **{d} {score} {opp_name}**\nWinner:**{winner}**\nConfirmed by both captains.")
        if c and c.get("matches_ch"):
            mc=i.guild.get_channel(int(c["matches_ch"]))
            if mc:
                found_t=None
                for t in mc.threads:
                    if d.lower()in t.name.lower()and opp_name in t.name.lower():found_t=t;break
                if not found_t:
                    async for t in mc.archived_threads():
                        if d.lower()in t.name.lower()and opp_name in t.name.lower():found_t=t;break
                if found_t:
                    try:
                        if found_t.archived:await found_t.edit(archived=False,locked=False)
                        await found_t.send(f"\u2705 **{d} {score} {opp_name}** - {winner} wins!")
                        await found_t.edit(archived=True,locked=True)
                    except:pass
        my_t=await team_get(gid,d);opp_t=await team_get(gid,opp_name)
        su="+"if won else"-";st="-"if won else"+"
        for c2 in self.children:c2.disabled=True
        await i.response.edit_message(content=f"\u26a1 **Result confirmed!**\n**{d} {score} {opp_name}** - Winner: **{winner}**\n{d}({get_rank(my_t['mmr'])}) {su}{delta} | {opp_name}({get_rank(opp_t['mmr'])}) {st}{delta}",view=self)
    @discord.ui.button(label="\u26a0\ufe0f Dispute",style=discord.ButtonStyle.red)
    async def dispute(self,i,btn):
        if str(i.user.id)!=self.ocid:await i.response.send_message("Other captain only.",ephemeral=True);return
        for c in self.children:c.disabled=True
        await i.response.edit_message(content="\u26a0\ufe0f **Result disputed!** Contact a League Admin to resolve.",view=self)

class ScrimSignupView(discord.ui.View):
    def __init__(self,gid,date):
        super().__init__(timeout=None);self.gid=gid;self.date=date
    @discord.ui.button(label="Sign Up",style=discord.ButtonStyle.green,emoji="\u2795")
    async def signup(self,i,btn):
        gid=self.gid;date=self.date;uid=str(i.user.id)
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT 1 FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid))as c:
                if await c.fetchone():await i.response.send_message("\u274c You're already signed up! Sign off first if you want to leave.",ephemeral=True);return
        count=await scrim_signup_count(gid,date)
        max_p=await scrim_max_players(gid,date)
        async with aiosqlite.connect(DB)as db:
            await db.execute("INSERT OR IGNORE INTO scrim_signups VALUES(?,?,?,?)",(gid,date,uid,count));await db.commit()
        active=count<max_p
        status="You're in the scrim!"if active else f"You're up next (#{count-max_p+1} in queue)."
        await i.response.send_message(f"\u2705 Signed up! {status}",ephemeral=True)
        th=await _scrim_thread(gid,date)
        if th:
            try:await th.add_user(i.user)
            except:pass
        await update_scrim_embed(gid,date)
        await update_scrim_thread(gid,date)
    @discord.ui.button(label="Sign Off",style=discord.ButtonStyle.red,emoji="\u274c")
    async def leave(self,i,btn):
        await scrim_leave(i,self.gid,self.date)

async def _scrim_thread(gid,date):
    g=bot.get_guild(int(gid))
    if not g:return None
    session=await get_scrim_session(gid,date)
    if not session or not session.get("thread_id"):return None
    tid=int(session["thread_id"])
    th=g.get_thread(tid)
    if th:return th
    try:return await g.fetch_thread(tid)
    except:return None

async def close_scrim(gid,date,sess=None):
    g=bot.get_guild(int(gid))
    if not g:return
    members=await scrim_signups_active(gid,date)
    queue=await scrim_queue_list(gid,date)
    th=None
    if sess and sess.get("thread_id"):
        tid=int(sess["thread_id"])
        th=g.get_thread(tid)
        if th is None:
            try:th=await g.fetch_thread(tid)
            except:th=None
    if th:
        try:await th.send("\U0001f512 **Scrim finished** - closing the thread. GG!")
        except:pass
        for uid in set(list(members)+list(queue)):
            try:
                m=g.get_member(int(uid))
                if m:await th.remove_user(m)
            except:pass
        try:await th.edit(archived=True,locked=True)
        except:pass
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date=?",(gid,date))
        await db.execute("DELETE FROM scrim_sessions WHERE guild_id=? AND date=?",(gid,date))
        await db.commit()

async def scrim_leave(i,gid,date):
    uid=str(i.user.id)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT position FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid))as c:
            r=await c.fetchone()
    if not r:await i.response.send_message("\u274c You're not signed up.",ephemeral=True);return
    pos=r[0]
    async with aiosqlite.connect(DB)as db:
        await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid));await db.commit()
    max_p=await scrim_max_players(gid,date)
    was_active=pos<max_p
    await i.response.send_message("\U0001f44b You've signed off.",ephemeral=True)
    th=await _scrim_thread(gid,date)
    g=bot.get_guild(int(gid))
    if th and g:
        try:
            m=g.get_member(int(uid))
            if m:await th.remove_user(m)
        except:pass
    # If an active player left, pull the first person up from the queue
    if was_active:
        nxt=await scrim_next_queue(gid,date)
        if nxt:
            async with aiosqlite.connect(DB)as db:
                await db.execute("UPDATE scrim_signups SET position=? WHERE guild_id=? AND date=? AND user_id=?",(pos,gid,date,nxt));await db.commit()
            if th:
                try:await th.send(f"\U0001f4e2 <@{nxt}> a spot opened up - you're in the scrim!")
                except:pass
    await update_scrim_embed(gid,date)
    await update_scrim_thread(gid,date)

class ScrimThreadView(discord.ui.View):
    def __init__(self,gid,date):
        super().__init__(timeout=None);self.gid=gid;self.date=date
    @discord.ui.button(label="Sign Off",style=discord.ButtonStyle.red,emoji="\u274c")
    async def signoff(self,i,btn):
        await scrim_leave(i,self.gid,self.date)
    @discord.ui.button(label="Ask for Queue",style=discord.ButtonStyle.blurple,emoji="\U0001f4e3")
    async def askqueue(self,i,btn):
        gid=self.gid;date=self.date
        members=await scrim_signups_active(gid,date)
        max_p=await scrim_max_players(gid,date)
        queue=await scrim_queue_list(gid,date)
        if len(members)<max_p:
            await i.response.send_message(f"\u2705 There's still a free spot - sign up in the scrim channel!",ephemeral=True);return
        if not queue:
            await i.response.send_message("\u274c Nobody is in the queue right now.",ephemeral=True);return
        nxt=queue[0]
        await i.response.send_message(f"\U0001f4e3 Asked <@{nxt}> to fill a spot.",ephemeral=True)
        try:await i.channel.send(f"\U0001f4e3 <@{nxt}> a spot is open - can you play?")
        except:pass

async def update_scrim_thread(gid,date):
    session=await get_scrim_session(gid,date)
    if not session or not session.get("thread_id"):return
    th=await _scrim_thread(gid,date)
    if not th:return
    members=await scrim_signups_active(gid,date)
    queue=await scrim_queue_list(gid,date)
    max_p=await scrim_max_players(gid,date)
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
    view=ScrimThreadView(gid,date)
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
                await db.execute("UPDATE scrim_sessions SET thread_msg_id=? WHERE guild_id=? AND date=?",(str(target.id),gid,date));await db.commit()
        for d in dups:
            try:await d.delete()
            except:pass
        return
    try:
        m=await th.send(embed=embed,view=view)
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE scrim_sessions SET thread_msg_id=? WHERE guild_id=? AND date=?",(str(m.id),gid,date));await db.commit()
        try:await m.pin()
        except:pass
    except Exception as e:log.warning("scrim thread msg: %s",e)

async def update_scrim_embed(gid,date):
    g=bot.get_guild(int(gid))
    if not g:return
    session=await get_scrim_session(gid,date)
    if not session or not session.get("msg_id"):return
    members=await scrim_signups_active(gid,date)
    total=await scrim_signup_count(gid,date)
    max_p=await scrim_max_players(gid,date)
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

class RescheduleView(discord.ui.View):
    def __init__(self,ocid,nt,mid,gid):
        super().__init__(timeout=86400);self.ocid=ocid;self.nt=nt;self.mid=mid;self.gid=gid
    async def _other_cap(self,gid,user_id):
        async with aiosqlite.connect(DB)as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT team1,team2 FROM matches WHERE id=?",(self.mid,))as cur:
                row=await cur.fetchone()
        if not row:return None
        t1=await team_get(gid,row["team1"]);t2=await team_get(gid,row["team2"])
        caps=[t1["captain_id"]if t1 else"",t2["captain_id"]if t2 else""]
        other=[c for c in caps if c!=user_id]
        return other[0]if other else None
    @discord.ui.button(label="Approve",style=discord.ButtonStyle.green)
    async def approve(self,i,btn):
        gid=str(i.guild_id)
        other=await self._other_cap(gid,str(i.user.id))
        if not other or str(i.user.id)!=other:await i.response.send_message("Other captain only.",ephemeral=True);return
        async with aiosqlite.connect(DB)as db:await db.execute("UPDATE matches SET scheduled=? WHERE id=?",(self.nt,self.mid));await db.commit()
        for c in self.children:c.disabled=True
        await i.response.edit_message(content=f"Rescheduled to **{self.nt}**",view=self)
    @discord.ui.button(label="Deny",style=discord.ButtonStyle.red)
    async def deny(self,i,btn):
        other=await self._other_cap(str(i.guild_id),str(i.user.id))
        if not other or str(i.user.id)!=other:await i.response.send_message("Other captain only.",ephemeral=True);return
        for c in self.children:c.disabled=True;await i.response.edit_message(content="Denied.",view=self)

# ===== Gen Matches =====
async def gen_matches(guild,c,force=False):
    gid=str(guild.id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT weeks_done FROM season WHERE guild_id=?",(gid,))as cur:
            row=await cur.fetchone()
    if not row:return
    week=row["weeks_done"]+1
    if week>SEASON_WEEKS and not force:return
    ts=await teams_all(gid)
    eligible=[t for t in ts if len(t.get("members",[]))>=3]
    names=[t["display"]for t in eligible]
    n=len(names)
    if n<2:return
    # Matches per week by team count
    rounds_needed=n if n%2 else n-1
    mpw=1 if rounds_needed<=SEASON_WEEKS else 2
    # Load already-scheduled pairings so nobody meets twice before all have met
    played=set()
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT team1,team2 FROM matches WHERE guild_id=?",(gid,))as cur:
            for r in await cur.fetchall():
                played.add(_pairkey(r[0],r[1]))
    names_sorted=sorted(names,key=str.lower)
    rounds=[make_pairs(names_sorted,rnd) for rnd in range(1,rounds_needed+1)]
    # Prefer rounds with the most not-yet-played pairings (clean rounds first)
    rounds.sort(key=lambda r:-sum(1 for a,b in r if _pairkey(a,b)not in played))
    target=mpw*(n//2)
    all_pairs=[];used=set()
    for rnd in rounds:
        if len(all_pairs)>=target:break
        for a,b in rnd:
            k=_pairkey(a,b)
            if k in used:continue
            used.add(k);played.add(k);all_pairs.append((a,b))
    if not all_pairs:return
    match_ids=[]
    async with aiosqlite.connect(DB)as db:
        for a,b in all_pairs:
            mid=str(uuid.uuid4())[:8]
            await db.execute("INSERT INTO matches VALUES(?,?,?,?,?,NULL,NULL,NULL,?,NULL,0,NULL,NULL,NULL,NULL)",(mid,gid,week,a,b,datetime.now(timezone.utc).isoformat()))
            match_ids.append((mid,a,b))
        if not force:await db.execute("UPDATE season SET weeks_done=? WHERE guild_id=?",(week,gid))
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
                            try:await th.add_user(m);added.add(uid)
                            except:pass
                for m in guild.members:
                    if is_admin(m)and str(m.id)not in added:
                        try:await th.add_user(m);added.add(str(m.id))
                        except:pass
                await th.send(f"\u26a1 **{a} vs {b}** - Week {week}\n\nEveryone's here! Captain, set a time with `/schedule {SCHED_HELP}` (GMT). Once both captains confirm, the map vote will open.")
            except Exception as e:log.warning("Thread: %s",e)
        lines=[f"\u26a1 **Week {week} Matches**",""]+[f"\u2022 **{a}** vs **{b}**"for _,a,b in match_ids]+["","Check threads!"]
        await ch.send("\n".join(lines))
        c2=await cfg_get(gid)
        if c2 and c2.get("announcements_ch"):
            ann=guild.get_channel(int(c2["announcements_ch"]))
            if ann:
                weeks_left=SEASON_WEEKS-week
                ts_all=await teams_all(gid)
                total_matches=sum(t["wins"]+t["losses"]for t in ts_all)//2
                finals_note="\U0001f3c6 Finals next week! Top 4 teams advance."if weeks_left==0 else f"Top 4 by rank advance to finals after Week {SEASON_WEEKS}."
                await ann.send(f"\U0001f4ca **Season Update  -  Week {week}/{SEASON_WEEKS}**\n\nWeeks remaining: **{weeks_left}**\nMatches played: **{total_matches}**\nTeams in league: **{len(ts_all)}**\n\n{finals_note}")
    return len(match_ids)

# ===== /guide =====
@bot.tree.command(name="guide",description="Post the league guide (League Admin only)")
async def guide_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    embed1=discord.Embed(title="\U0001f4d6 EGL - Elite Goon League",description="Welcome to the EGL, our own 3v3 competitive league for Elements Divided.",color=0x5865F2)
    embed1.add_field(name="\U0001f3c6 League Format",value="- Teams of **up to 5 players**\n- Season lasts **8 weeks**\n- Matches every **Sunday 10pm GMT**\n- You play **1 match a week** - sometimes **2** if there are lots of teams, so everyone gets to play everyone\n- Captains vote on map (coin flip on tie)\n- **Top 4** advance to Finals, then Grand Final!",inline=False)
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
    embed2.add_field(name="\U0001f4c5 Match *(captains only, in match thread)*",value="`/schedule 05 Aug 19:00` - Set match time\n`/reschedule 05 Aug 20:00` - Change time (other captain approves)\n`/match result opponent:Name score:2-1` - Report result",inline=False)
    embed2.add_field(name="\U0001f4ca Stats",value="`/teaminfo name:Name` - Rank, MMR, progress bar\n`/leaderboard` - Full leaderboard (live)\n`/league status` - Season progress + top 4",inline=False)
    embed2.set_footer(text="Questions? Ask a League Admin. Good luck, Goons!")
    await i.followup.send(embed=embed2)

# ===== /matchrules =====
@bot.tree.command(name="matchrules",description="Post the match rules in this channel (League Admin)")
async def matchrules_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    rules_embed=discord.Embed(title="\U0001f4dc Match Rules",color=0x9b59b6)
    rules_embed.add_field(name="\U0001f525 Element Rules",value="Play **anything you want**, as long as there are no copies.\n- Example: You can't play 2x fire, but you can play fire and explosion.\n- **No duplicate ultimates** on a team (e.g. 3 players = 3 different ults)",inline=False)
    rules_embed.add_field(name="\U0001f3ae Game Rules",value="- **Control point:** OFF\n- **Damage zone:** ON\n- **Game mode:** Rounds\n- **Rounds to win:** 3 (BO5)\n- **Stage hazards:** ON\n- **Off map damage:** ON\n- **Multipliers:** All x1",inline=False)
    rules_embed.add_field(name="\U0001f5fa\ufe0f Map",value="Voted on after the match time is set.",inline=False)
    rules_embed.add_field(name="\u26a1 Toggles",value="- Powerups: OFF\n- Ultimates: ON\n- Techniques: ON\n- Blocking: ON\n- Perfect blocking: ON\n- Dashing: ON",inline=False)
    rules_embed.add_field(name="\u23f0 No-Shows & Forfeits",value="- If some of your team **doesn't show up within 15 minutes**, you either **play down a player** (e.g. 2v3 or 1v3) or **forfeit** (3-0 loss)\n- If a team **can't play** that week, they forfeit the match **3-0**\n- If you **can't agree on a time**, ping the League Admins - they decide based on who tried to schedule\n- Captains are responsible for the rules - a match played with invalid rules **must be replayed**",inline=False)
    rules_embed.add_field(name="\ud83d\udeab Substitutes",value="- **No subs** - nobody outside your team can join the match\n- You play with your own roster only",inline=False)
    await i.response.send_message(embed=rules_embed)

# ===== /scrimguide =====
@bot.tree.command(name="scrimguide",description="Post the scrim guide (League Admin)")
async def scrimguide_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    embed=discord.Embed(title="\U0001f3ae Scrims Guide",description="Casual practice matches - no league points, just fun.",color=0xe67e22)
    embed.add_field(name="\U0001f4dd How it works",value="`/create mixedscrim time:20:00` (League Admin)\n- **3v3 = 6 spots**\n- Click **Sign Up** in #mixed-scrims to join\n- Once 6 players are in, anyone else goes on the **Up Next** queue\n- If someone drops out, the **first person in the queue is pulled in** automatically",inline=False)
    embed.add_field(name="\U0001f9f5 The scrim thread",value="Every scrim gets its own thread - everyone signed up (and everyone in the queue) is added automatically.\n\nIn the thread you'll see the live list, plus:\n- **Sign Off** - drop out, and the next player in line takes your spot\n- **Ask for Queue** - ping the next player in line to fill a spot",inline=False)
    embed.add_field(name="\u23f0 Reminder",value="**5 minutes before start**, everyone in the scrim gets pinged in the thread.\nGet a lobby ready and drop the code in the chat!",inline=False)
    await i.response.send_message(embed=embed)

# ===== /setup /setchannel /league /team /disband /teaminfo /captain /match /stats /fa /mmr /test /schedule /reschedule /backup /restore =====
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
    bot_override=discord.PermissionOverwrite(send_messages=True,read_messages=True)
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
    cat_perm=_base()
    if goon:cat_perm[goon]=discord.PermissionOverwrite(read_messages=True)
    try:
        lcat=await i.guild.create_category("EGL",overwrites=cat_perm)
        ann=await i.guild.create_text_channel("announcements",category=lcat,overwrites=admin_only)
        guide_ch=await i.guild.create_text_channel("guide",category=lcat,overwrites=admin_only)
        gen=await i.guild.create_text_channel("general",category=lcat,overwrites=open_perm)
        tch=await i.guild.create_text_channel("teams",category=lcat,overwrites=open_perm)
        rec=await i.guild.create_forum("recruiting",category=lcat,overwrites=open_perm)
        mcat=await i.guild.create_category("Matches",overwrites=cat_perm)
        mat=await i.guild.create_text_channel("matches",category=mcat,overwrites=react_only)
        res=await i.guild.create_text_channel("results",category=mcat,overwrites=react_only)
        lb=await i.guild.create_text_channel("leaderboard",category=mcat,overwrites=admin_only)
        mr=await i.guild.create_text_channel("match-rules",category=mcat,overwrites=admin_only)
    except Exception as e:await i.followup.send(f"\u274c Failed: {e}");return
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO setup_data VALUES(?,?,?,?,?,?,?,?,?,?)",(gid,"EGL",str(lcat.id),str(mcat.id),str(ann.id),str(gen.id),str(tch.id),"",str(mat.id),str(res.id)))
        await db.commit()
    msg="\u2694\ufe0f **Elite Goon League is live.**\nAll channels and permissions have been configured.\n\n"
    msg+=f"{ann.mention} \u2014 Announcements\n"
    msg+=f"{guide_ch.mention} \u2014 Player guide\n"
    msg+=f"{gen.mention} \u2014 General chat\n"
    msg+=f"{tch.mention} \u2014 Team threads\n"
    msg+=f"{rec.mention} \u2014 Recruiting (looking for team)\n"
    msg+=f"{mat.mention} \u2014 Match schedule\n"
    msg+=f"{res.mention} \u2014 Results\n"
    msg+=f"{lb.mention} \u2014 Leaderboard\n"
    msg+=f"{mr.mention} \u2014 Match rules\n\n"
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
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM setup_data WHERE guild_id=?",(gid,));await db.commit()
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
        await db.execute("INSERT OR IGNORE INTO season VALUES(?,0,0)",(gid,));await db.commit()
    end_date=datetime.now(timezone.utc)+timedelta(weeks=SEASON_WEEKS)
    now=datetime.now(timezone.utc)
    days_until_sunday=(6-now.weekday())%7
    next_sunday=now.replace(hour=22,minute=0,second=0)+timedelta(days=days_until_sunday if days_until_sunday>0 else 0)
    if days_until_sunday==0 and now.hour>=22:next_sunday+=timedelta(days=7)
    sun_unix=int(next_sunday.replace(tzinfo=timezone.utc).timestamp())
    if sd.get("announcements_ch"):
        ann=i.guild.get_channel(int(sd["announcements_ch"]))
        if ann:
            await ann.send(f"\U0001f3c6 **{name}** has begun!\n\n\u2022 **{SEASON_WEEKS}-week season**  -  every team plays every other team\n\u2022 Matches generated **every Sunday at 10pm GMT** (<t:{sun_unix}:t> your time)\n\u2022 End of regular season: **{end_date.strftime('%d %b %Y')}**\n\u2022 Top 4 advance to Finals, then Grand Final\n\nGood luck, Goons!")
    await i.response.send_message(f"\u2694\ufe0f **{name}** season started!")
@league.command(name="delete",description="Delete league")
async def league_delete(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    async with aiosqlite.connect(DB)as db:
        for tbl in("config","teams","members","fa","matches","season"):await db.execute(f"DELETE FROM {tbl} WHERE guild_id=?",(gid,))
        await db.commit()
    await i.response.send_message(f"\U0001f5d1\ufe0f **{c['name']}** deleted.")
@league.command(name="info",description="League info")
async def league_info(i):
    if not await need_league(i):return
    c=await cfg_get(str(i.guild_id));ts=await teams_all(str(i.guild_id))
    await i.response.send_message(f"\u2694\ufe0f **{c['name']}**\nTeams:{len(ts)}|{SEASON_WEEKS}w")
@league.command(name="finals",description="Start Top-4 finals")
async def league_finals(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("\u274c No league.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT finals_generated FROM season WHERE guild_id=?",(gid,))as cur:row=await cur.fetchone()
    if row and row["finals_generated"]:await i.response.send_message("\u274c Already started.",ephemeral=True);return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)[:4]
    if len(ts)<4:await i.response.send_message(f"\u274c Need 4 teams, have {len(ts)}.",ephemeral=True);return
    names=[t["display"]for t in ts]
    pairs=[(names[x],names[y])for x in range(4)for y in range(x+1,4)]
    async with aiosqlite.connect(DB)as db:
        for a,b in pairs:await db.execute("INSERT INTO matches VALUES(?,?,0,?,?,NULL,NULL,NULL,?,NULL,1,NULL,NULL,NULL,NULL)",(str(uuid.uuid4())[:8],gid,a,b,datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE season SET finals_generated=1 WHERE guild_id=?",(gid,));await db.commit()
    ch=i.guild.get_channel(int(c["matches_ch"]))
    if ch:
        seed_txt="\n".join(f"{n+1}. **{nm}**"for n,nm in enumerate(names))
        match_txt="\n".join(f"Match {n+1}: **{a}** vs **{b}**"for n,(a,b)in enumerate(pairs))
        await ch.send(f"\U0001f3c6 **TOP 4 FINALS**\n\nSeedings:\n{seed_txt}\n\nMatches:\n{match_txt}\n\nAfter all 6 matches, use `/league grandfinal`!")
    ann=i.guild.get_channel(int(c["announcements_ch"]))if c.get("announcements_ch")else None
    if ann:await ann.send(f"\U0001f3c6 **The Top 4 Finals have begun!**\n\nTop 4:\n{seed_txt}\n\nCheck <#{c['matches_ch']}> for the schedule!")
    await i.response.send_message(f"\U0001f3c6 Finals started! {len(pairs)} matches in <#{c['matches_ch']}>.")
@league.command(name="grandfinal",description="Generate Grand Final")
async def league_grandfinal(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("\u274c No league.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT team1,team2,winner FROM matches WHERE guild_id=? AND is_finals=1 AND winner IS NOT NULL",(gid,))as c2:results=await c2.fetchall()
    if not results:await i.response.send_message("\u274c No finals results yet.",ephemeral=True);return
    wins={}
    for r in results:
        w=r["winner"]
        if w:wins[w]=wins.get(w,0)+1
    if len(wins)<2:await i.response.send_message("\u274c Not enough results.",ephemeral=True);return
    top2=sorted(wins.items(),key=lambda x:x[1],reverse=True)[:2]
    t1,t2=top2[0][0],top2[1][0]
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO matches VALUES(?,?,0,?,?,NULL,NULL,NULL,?,NULL,2,NULL,NULL,NULL,NULL)",(str(uuid.uuid4())[:8],gid,t1,t2,datetime.now(timezone.utc).isoformat()))
        await db.commit()
    ch=i.guild.get_channel(int(c["matches_ch"]))
    if ch:await ch.send(f"\U0001f3c6\U0001f525 **GRAND FINAL**\n\n**{t1}** vs **{t2}**\n\nMay the best team win!")
    await i.response.send_message(f"\U0001f3c6 Grand Final: **{t1}** vs **{t2}**!")
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
        async with aiosqlite.connect(DB)as db:await db.execute("INSERT INTO members VALUES(?,?,?)",(str(i.guild_id),s.tn.lower(),str(i.user.id)));await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",(str(i.guild_id),str(i.user.id)));await db.commit()
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
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT INTO teams VALUES(?,?,?,?,0,0,?,NULL,NULL,?,?)",(gid,name.lower(),name,uid,start_mmr,datetime.now(timezone.utc).isoformat(),clantag));await db.execute("INSERT INTO members VALUES(?,?,?)",(gid,name.lower(),uid));await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",(gid,uid));await db.commit()
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
    c=await cfg_get(gid);teams_ch=c.get("teams_ch")if c else None;view=InviteView(player.id,d,i.user.id,gid)
    if teams_ch and(ch:=i.guild.get_channel(int(teams_ch))):await ch.send(f"\U0001f4e8 {player.mention} invited to **{d}** by {i.user.mention}!",view=view);await i.response.send_message(f"\u2705 Sent in {ch.mention}.",ephemeral=True)
    else:await i.response.send_message(f"\U0001f4e8 {player.mention} invited to **{d}**!",view=view)
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

@bot.tree.command(name="resetteams",description="Reset all teams' MMR + record (League Admin only)")
async def resetteams(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:
        await db.execute("UPDATE teams SET mmr=1000,wins=0,losses=0 WHERE guild_id=?",(gid,))
        await db.commit()
    await i.followup.send("\u2705 All teams reset to 1000 MMR, 0W/0L.")

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
        await i.followup.send(f"\U0001f944 You already have the **Spoon** role!")
    else:
        try:
            await i.user.add_roles(role)
            await i.followup.send(f"\U0001f944 {i.user.mention} is now a **Spoon** - anyone can ping them with {role.mention}!")
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

@bot.tree.command(name="schedulereview",description="Analyze scheduling effort in this match thread (League Admin only)")
async def schedulereview(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("\u274c Use inside the match thread.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT id,team1,team2 FROM matches WHERE thread_id=?",(str(i.channel.id),))as cur:
            m=await cur.fetchone()
    if not m:await i.followup.send("\u274c No match in this thread.",ephemeral=True);return
    m=dict(m);t1=m["team1"];t2=m["team2"]
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        await db.execute("CREATE TABLE IF NOT EXISTS sched_events(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT,match_id TEXT,team TEXT,action TEXT,detail TEXT,ts TEXT)")
        async with db.execute("SELECT team,action,detail,ts FROM sched_events WHERE match_id=? ORDER BY id",(m["id"],))as cur:
            evs=await cur.fetchall()
    if not evs:await i.followup.send("\u274c No scheduling activity recorded for this match yet.",ephemeral=True);return
    stats={}
    for ev in evs:
        ev=dict(ev);tm=ev["team"]
        s=stats.setdefault(tm,{"propose":0,"counter":0,"confirm":0,"decline":0,"details":[]})
        if ev["action"]in s:s[ev["action"]]+=1
        if ev["detail"]:s["details"].append(f"{ev['action']}: {ev['detail']}")
    for tm in[t1,t2]:stats.setdefault(tm,{"propose":0,"counter":0,"confirm":0,"decline":0,"details":[]})
    def score(x):return x["propose"]*3+x["counter"]*2+x["confirm"]*2-x["decline"]*2
    s1=stats[t1];s2=stats[t2]
    def line(tm,s):return f"**{tm}**: proposals {s['propose']} \u00b7 counters {s['counter']} \u00b7 confirms {s['confirm']} \u00b7 declines {s['decline']}"
    verdict=""
    if score(s1)>score(s2):verdict=f"\n\n\u2705 **{t1}** tried harder to schedule."
    elif score(s2)>score(s1):verdict=f"\n\n\u2705 **{t2}** tried harder to schedule."
    else:verdict="\n\n\u26a0\ufe0f Both teams showed similar effort."
    lines=[line(t1,s1),line(t2,s2)]
    if s1["details"]:lines.append(f"\n**{t1}:** "+"; ".join(s1["details"][-5:]))
    if s2["details"]:lines.append(f"**{t2}:** "+"; ".join(s2["details"][-5:]))
    embed=discord.Embed(title="\U0001f4c5 Scheduling Review",description="\n".join(lines)+verdict,color=0x5865F2)
    await i.followup.send(embed=embed,ephemeral=True)

@bot.tree.command(name="closematch",description="Force close a match (League Admin only, in match thread)")
@app_commands.choices(result=[
    app_commands.Choice(name="Team 1 wins",value="team1"),
    app_commands.Choice(name="Team 2 wins",value="team2"),
    app_commands.Choice(name="Cancel (no changes)",value="cancel"),
    app_commands.Choice(name="Both forfeit (small MMR penalty, no W/L)",value="bothforfeit"),
])
async def closematch(i,result:str):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("\u274c Use inside the match thread.",ephemeral=True);return
    gid=str(i.guild_id)
    await i.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        await db.execute("CREATE TABLE IF NOT EXISTS closed_matches(guild_id TEXT PRIMARY KEY)")
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=? AND winner IS NULL",(str(i.channel.id),gid))as cur:
            row=await cur.fetchone()
    if not row:await i.followup.send("\u274c No open match in this thread (or already resolved).",ephemeral=True);return
    row=dict(row);t1=row["team1"];t2=row["team2"]
    c=await cfg_get(gid)
    rc=None
    if c and c.get("results_ch"):
        try:rc=i.guild.get_channel(int(c["results_ch"]))
        except:rc=None
    if result=="cancel":
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE matches SET score='canceled',winner='canceled',reporter=? WHERE id=?",(str(i.user.id),row["id"]))
            await db.commit()
        await i.followup.send(f"\u274c Match **{t1} vs {t2}** canceled. No MMR or W/L changes.",ephemeral=True)
        if rc:
            try:await rc.send(f"\U0001f6ab **{t1} vs {t2}**\nCanceled by an admin - no MMR or W/L changes.")
            except:pass
    elif result=="bothforfeit":
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE teams SET mmr=mmr-10 WHERE guild_id=? AND name IN (?,?)",(gid,t1.lower(),t2.lower()))
            await db.execute("UPDATE matches SET score='forfeit',winner='both-forfeit',reporter=? WHERE id=?",(str(i.user.id),row["id"]))
            await db.commit()
        await i.followup.send(f"\u26a0\ufe0f **{t1} vs {t2}** closed: both forfeit (-10 MMR each, no W/L).",ephemeral=True)
        if rc:
            try:await rc.send(f"\u26a0\ufe0f **{t1} vs {t2}**\nBoth teams forfeit - no W/L change, -10 MMR each.")
            except:pass
    else:
        # team1 or team2 wins
        winner=t1 if result=="team1" else t2
        loser=t2 if result=="team1" else t1
        wt=await team_get(gid,winner);lt=await team_get(gid,loser)
        wmmr=int(wt["mmr"]);lmmr=int(lt["mmr"])
        expected=0.5 if wmmr==lmmr else 1/(1+10**((lmmr-wmmr)/400))
        delta=round(50*(1-expected))
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,winner.lower()))
            await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,loser.lower()))
            await db.execute("UPDATE matches SET score='forfeit',winner=?,reporter=? WHERE id=?",(winner,str(i.user.id),row["id"]))
            await db.commit()
        await i.followup.send(f"\U0001f3c6 **{winner}** wins by forfeit over **{loser}** ({delta} MMR).",ephemeral=True)
        if rc:
            try:await rc.send(f"\u26a1 **{winner} 3-0 {loser}**\nWinner: **{winner}**\nClosed by an admin (forfeit).")
            except:pass
    # Archive the thread
    try:
        await i.channel.send(f"\U0001f512 Match closed by {i.user.mention}.")
        await i.channel.edit(archived=True,locked=True)
    except:pass

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
bot.tree.add_command(cap)

mat=app_commands.Group(name="match",description="Match")
@mat.command(name="result",description="Report result (captain)")
@app_commands.describe(opponent="Opponent",score="e.g. 3-0")
async def match_report(i,opponent:str,score:str):
    d=await need_captain(i)
    if not d:return
    await i.response.defer()
    gid=str(i.guild_id);opp=await team_get(gid,opponent)
    if not opp:await i.followup.send("\u274c Not found.",ephemeral=True);return
    if opp["name"]==d.lower():await i.followup.send("\u274c Self.",ephemeral=True);return
    try:our,their=map(int,score.strip().split("-"))
    except:await i.followup.send("\u274c 3-0 / 3-1 / 3-2",ephemeral=True);return
    won=our>their;winner=d if won else opp["display"]
    my_t=await team_get(gid,d);opp_t=await team_get(gid,opp["name"])
    my_mmr=int(my_t["mmr"]);opp_mmr=int(opp_t["mmr"])
    if my_mmr==opp_mmr:expected=0.5
    else:expected=1/(1+10**((opp_mmr-my_mmr)/400))
    # Base MMR swing: beating stronger = more, beating weaker = less
    base=50*(1-expected)if won else 50*expected
    # BO5 margin bonus: sweep (3-0) = more, close (3-2) = less
    margin=abs(our-their)
    mult={3:1.25,2:1.0,1:0.75}.get(margin,1.0)
    delta=round(base*mult)
    c=await cfg_get(gid)
    oc=i.guild.get_member(int(opp_t["captain_id"]))
    if not oc:await i.followup.send("\u274c Other captain not found.",ephemeral=True);return
    su="+"if won else"-";st="-"if won else"+"
    v=ResultConfirmView(opp_t["captain_id"],d,opp["name"],score,won,winner,delta,gid,c)
    await i.followup.send(f"\u26a1 {i.user.mention} reports: **{d} {score} {opp['display']}** - Winner: **{winner}**\n{d}({get_rank(my_mmr)}) {su}{delta} | {opp['display']}({get_rank(opp_mmr)}) {st}{delta}\n\n{oc.mention} please confirm:",view=v)
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
    await i.response.send_message(f"\U0001f4ca **{t['display']}** Rank:{get_rank(old)}\u2192{get_rank(new)}")
bot.tree.add_command(mmr)

test=app_commands.Group(name="test",description="Test (Admin)")
@test.command(name="generatematches",description="Force generate")
async def test_gen(i):
    if not await need_admin(i):return
    gid=str(i.guild_id);c=await cfg_get(gid)
    if not c:await i.response.send_message("\u274c No league.",ephemeral=True);return
    await i.response.defer();n=await gen_matches(i.guild,c,force=True)
    await i.followup.send(f"\u2705 {n or 0} matches in <#{c['matches_ch']}>.")
bot.tree.add_command(test)

@bot.tree.command(name="schedule",description="Set match time GMT (match thread)")
@app_commands.describe(datetime_str=SCHED_HELP)
async def schedule_cmd(i,datetime_str:str):
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("\u274c Match threads only.",ephemeral=True);return
    d=await need_captain(i)
    if not d:return
    try:
        dt=parse_schedule(datetime_str)
        sched=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
    except Exception as ex:await i.response.send_message(f"\u274c {ex}. Try: 05 Aug 20:00 or 05 Aug 8pm",ephemeral=True);return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=?",(str(i.channel.id),gid))as cur:row=await cur.fetchone()
    if not row:await i.response.send_message("\u274c No match here.",ephemeral=True);return
    row=dict(row)
    other=row["team2"]if d==row["team1"]else row["team1"]
    ot=await team_get(gid,other)
    if not ot:await i.response.send_message("\u274c Other team gone.",ephemeral=True);return
    oc=i.guild.get_member(int(ot["captain_id"]))
    if not oc:await i.response.send_message("\u274c Other captain not found.",ephemeral=True);return
    await record_sched_event(gid,row["id"],d,"propose",sched)
    v=ScheduleConfirmView(row["id"],ot["captain_id"],str(i.user.id),sched,unix,str(i.channel.id))
    await i.response.send_message(f"\U0001f4c5 {i.user.mention} proposes: **{sched}**\n\U0001f550 Your time: <t:{unix}:f>\n\n{oc.mention} please confirm:",view=v)

@bot.tree.command(name="reschedule",description="Reschedule (other captain approves)")
@app_commands.describe(datetime_str=SCHED_HELP)
async def reschedule_cmd(i,datetime_str:str):
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("\u274c Match threads only.",ephemeral=True);return
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id)
    try:
        dt=parse_schedule(datetime_str)
        nt=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
    except:await i.response.send_message(f"\u274c Format: `{SCHED_HELP}`",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE thread_id=? AND guild_id=?",(str(i.channel.id),gid))as cur:row=await cur.fetchone()
    if not row:await i.response.send_message("\u274c No match here.",ephemeral=True);return
    row=dict(row);mt=await team_by_player(gid,str(i.user.id))
    if not mt:await i.response.send_message("\u274c Not on team.",ephemeral=True);return
    other=row["team2"]if mt["display"]==row["team1"]else row["team1"];ot=await team_get(gid,other)
    if not ot:await i.response.send_message("\u274c Other team gone.",ephemeral=True);return
    oc=i.guild.get_member(int(ot["captain_id"]))
    if not oc:await i.response.send_message("\u274c Other captain gone.",ephemeral=True);return
    await record_sched_event(gid,row["id"],d,"propose",nt)
    v=ScheduleConfirmView(row["id"],ot["captain_id"],str(i.user.id),nt,unix,str(i.channel.id))
    await i.response.send_message(f"\U0001f4c5 {i.user.mention} wants **{nt}** (<t:{unix}:f>).\n{oc.mention} approve?",view=v)

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
    except:await i.response.send_message("\u274c Try: 20:00, 20.00, 8pm, 8:30pm",ephemeral=True);return
    await i.response.defer(ephemeral=True)
    if not has_scrims(i.guild):await i.followup.send("\u274c Run `/scrimbot setup` first.",ephemeral=True);return
    gid=str(i.guild_id);max_p=6;today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title="Mixed Scrim - 3V3"
    msc=None
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            for ch in cat.text_channels:
                if ch.name=="mixed-scrims":msc=ch;break
    if not msc:await i.response.send_message("\u274c No #mixed-scrims channel.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date=?",(gid,today));await db.commit()
    embed=discord.Embed(title=title,description=f"*{max_p} spots | Click Sign Up!*",color=0x5865F2)
    embed.add_field(name="Signed Up",value=f"0/{max_p}",inline=True)
    embed.add_field(name="\u23f0 Time (GMT)",value=scrim_time,inline=True)
    embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
    embed.set_footer(text="Click Sign Up to join")
    view=ScrimSignupView(gid,today);msg=await msc.send(embed=embed,view=view)
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT OR REPLACE INTO scrim_sessions(guild_id,date,thread_id,msg_id,max_players,scrim_title,unix_time) VALUES(?,?,NULL,?,?,?,?)",(gid,today,str(msg.id),max_p,title,unix));await db.commit()
    # Create the scrim thread
    try:
        th=await msc.create_thread(name=f"Scrim {scrim_time} GMT - 3V3",type=discord.ChannelType.public_thread,auto_archive_duration=1440)
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE scrim_sessions SET thread_id=? WHERE guild_id=? AND date=?",(str(th.id),gid,today));await db.commit()
        await update_scrim_thread(gid,today)
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
    if not ts:return
    medals=["\U0001f947","\U0001f948","\U0001f949"]
    rows=[]
    for n,t in enumerate(ts[:25]):
        pos=medals[n]if n<3 else f"`{n+1}.`"
        rows.append(f"{pos} **{t['display']}** \u00b7 {t['wins']}W {t['losses']}L \u00b7 `{t['mmr']}`")
    if len(ts)>25:rows.append(f"*...and {len(ts)-25} more teams*")
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4ca {season_name} Leaderboard",description="\n".join(rows),color=0x5865F2)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Updates automatically")
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
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Updates automatically")
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
    if not ts:return
    lines=[]
    for t in ts[:25]:
        cap_id=t["captain_id"]
        sorted_uids=sorted(t["members"],key=lambda u:0 if u==cap_id else 1)
        lines.append(f"**{t['display']}** ({len(t['members'])}/{MAX_TEAM}) \u00b7 {get_rank(t['mmr'])}")
        for uid in sorted_uids:
            m=guild.get_member(int(uid))
            nm=m.display_name if m else f"<@{uid}>"
            lines.append(f"\U0001f451 {nm}"if uid==cap_id else f"\u00b7 {nm}")
        lines.append("")
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4cb {season_name} Rosters",description="\n".join(lines),color=0x57F287)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Updates automatically")
    try:
        msg=await ch.fetch_message(int(r[1]))
        await msg.edit(embed=embed)
    except:pass

@bot.tree.command(name="teaminfoall",description="Post/refresh the team roster list (League Admin only)")
async def teaminfoall_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    ro_ch=None
    for cat in i.guild.categories:
        for ch in cat.text_channels:
            if ch.name=="rosters":ro_ch=ch;break
    if not ro_ch:await i.response.send_message("\u274c No #rosters channel. Create one first.",ephemeral=True);return
    if i.channel.id!=ro_ch.id:await i.response.send_message(f"\u274c Use this in {ro_ch.mention}.",ephemeral=True);return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    if not ts:await i.response.send_message("No teams yet.",ephemeral=True);return
    lines=[]
    for t in ts[:25]:
        cap_id=t["captain_id"]
        sorted_uids=sorted(t["members"],key=lambda u:0 if u==cap_id else 1)
        lines.append(f"**{t['display']}** ({len(t['members'])}/{MAX_TEAM}) \u00b7 {get_rank(t['mmr'])}")
        for uid in sorted_uids:
            m=i.guild.get_member(int(uid))
            nm=m.display_name if m else f"<@{uid}>"
            lines.append(f"\U0001f451 {nm}"if uid==cap_id else f"\u00b7 {nm}")
        lines.append("")
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4cb {season_name} Rosters",description="\n".join(lines),color=0x57F287)
    embed.set_footer(text=f"{len(ts)} teams \u00b7 Updates automatically")
    async with aiosqlite.connect(DB)as db:
        await db.execute("CREATE TABLE IF NOT EXISTS roster_state(guild_id TEXT PRIMARY KEY,ch TEXT,msg TEXT)")
        async with db.execute("SELECT ch,msg FROM roster_state WHERE guild_id=?",(gid,))as cur:
            old=await cur.fetchone()
    if old and old[0]:
        old_ch=i.guild.get_channel(int(old[0]))
        if old_ch and old[1]:
            try:
                old_msg=await old_ch.fetch_message(int(old[1]))
                await old_msg.delete()
            except:pass
    msg=await ro_ch.send(embed=embed)
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT OR REPLACE INTO roster_state VALUES(?,?,?)",(gid,str(ro_ch.id),str(msg.id)))
        await db.commit()
    await i.response.send_message("\u2705 Rosters posted!",ephemeral=True)

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
    await i.followup.send("\u2705 Database restored!",ephemeral=True)

# ===== Scheduler =====
@tasks.loop(hours=1)
async def weekly_check():
    now=datetime.now(timezone.utc)
    if now.weekday()!=6 or now.hour!=22:return
    for g in bot.guilds:
        c=await cfg_get(str(g.id))
        if c:await gen_matches(g,c)
@weekly_check.before_loop
async def bef():await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def scrim_check():
    now=datetime.now(timezone.utc)
    hour=now.hour;minute=now.minute;today=now.strftime("%Y-%m-%d")
    for g in bot.guilds:
        gid=str(g.id)
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT guild_id FROM setup_data WHERE guild_id=?",(gid,))as c:
                if not await c.fetchone():continue
        msc=None
        for cat in g.categories:
            if cat.name=="Scrims":
                for ch in cat.text_channels:
                    if ch.name=="mixed-scrims":msc=ch;break
        if not msc:continue
        if not has_scrims(g):continue
        # Every 5 min - refresh embed
        if minute%5==0:
            await update_scrim_embed(gid,today)
        # Check for 5-min ping
        async with aiosqlite.connect(DB)as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT * FROM scrim_sessions WHERE guild_id=? AND date=? AND unix_time IS NOT NULL",(gid,today))as c:
                sessions=await c.fetchall()
        for sess in sessions:
            sess=dict(sess)
            if not sess.get("unix_time"):continue
            dt=datetime.fromtimestamp(sess["unix_time"],tz=timezone.utc)
            diff=(dt-now).total_seconds()
            if 0<diff<=300 and not sess.get("pinged"):
                members=await scrim_signups_active(gid,today)
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
                    await db.execute("UPDATE scrim_sessions SET pinged=1 WHERE guild_id=? AND date=?",(gid,today));await db.commit()
            if diff<=-5400:
                await close_scrim(gid,today,sess)
                continue
        # Midnight cleanup
        if hour==0 and minute==0:
            async with aiosqlite.connect(DB)as db:
                await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date<?",(gid,today))
                await db.execute("DELETE FROM scrim_sessions WHERE guild_id=? AND date<?",(gid,today));await db.commit()

@scrim_check.before_loop
async def scrim_bef():await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def match_reminders():
    now=datetime.now(timezone.utc)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM matches WHERE scheduled IS NOT NULL AND winner IS NULL AND score IS NULL")as c:
            rows=await c.fetchall()
    for row in rows:
        row=dict(row)
        sched=row.get("scheduled")
        if not sched:continue
        try:
            dt=datetime.strptime(sched.replace(" GMT",""),SCHED_FMT).replace(year=now.year,tzinfo=timezone.utc)
            diff=(dt-now).total_seconds()
            if 3540<=diff<=3660:  # 59-61 min window
                g=bot.get_guild(int(row["guild_id"]))
                if not g:continue
                th=g.get_thread(int(row["thread_id"]))if row.get("thread_id")else None
                if th:
                    unix=int(dt.timestamp())
                    await th.send(f"\u23f0 **Match starts in 1 hour!** <t:{unix}:f>\n\nBoth teams be ready! Use `/match result` after the match.")
        except:continue

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

bot.tree.add_command(scrimbot_grp)
bot.tree.add_command(setup)

if __name__=="__main__":
    if not TOKEN:print("\n\u274c Set DISCORD_BOT_TOKEN!\n")
    else:bot.run(TOKEN)

