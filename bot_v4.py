# Elements Divided League Bot v5 — Elite Goon League
# pip install discord.py aiosqlite
# 2v2 league, max 3 players/team, rank-based system
import asyncio,logging,os,uuid,random
from datetime import datetime,timezone,timedelta
import aiosqlite,discord
from discord import app_commands
from discord.ext import commands,tasks
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)
TOKEN=os.environ.get("DISCORD_BOT_TOKEN","")
DB="league.db";DEFAULT_MMR=1000;MAX_TEAM=3;SEASON_WEEKS=8
ADMIN_ROLE="League Admin";TESTER_ROLE="EGL Tester"
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
RANKS=[(0,"Adept"),(900,"Lotus"),(1000,"Monk"),(1050,"Warden"),(1100,"Avatar"),(1150,"Raava")]
intents=discord.Intents.default();intents.members=True;intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)

async def init_db():
    async with aiosqlite.connect(DB)as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS config(guild_id TEXT PRIMARY KEY,name TEXT,admin_id TEXT,announcements_ch TEXT,matches_ch TEXT,results_ch TEXT,general_ch TEXT,fa_ch TEXT,teams_ch TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS teams(guild_id TEXT,name TEXT,display TEXT,captain_id TEXT,wins INT DEFAULT 0,losses INT DEFAULT 0,mmr INT DEFAULT 1000,thread_id TEXT,role_id TEXT,created_at TEXT,PRIMARY KEY(guild_id,name));
        CREATE TABLE IF NOT EXISTS members(guild_id TEXT,team_name TEXT,user_id TEXT,PRIMARY KEY(guild_id,team_name,user_id));
        CREATE TABLE IF NOT EXISTS fa(guild_id TEXT,user_id TEXT,username TEXT,joined_at TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,guild_id TEXT,week INT,team1 TEXT,team2 TEXT,score TEXT,winner TEXT,reporter TEXT,created_at TEXT,thread_id TEXT,is_finals INT DEFAULT 0,map TEXT,scheduled TEXT,reschedule_by TEXT,reschedule_to TEXT);
        CREATE TABLE IF NOT EXISTS season(guild_id TEXT PRIMARY KEY,weeks_done INT DEFAULT 0,finals_generated INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS player_history(guild_id TEXT,user_id TEXT,last_mmr INT DEFAULT 1000,cooldown_until TEXT,PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS guild_settings(guild_id TEXT PRIMARY KEY,teams_ch TEXT);
        CREATE TABLE IF NOT EXISTS setup_data(guild_id TEXT PRIMARY KEY,league_name TEXT,league_category_id TEXT,matches_category_id TEXT,announcements_ch TEXT,general_ch TEXT,teams_ch TEXT,fa_ch TEXT,matches_ch TEXT,results_ch TEXT);
        CREATE TABLE IF NOT EXISTS scrim_sessions(guild_id TEXT,date TEXT,thread_id TEXT,msg_id TEXT,max_players INT DEFAULT 6,PRIMARY KEY(guild_id,date));
        CREATE TABLE IF NOT EXISTS scrim_signups(guild_id TEXT,date TEXT,user_id TEXT,position INT,PRIMARY KEY(guild_id,date,user_id));
        """)
        # Migrate: add new columns to scrim_sessions if missing
        for col,typ in [("max_players","INT DEFAULT 6"),("scrim_title","TEXT"),("unix_time","INT")]:
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

async def fa_exists(gid,uid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT 1 FROM fa WHERE guild_id=? AND user_id=?",(gid,uid))as c:
            return bool(await c.fetchone())

async def fa_all(gid):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id,username FROM fa WHERE guild_id=?",(gid,))as c:
            return[{"user_id":r[0],"username":r[1]}for r in await c.fetchall()]

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
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups WHERE guild_id=? AND date=? ORDER BY position ASC LIMIT 6",(gid,date))as c:
            return[r[0]for r in await c.fetchall()]

async def scrim_next_queue(gid,date):
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT user_id FROM scrim_signups WHERE guild_id=? AND date=? AND position>=6 ORDER BY position ASC LIMIT 1",(gid,date))as c:
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

async def add_roles(guild,member,team_display,captain=False):
    try:
        fa=find_role(guild,"Free Agent")
        if fa and fa in member.roles:await member.remove_roles(fa)
        p=find_role(guild,"Player")
        if p:await member.add_roles(p)
        tr=find_role(guild,team_display)or await guild.create_role(name=team_display,mentionable=True)
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
class FAButtonView(discord.ui.View):
    def __init__(self,tn,cid,gid,fa_ch):
        super().__init__(timeout=121);self.tn=tn;self.cid=cid;self.gid=gid;self.fa_ch=fa_ch;self.responded=set()
    @discord.ui.button(label="I'm Available!",style=discord.ButtonStyle.green,emoji="\u2795")
    async def avail(self,i,btn):
        uid=str(i.user.id)
        if not await fa_exists(self.gid,uid):await i.response.send_message("Not a FA.",ephemeral=True);return
        if uid==self.cid:await i.response.send_message("Your own request.",ephemeral=True);return
        self.responded.add(uid);await i.response.send_message("Listed!",ephemeral=True)
    async def on_timeout(self):
        for c in self.children:c.disabled=True
        if hasattr(self,"msg")and self.msg:
            try:await self.msg.edit(content=f"Time up! {len(self.responded)} FA(s).",view=self)
            except:pass
        if not self.responded:
            g=bot.get_guild(int(self.gid))
            if self.fa_ch and g:
                ch=g.get_channel(int(self.fa_ch))
                if ch:await ch.send(f"No FAs responded to **{self.tn}** sub request.")
            return
        opts=[]
        g=bot.get_guild(int(self.gid))
        if not g:return
        for uid in self.responded:
            m=g.get_member(int(uid))
            if m:opts.append(discord.SelectOption(label=m.display_name,value=uid))
        if not opts:return
        v=FASelectView(self.tn,self.cid,self.gid,opts,self.fa_ch)
        if self.fa_ch:
            ch=g.get_channel(int(self.fa_ch))
            cap=g.get_member(int(self.cid))
            if ch:
                try:await ch.send(f"{cap.mention if cap else ''} Pick a sub for **{self.tn}**:",view=v)
                except:pass

class FASelectView(discord.ui.View):
    def __init__(self,tn,cid,gid,opts,fa_ch):
        super().__init__(timeout=300);self.tn=tn;self.cid=cid;self.gid=gid;self.fa_ch=fa_ch
        self.sel.options=opts
    @discord.ui.select(placeholder="Choose FA...",min_values=1,max_values=1)
    async def sel(self,i,menu):
        if str(i.user.id)!=self.cid:await i.response.send_message("Not for you.",ephemeral=True);return
        uid=menu.values[0];g=i.guild;t=await team_get(self.gid,self.tn)
        if not t:await i.response.send_message("Team gone.",ephemeral=True);return
        p=g.get_member(int(uid))
        if not p:await i.response.send_message("Left server.",ephemeral=True);return
        for c in self.children:c.disabled=True
        await i.response.edit_message(content=f"\u2705 {p.mention} is subbing for **{t['display']}**!",view=self)
        if t.get("thread_id"):
            th=g.get_thread(int(t["thread_id"]))
            if th:
                try:await th.add_user(p);await th.send(f"\U0001f44b {p.mention} is subbing for **{t['display']}** this match!")
                except:pass
        if self.fa_ch and(ch:=g.get_channel(int(self.fa_ch))):await ch.send(f"\u2705 {p.mention} subbing for **{self.tn}**!")

class ScrimSignupView(discord.ui.View):
    def __init__(self,gid,date):
        super().__init__(timeout=None);self.gid=gid;self.date=date
    @discord.ui.button(label="Sign Up",style=discord.ButtonStyle.green,emoji="\u2795")
    async def signup(self,i,btn):
        gid=self.gid;date=self.date;uid=str(i.user.id)
        # Prevent duplicate
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT 1 FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid))as c:
                if await c.fetchone():await i.response.send_message("\u274c Already signed up!",ephemeral=True);return
        count=await scrim_signup_count(gid,date)
        max_p=await scrim_max_players(gid,date)
        async with aiosqlite.connect(DB)as db:
            await db.execute("INSERT OR IGNORE INTO scrim_signups VALUES(?,?,?,?)",(gid,date,uid,count));await db.commit()
        status="Active"if count<max_p else f"Queue (#{count-max_p+1})"
        await i.response.send_message(f"\u2705 Signed up! ({min(count+1,max_p)}/{max_p}) {status}",ephemeral=True)
        await update_scrim_embed(gid,date)
    @discord.ui.button(label="Leave",style=discord.ButtonStyle.red,emoji="\u274c")
    async def leave(self,i,btn):
        gid=self.gid;date=self.date;uid=str(i.user.id)
        async with aiosqlite.connect(DB)as db:
            async with db.execute("SELECT position FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid))as c:
                r=await c.fetchone()
            if not r:await i.response.send_message("\u274c Not signed up.",ephemeral=True);return
            pos=r[0];await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date=? AND user_id=?",(gid,date,uid));await db.commit()
        await i.response.send_message("\u274c Left the scrim.",ephemeral=True)
        # Also fix active limit based on max_players
        max_p=await scrim_max_players(gid,date)
        if pos<max_p:
            next_uid=await scrim_next_queue(gid,date)
            if next_uid:
                async with aiosqlite.connect(DB)as db:
                    await db.execute("INSERT OR REPLACE INTO scrim_signups VALUES(?,?,?,?)",(gid,date,next_uid,5));await db.commit()
                g=bot.get_guild(int(gid))
                if g:
                    m=g.get_member(int(next_uid))
                    ch=g.get_channel(int(i.channel_id))
                    if m and ch:await ch.send(f"\U0001f4e2 {m.mention} you're now in the scrim!")
        await update_scrim_embed(gid,date)

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
    if unix:embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
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
    @discord.ui.button(label="Approve",style=discord.ButtonStyle.green)
    async def approve(self,i,btn):
        if str(i.user.id)!=self.ocid:await i.response.send_message("Other captain only.",ephemeral=True);return
        async with aiosqlite.connect(DB)as db:await db.execute("UPDATE matches SET scheduled=? WHERE id=?",(self.nt,self.mid));await db.commit()
        for c in self.children:c.disabled=True
        await i.response.edit_message(content=f"Rescheduled to **{self.nt}**",view=self)
    @discord.ui.button(label="Deny",style=discord.ButtonStyle.red)
    async def deny(self,i,btn):
        if str(i.user.id)!=self.ocid:await i.response.send_message("Other captain only.",ephemeral=True);return
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
    names=[t["display"]for t in ts]
    if len(names)<2:return
    pairs=make_pairs(names,week)
    if not pairs:return
    match_ids=[]
    async with aiosqlite.connect(DB)as db:
        for a,b in pairs:
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
                await th.send(f"\u26a1 **{a} vs {b}** - Week {week}\n\nEveryone's here! Captains vote below then `/schedule {SCHED_HELP}` (GMT).")
                cap1_id=t1["captain_id"]if t1 else"";cap2_id=t2["captain_id"]if t2 else""
                class MV(discord.ui.View):
                    def __init__(s):super().__init__(timeout=604800);s.c1=cap1_id;s.c2=cap2_id;s.p={};s.mid=mid;s.gid=gid;s.th=th
                    async def _v(s,i,mn):
                        uid=str(i.user.id)
                        if uid not in(s.c1,s.c2):await i.response.send_message("Captains only.",ephemeral=True);return
                        if uid in s.p:await i.response.send_message("Already voted.",ephemeral=True);return
                        s.p[uid]=mn;await i.response.send_message(f"{mn}!",ephemeral=True)
                        if len(s.p)==2:
                            vs=list(s.p.values())
                            res=vs[0]if vs[0]==vs[1]else random.choice(vs)
                            coin=" (coin flip!)"if vs[0]!=vs[1]else""
                            for c in s.children:c.disabled=True
                            await i.edit_original_response(content=f"Map: **{res}**{coin}",view=None)
                            await s.th.send(f"\U0001f5fa\ufe0f Map: **{res}**{coin}\nUse `/schedule {SCHED_HELP}` (GMT).")
                            async with aiosqlite.connect(DB)as db3:await db3.execute("UPDATE matches SET map=? WHERE id=?",(res,s.mid));await db3.commit()
                mv=MV()
                for mn in MAPS:
                    btn=discord.ui.Button(label=mn,style=discord.ButtonStyle.primary)
                    async def cb(i,mn=mn):await MV._v(mv,i,mn)
                    btn.callback=cb;mv.add_item(btn)
                await th.send("**\U0001f5fa\ufe0f Map Vote:**",view=mv)
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
    p1="\U0001f4d6 **EGL  -  Elite Goon League**\n"
    p1+="Our own 2v2 league for Elements Divided. Teams of up to 3, 8-week season, weekly matches.\n\n"
    p1+="\u2501"*22+"\n"
    p1+="\U0001f3c6 **The Basics**\n"
    p1+="Start at **Monk** rank, climb to Raava. Top 4 advance to Finals.\n"
    p1+="Matches every **Sunday 10pm GMT**. Map voted by captains.\n"
    p1+="Max 1 Free Agent sub per match. 24h cooldown after leaving.\n\n"
    p1+="\u2501"*22+"\n"
    p1+="\U0001f91d **Free Agents**\n"
    p1+="Subs who fill in when teams are short. FAs keep their role."
    await i.response.send_message(p1)
    p2="\U0001f4cb **Commands**\n\n"
    p2+="\u2694\ufe0f **Team**\n"
    p2+="`/team create name:Name` — Create team, become captain\n"
    p2+="`/team invite @player` — Invite someone *(captain)*\n"
    p2+="`/team roster` / `/teaminfo` / `/team leave` / `/disband`\n"
    p2+="`/captain swap @player` — Transfer captain\n\n"
    p2+="\U0001f91d **Free Agent**\n"
    p2+="`/fa register` / `unregister` / `list`\n"
    p2+="`/fa request` — Find a sub *(captain)\n\n"
    p2+="\U0001f4c5 **Match**\n"
    p2+="`/schedule 05 Aug 19:00` / `/reschedule` — Set match time\n"
    p2+="`/match result opponent:Name score:2-1` — Report result\n\n"
    p2+="\U0001f4ca **Stats**\n"
    p2+="`/stats team` `/league standings` `/league status`\n\n"
    p2+="\u2501"*22+"\n"
    p2+="*Questions? Ask a **League Admin**. Good luck, Goons!*"
    await i.followup.send(p2)

# ===== /scrimguide =====
@bot.tree.command(name="scrimguide",description="Post the scrim guide (League Admin)")
async def scrimguide_cmd(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    sg="\U0001f3ae **Scrims Guide**\n\n"
    sg+="Scrims are casual practice matches — no league points, just fun.\n\n"
    sg+="\u2501"*22+"\n"
    sg+="**Mixed Scrims**\n"
    sg+="Anyone can create one: `/create mixedscrim time:20:00 format:3v3`\n"
    sg+="- 3v3 = 6 spots | 2v2 = 4 spots\n"
    sg+="- People click **Sign Up** to join (leave anytime)\n"
    sg+="- Embed auto-updates with the player list\n"
    sg+="- 5 minutes before start, everyone gets pinged\n\n"
    sg+="**Time formats:** `20:00`, `8pm`, `8:30pm`\n\n"
    sg+="\u2501"*22+"\n"
    sg+="**Team Scrims**\n"
    sg+="Captains use `/teamscrim` in #team-scrims to find opponents.\n"
    sg+="This pings @TeamScrims with your team name.\n\n"
    sg+="\u2501"*22+"\n"
    sg+="**Setup:** Run `/setup scrimbot` first to create the Scrims channels.\n"
    sg+="**Guide:** Use `/scrimguide` to repost this anytime."
    await i.response.send_message(sg)

# ===== /setup /setchannel /league /team /disband /teaminfo /captain /match /stats /fa /mmr /test /schedule /reschedule /backup /restore =====
setup=app_commands.Group(name="setup",description="Bot setup")
@setup.command(name="run",description="Create all league channels (League Admin only)")
async def setup_run(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        async with db.execute("SELECT guild_id FROM setup_data WHERE guild_id=?",(gid,))as cur:
            if await cur.fetchone():await i.response.send_message("\u274c Already set up. Use `/setup reset` first.",ephemeral=True);return
    await i.response.defer()
    ev=i.guild.default_role;ar=discord.utils.get(i.guild.roles,name=ADMIN_ROLE)
    bot_override=discord.PermissionOverwrite(send_messages=True,read_messages=True)
    admin_only={ev:discord.PermissionOverwrite(send_messages=False,read_messages=True),i.guild.me:bot_override}
    if ar:admin_only[ar]=discord.PermissionOverwrite(send_messages=True,read_messages=True)
    react_only={ev:discord.PermissionOverwrite(send_messages=False,add_reactions=True,read_messages=True),i.guild.me:bot_override}
    if ar:react_only[ar]=discord.PermissionOverwrite(send_messages=True,read_messages=True)
    try:
        lcat=await i.guild.create_category("EGL")
        ann=await i.guild.create_text_channel("announcements",category=lcat,overwrites=admin_only)
        guide_ch=await i.guild.create_text_channel("guide",category=lcat,overwrites=admin_only)
        gen=await i.guild.create_text_channel("general",category=lcat)
        tch=await i.guild.create_text_channel("teams",category=lcat)
        fa=await i.guild.create_text_channel("free-agent",category=lcat)
        mcat=await i.guild.create_category("Matches")
        mat=await i.guild.create_text_channel("matches",category=mcat,overwrites=react_only)
        res=await i.guild.create_text_channel("results",category=mcat,overwrites=react_only)
        lb=await i.guild.create_text_channel("leaderboard",category=mcat,overwrites=admin_only)
    except Exception as e:await i.followup.send(f"\u274c Failed: {e}");return
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO setup_data VALUES(?,?,?,?,?,?,?,?,?,?)",(gid,"EGL",str(lcat.id),str(mcat.id),str(ann.id),str(gen.id),str(tch.id),str(fa.id),str(mat.id),str(res.id)))
        await db.commit()
    msg="\u2694\ufe0f **Elite Goon League is live.**\nAll channels and permissions have been configured.\n\n"
    msg+=f"{ann.mention} \u2014 Announcements\n"
    msg+=f"{guide_ch.mention} \u2014 Player guide\n"
    msg+=f"{gen.mention} \u2014 General chat\n"
    msg+=f"{tch.mention} \u2014 Team threads\n"
    msg+=f"{fa.mention} \u2014 Free agents\n"
    msg+=f"{mat.mention} \u2014 Match schedule\n"
    msg+=f"{res.mention} \u2014 Results\n"
    msg+=f"{lb.mention} \u2014 Leaderboard\n\n"
    msg+="Run `/league create` to start your first season.\nWant scrims? `/setup scrimbot` to add those channels."
    await i.followup.send(msg)
@setup.command(name="reset",description="Delete all bot channels and reset")
async def setup_reset(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    gid=str(i.guild_id)
    async with aiosqlite.connect(DB)as db:
        db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM setup_data WHERE guild_id=?",(gid,))as cur:row=await cur.fetchone()
    if not row:await i.response.send_message("\u274c Not set up yet.",ephemeral=True);return
    await i.response.defer()
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

@setup.command(name="scrimbot",description="Set up the Scrims category + channels (League Admin)")
async def setup_scrimbot(i):
    if not is_admin(i.user):await i.response.send_message(f"\u274c Need **{ADMIN_ROLE}**.",ephemeral=True);return
    await i.response.defer()
    try:
        scat=await i.guild.create_category("Scrims")
        msc=await i.guild.create_text_channel("mixed-scrims",category=scat)
        tsc=await i.guild.create_text_channel("team-scrims",category=scat)
    except Exception as e:await i.followup.send(f"\u274c Failed (may already exist): {e}");return
    await i.followup.send(f"\U0001f3ae **Scrims activated!**\n{msc.mention} - Mixed scrims\n{tsc.mention} - Team scrims\n\nUse `/create mixedscrim` and `/teamscrim` to get started.")

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
    next_sunday=now.replace(hour=18,minute=0,second=0)+timedelta(days=days_until_sunday if days_until_sunday>0 else 0)
    if days_until_sunday==0 and now.hour>=18:next_sunday+=timedelta(days=7)
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
@app_commands.describe(name="Team name")
async def team_create(i,name:str):
    gid=str(i.guild_id);uid=str(i.user.id)
    if ex:=await team_by_player(gid,uid):await i.response.send_message(f"\u274c On **{ex['display']}**.",ephemeral=True);return
    if await team_get(gid,name):await i.response.send_message(f"\u274c Exists.",ephemeral=True);return
    if await is_on_cooldown(gid,uid):await i.response.send_message("\u274c 24h cooldown.",ephemeral=True);return
    start_mmr=await get_player_mmr(gid,uid)
    await i.response.defer()
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT INTO teams VALUES(?,?,?,?,0,0,?,NULL,NULL,?)",(gid,name.lower(),name,uid,start_mmr,datetime.now(timezone.utc).isoformat()));await db.execute("INSERT INTO members VALUES(?,?,?)",(gid,name.lower(),uid));await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",(gid,uid));await db.commit()
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
            th=await target_ch.create_thread(name=name,type=discord.ChannelType.private_thread,auto_archive_duration=10080)
            thread_id=str(th.id);await th.add_user(i.user)
            await th.send(f"\u2694\ufe0f **{name}** thread!\nCaptain:{i.user.mention}\n`/team invite @player`")
        except Exception as e:log.warning("THREAD ERROR: %s",e)
    async with aiosqlite.connect(DB)as db:await db.execute("UPDATE teams SET role_id=?,thread_id=? WHERE guild_id=? AND name=?",(role_id,thread_id,gid,name.lower()));await db.commit()
    extra=f"\n\U0001f4cc <#{thread_id}>"if thread_id else""
    await i.followup.send(f"\u2694\ufe0f **{name}** created!\nCaptain:{i.user.mention}|Rank:{get_rank(start_mmr)}|1/{MAX_TEAM}{extra}")
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
@team.command(name="roster",description="Show roster")
@app_commands.describe(team="Team (blank=yours)")
async def team_roster(i,team:str=None):
    gid=str(i.guild_id);t=await team_get(gid,team)if team else await team_by_player(gid,str(i.user.id))
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    crown="\U0001f451";players="\n".join(f"\u2022 <@{m}> {crown if m==t['captain_id']else''}"for m in t["members"])
    await i.response.send_message(f"\u2694\ufe0f **{t['display']}** {t['wins']}W/{t['losses']}L  -  **{get_rank(t['mmr'])}**\n\n**Players ({len(t['members'])}/{MAX_TEAM}):**\n{players}")
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

@bot.tree.command(name="teaminfo",description="Team info")
@app_commands.describe(team="Team name")
async def teaminfo(i,team:str):
    t=await team_get(str(i.guild_id),team)
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    tot=t["wins"]+t["losses"];wr=f"{round(t['wins']/tot*100)}%"if tot else"N/A"
    crown="\U0001f451";players="\n".join(f"\u2022 <@{m}> {crown if m==t['captain_id']else''}"for m in t["members"])
    await i.response.send_message(f"\u2694\ufe0f **{t['display']}**\nRank:**{get_rank(t['mmr'])}**|{t['wins']}W/{t['losses']}L|WR:{wr}\n\n**Players ({len(t['members'])}/{MAX_TEAM}):**\n{players}")

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
@app_commands.describe(opponent="Opponent",score="e.g. 2-1")
async def match_report(i,opponent:str,score:str):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id);opp=await team_get(gid,opponent)
    if not opp:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    if opp["name"]==d.lower():await i.response.send_message("\u274c Self.",ephemeral=True);return
    try:our,their=map(int,score.strip().split("-"))
    except:await i.response.send_message("\u274c 2-1",ephemeral=True);return
    won=our>their;winner=d if won else opp["display"]
    my_t=await team_get(gid,d);opp_t=await team_get(gid,opp["name"])
    my_mmr=int(my_t["mmr"]);opp_mmr=int(opp_t["mmr"])
    if my_mmr==opp_mmr:expected=0.5
    else:expected=1/(1+10**((opp_mmr-my_mmr)/400))
    delta=round(50*(1-expected))if won else round(50*(0-expected))
    su="+"if won else"-";st="-"if won else"+"
    async with aiosqlite.connect(DB)as db:
        if won:
            await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,d.lower()))
            await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,opp["name"]))
        else:
            await db.execute("UPDATE teams SET losses=losses+1,mmr=mmr-? WHERE guild_id=? AND name=?",(delta,gid,d.lower()))
            await db.execute("UPDATE teams SET wins=wins+1,mmr=mmr+? WHERE guild_id=? AND name=?",(delta,gid,opp["name"]))
        await db.execute("INSERT INTO matches VALUES(?,?,0,?,?,?,?,?,?,NULL,0,NULL,NULL,NULL,NULL)",(str(uuid.uuid4())[:8],gid,d,opp["display"],score,winner,str(i.user.id),datetime.now(timezone.utc).isoformat()))
        await db.commit()
    c=await cfg_get(gid)
    if c and c.get("matches_ch"):
        mc=i.guild.get_channel(int(c["matches_ch"]))
        if mc:
            found_t=None
            for t in mc.threads:
                if d.lower()in t.name.lower()and opp["name"]in t.name.lower():found_t=t;break
            if not found_t:
                async for t in mc.archived_threads():
                    if d.lower()in t.name.lower()and opp["name"]in t.name.lower():found_t=t;break
            if found_t:
                try:
                    if found_t.archived:await found_t.edit(archived=False,locked=False)
                    await found_t.send(f"\u2705 **{d} {score} {opp['display']}** - {winner} wins!")
                    await found_t.edit(archived=True,locked=True)
                except:pass
    if c and c.get("results_ch"):
        rc=i.guild.get_channel(int(c["results_ch"]))
        if rc:await rc.send(f"\u26a1 **{d} {score} {opp['display']}**\nWinner:**{winner}**\nBy {i.user.mention}")
    await i.response.send_message(f"\u26a1 **{d} {score} {opp['display']}**\nWinner:**{winner}**\n{d}({get_rank(my_t['mmr'])}) +{delta}|{opp['display']}({get_rank(opp_t['mmr'])}) -{delta}")
bot.tree.add_command(mat)

st=app_commands.Group(name="stats",description="Stats")
@st.command(name="team",description="Team stats")
@app_commands.describe(team="Team")
async def stats_team(i,team:str=None):
    gid=str(i.guild_id);t=await team_get(gid,team)if team else await team_by_player(gid,str(i.user.id))
    if not t:await i.response.send_message("\u274c Not found.",ephemeral=True);return
    tot=t["wins"]+t["losses"];wr=f"{round(t['wins']/tot*100)}%"if tot else"N/A"
    await i.response.send_message(f"\U0001f4ca **{t['display']}**\nRank:**{get_rank(t['mmr'])}**|{t['wins']}W/{t['losses']}L|WR:{wr}\n{len(t['members'])}/{MAX_TEAM}|Capt:<@{t['captain_id']}>")
bot.tree.add_command(st)

fa=app_commands.Group(name="fa",description="Free agents")
@fa.command(name="register",description="Register")
async def fa_register(i):
    gid=str(i.guild_id);uid=str(i.user.id)
    if await team_by_player(gid,uid):await i.response.send_message("\u274c On team.",ephemeral=True);return
    if await fa_exists(gid,uid):await i.response.send_message("\u274c Already.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT INTO fa VALUES(?,?,?,?)",(gid,uid,i.user.display_name,datetime.now(timezone.utc).isoformat()));await db.commit()
    if r:=find_role(i.guild,"Free Agent"):
        try:await i.user.add_roles(r)
        except:pass
    await i.response.send_message(f"\U0001f7e2 {i.user.mention} is a **Free Agent**!")
@fa.command(name="unregister",description="Leave")
async def fa_unregister(i):
    gid=str(i.guild_id);uid=str(i.user.id)
    if not await fa_exists(gid,uid):await i.response.send_message("\u274c Not FA.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM fa WHERE guild_id=? AND user_id=?",(gid,uid));await db.commit()
    if r:=find_role(i.guild,"Free Agent"):
        try:await i.user.remove_roles(r)
        except:pass
    await i.response.send_message(f"\U0001f534 {i.user.mention} removed.")
@fa.command(name="list",description="List")
async def fa_list(i):
    pool=await fa_all(str(i.guild_id))
    if not pool:await i.response.send_message("None.");return
    await i.response.send_message("\U0001f91d **Free Agents**\n\n"+"\n".join(f"\u2022 <@{p['user_id']}>"for p in pool))
@fa.command(name="request",description="Request sub (captain, 2min)")
async def fa_request(i):
    d=await need_captain(i)
    if not d:return
    gid=str(i.guild_id);pool=await fa_all(gid)
    if not pool:await i.response.send_message("\u274c No FAs.",ephemeral=True);return
    c=await cfg_get(gid);fa_ch_id=c.get("fa_ch")if c else None;view=FAButtonView(d,str(i.user.id),gid,fa_ch_id)
    if fa_ch_id and(ch:=i.guild.get_channel(int(fa_ch_id))):
        pings=' '.join(f'<@{p["user_id"]}>'for p in pool)
        msg=await ch.send(f"\U0001f4e2 **{d}** needs a sub! (2 min)\n{pings}",view=view);view.msg=msg
        await i.response.send_message(f"\u2705 Posted in {ch.mention}.",ephemeral=True)
    else:
        pings=' '.join(f'<@{p["user_id"]}>'for p in pool)
        msg=await i.response.send_message(f"\U0001f4e2 **{d}** needs sub!\n{pings}",view=view);view.msg=(await i.original_response())
bot.tree.add_command(fa)

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
    try:
        dt=parse_schedule(datetime_str)
        sched=dt.strftime(SCHED_FMT+" GMT");unix=int(dt.timestamp())
    except Exception as ex:await i.response.send_message(f"\u274c {ex}. Try: 05 Aug 20:00 or 05 Aug 8pm",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("UPDATE matches SET scheduled=? WHERE thread_id=?",(sched,str(i.channel.id)));await db.commit()
    await i.response.send_message(f"\U0001f4c5 Scheduled: **{sched}**\n\U0001f550 Your time: <t:{unix}:f>")

@bot.tree.command(name="reschedule",description="Reschedule (other captain approves)")
@app_commands.describe(datetime_str=SCHED_HELP)
async def reschedule_cmd(i,datetime_str:str):
    if not isinstance(i.channel,discord.Thread):await i.response.send_message("\u274c Match threads only.",ephemeral=True);return
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
    v=RescheduleView(ot["captain_id"],nt,row["id"],gid)
    await i.response.send_message(f"\U0001f4c5 {i.user.mention} wants **{nt}** (<t:{unix}:f>).\n{oc.mention} approve?",view=v)

create_grp=app_commands.Group(name="create",description="Create scrims")
@create_grp.command(name="mixedscrim",description="Create a mixed scrim")
@app_commands.describe(time="Start time HH:MM GMT (e.g. 20:00)",format="Match format")
@app_commands.choices(format=[app_commands.Choice(name="3v3 (6 players)",value="3v3"),app_commands.Choice(name="2v2 (4 players)",value="2v2")])
async def create_mixedscrim(i,time:str,format:str):
    # Accept: 20:00, 20.00, 8pm, 8:30pm, 8.30pm, 20, 8pm
    import re
    t=time.strip().lower()
    m24=re.match(r'^(\d{1,2})[:\.](\d{2})$',t)
    m12=re.match(r'^(\d{1,2})[:\.]?(\d{2})?(am|pm)$',t)
    try:
        if m12: h=int(m12.group(1));mi=int(m12.group(2)or 0);ap=m12.group(3);h=0 if h==12 and ap=='am'else(12 if h==12 and ap=='pm'else(h+(12 if ap=='pm'else 0)))
        elif m24: h=int(m24.group(1));mi=int(m24.group(2))
        else: raise ValueError
        utc_h=(h-2)%24;scrim_time=f"{h:02d}:{mi:02d}"
        dt=datetime.now(timezone.utc).replace(hour=utc_h,minute=mi,second=0,tzinfo=timezone.utc);unix=int(dt.timestamp())
    except:await i.response.send_message("\u274c Try: 20:00, 20.00, 8pm, 8:30pm",ephemeral=True);return
    await i.response.defer(ephemeral=True)
    if not has_scrims(i.guild):await i.response.send_message("\u274c Run `/setup scrimbot` first.",ephemeral=True);return
    gid=str(i.guild_id);max_p=6 if format=="3v3" else 4;today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title=f"Mixed Scrim  -  {format.upper()}  -  Today {scrim_time}"
    msc=None
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            for ch in cat.text_channels:
                if ch.name=="mixed-scrims":msc=ch;break
    if not msc:await i.response.send_message("\u274c No #mixed-scrims channel.",ephemeral=True);return
    async with aiosqlite.connect(DB)as db:await db.execute("DELETE FROM scrim_signups WHERE guild_id=? AND date=?",(gid,today));await db.commit()
    embed=discord.Embed(title=title,description=f"*{max_p} spots | Click Sign Up!*",color=0x5865F2)
    embed.add_field(name="Signed Up",value=f"0/{max_p}",inline=True)
    embed.add_field(name="Your Time",value=f"<t:{unix}:f>",inline=True)
    embed.set_footer(text="Click Sign Up to join")
    view=ScrimSignupView(gid,today);msg=await msc.send(embed=embed,view=view)
    async with aiosqlite.connect(DB)as db:await db.execute("INSERT OR REPLACE INTO scrim_sessions VALUES(?,?,NULL,?,?,?,?)",(gid,today,str(msg.id),max_p,title,unix));await db.commit()
    await i.followup.send(f"\u2705 Mixed {format} scrim created! ({scrim_time})",ephemeral=True)

@bot.tree.command(name="teamscrim",description="Ping @TeamScrims for scrim (Captain only, in #team-scrims)")
async def teamscrim_ping(i):
    d=await need_captain(i)
    if not d:return
    if not has_scrims(i.guild):await i.response.send_message("\u274c Run `/setup scrimbot` first.",ephemeral=True);return
    role=find_role(i.guild,"TeamScrims")
    if not role:await i.response.send_message("\u274c @TeamScrims role not found.",ephemeral=True);return
    tsc=None
    for cat in i.guild.categories:
        if cat.name=="Scrims":
            for ch in cat.text_channels:
                if ch.name=="team-scrims":tsc=ch;break
    if not tsc:await i.response.send_message("\u274c #team-scrims not found.",ephemeral=True);return
    if i.channel.id!=tsc.id:await i.response.send_message(f"\u274c Use in {tsc.mention}.",ephemeral=True);return
    await i.response.send_message(f"{role.mention} {i.user.mention} from **{d}** is looking for a team scrim!")

bot.tree.add_command(create_grp)

# ===== /leaderboard =====
leaderboard_msg_ids={}

async def refresh_leaderboard(guild):
    gid=str(guild.id)
    if gid not in leaderboard_msg_ids:return
    msg_data=leaderboard_msg_ids[gid]
    ch=guild.get_channel(int(msg_data["ch"]))
    if not ch:return
    ts=sorted(await teams_all(gid),key=lambda x:x["mmr"],reverse=True)
    if not ts:return
    lines=[]
    for n,t in enumerate(ts[:25]):
        lines.append(f"`{n+1:>2}.` **{t['display']}** - {t['wins']}W/{t['losses']}L - {get_rank(t['mmr'])} `{t['mmr']} MMR`")
    if len(ts)>25:lines.append(f"\n*...and {len(ts)-25} more teams*")
    c=await cfg_get(gid)
    season_name=c["name"]if c else "EGL"
    embed=discord.Embed(title=f"\U0001f4ca {season_name} Leaderboard",description="\n".join(lines),color=0x5865F2)
    embed.set_footer(text=f"{len(ts)} teams - Auto-refreshed")
    try:
        msg=await ch.fetch_message(int(msg_data["msg"]))
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
    lines=[]
    for n,t in enumerate(ts[:25]):
        lines.append(f"`{n+1:>2}.` **{t['display']}** - {t['wins']}W/{t['losses']}L - {get_rank(t['mmr'])} `{t['mmr']} MMR`")
    if len(ts)>25:lines.append(f"\n*...and {len(ts)-25} more teams*")
    c=await cfg_get(gid)
    season_name=c["name"]if c else"EGL"
    embed=discord.Embed(title=f"\U0001f4ca {season_name} Leaderboard",description="\n".join(lines),color=0x5865F2)
    embed.set_footer(text=f"{len(ts)} teams - Auto-refreshed")
    # Delete old message if exists
    if gid in leaderboard_msg_ids:
        try:
            old_ch=i.guild.get_channel(int(leaderboard_msg_ids[gid]["ch"]))
            if old_ch:
                try:
                    old_msg=await old_ch.fetch_message(int(leaderboard_msg_ids[gid]["msg"]))
                    await old_msg.delete()
                except:pass
        except:pass
    msg=await lb_ch.send(embed=embed)
    leaderboard_msg_ids[gid]={"ch":str(lb_ch.id),"msg":str(msg.id)}
    await i.response.send_message("\u2705 Leaderboard posted!",ephemeral=True)

@tasks.loop(minutes=1)
async def leaderboard_refresh():
    for g in bot.guilds:
        await refresh_leaderboard(g)
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
    if now.weekday()!=6 or now.hour!=18:return
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
            if 280<=diff<=310:
                members=await scrim_signups_active(gid,today)
                if members:
                    pings=" ".join(f"<@{uid}>"for uid in members)
                    await msc.send(f"\U0001f514 **{sess.get('scrim_title','Mixed Scrim')}** starts in 5 minutes! {pings}")
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
        ("Free Agent",discord.Color.blue()),
        ("Player",discord.Color.green()),
        ("TeamScrims",discord.Color.orange()),
        (TESTER_ROLE,discord.Color.from_rgb(255,105,180)),
    ]
    for role_name,color in required:
        if not find_role(guild,role_name):
            try:
                r=await guild.create_role(name=role_name,color=color)
            except:pass
    log.info("\u2705 Joined %s, commands synced, roles created",guild.name)

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

bot.tree.add_command(setup)

if __name__=="__main__":
    if not TOKEN:print("\n\u274c Set DISCORD_BOT_TOKEN!\n")
    else:bot.run(TOKEN)

