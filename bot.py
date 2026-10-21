import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
from datetime import datetime, timedelta
import os

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

conn = sqlite3.connect("duty_system.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS duty (
    user_id INTEGER PRIMARY KEY,
    is_on_duty BOOLEAN DEFAULT 0,
    duty_start_time TEXT,
    total_seconds INTEGER DEFAULT 0,
    last_duty_date TEXT
)
""")
conn.commit()

DUTY_ROLE_ID = int(os.getenv("DUTY_ROLE_ID", "0"))
INACTIVITY_CHANNEL_ID = int(os.getenv("INACTIVITY_CHANNEL_ID", "0"))

class ActivityCheckView(discord.ui.View):
    def __init__(self, member_id: int):
        super().__init__(timeout=1800)
        self.member_id = member_id
        self.clicked = False

    @discord.ui.button(label="Aktív vagyok", style=discord.ButtonStyle.secondary, custom_id="im_active")
    async def active_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("Ez nem a te ellenőrző gombod!", ephemeral=True)
            return
        self.clicked = True
        button.disabled = True
        await interaction.response.edit_message(content="✅ Köszönjük a visszajelzést! Még mindig szolgálatban vagy.", view=self)
        self.stop()

    async def on_timeout(self):
        if not self.clicked:
            cursor.execute("SELECT duty_start_time, total_seconds FROM duty WHERE user_id = ?", (self.member_id,))
            row = cursor.fetchone()
            if row:
                start_time = datetime.fromisoformat(row[0])
                elapsed = int((datetime.now() - start_time).total_seconds())
                new_total = row[1] + elapsed
                cursor.execute("UPDATE duty SET is_on_duty = 0, total_seconds = ? WHERE user_id = ?", (new_total, self.member_id))
                conn.commit()

class MainDutyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Szolgálatba állás", style=discord.ButtonStyle.green, custom_id="duty_start")
    async def start_duty(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        now_str = datetime.now().isoformat()
        cursor.execute("SELECT is_on_duty FROM duty WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            await interaction.response.send_message("⚠️ Már szolgálatban vagy!", ephemeral=True)
            return

        cursor.execute("""
            INSERT INTO duty (user_id, is_on_duty, duty_start_time, last_duty_date) 
            VALUES (?, 1, ?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET is_on_duty=1, duty_start_time=?, last_duty_date=?
        """, (user_id, now_str, now_str, now_str, now_str))
        conn.commit()

        if DUTY_ROLE_ID != 0:
            role = interaction.guild.get_role(DUTY_ROLE_ID)
            if role:
                await interaction.user.add_roles(role)

        await interaction.response.send_message("🟢 Sikeresen beléptél szolgálatba!", ephemeral=True)
        bot.loop.create_task(send_activity_check(interaction.user, interaction.channel))

    @discord.ui.button(label="Szolgálat leadás", style=discord.ButtonStyle.red, custom_id="duty_stop")
    async def stop_duty(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        cursor.execute("SELECT is_on_duty, duty_start_time, total_seconds FROM duty WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            await interaction.response.send_message("⚠️ Nem vagy szolgálatban!", ephemeral=True)
            return

        start_time = datetime.fromisoformat(row[1])
        elapsed = int((datetime.now() - start_time).total_seconds())
        new_total = row[2] + elapsed

        cursor.execute("UPDATE duty SET is_on_duty = 0, total_seconds = ? WHERE user_id = ?", (new_total, user_id))
        conn.commit()

        if DUTY_ROLE_ID != 0:
            role = interaction.guild.get_role(DUTY_ROLE_ID)
            if role:
                await interaction.user.remove_roles(role)

        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        await interaction.response.send_message(f"🔴 Leadtad a szolgálatot! A mostani munkameneted: **{hours} óra {minutes} perc**.", ephemeral=True)

    @discord.ui.button(label="Szolgálati időm", style=discord.ButtonStyle.grey, custom_id="duty_time")
    async def check_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        cursor.execute("SELECT total_seconds, is_on_duty, duty_start_time FROM duty WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message("Még nincs rögzített szolgálati időd.", ephemeral=True)
            return

        total = row[0]
        if row[1]:
            start_time = datetime.fromisoformat(row[2])
            total += int((datetime.now() - start_time).total_seconds())

        hours = total // 3600
        minutes = (total % 3600) // 60
        await interaction.response.send_message(f"⏱️ Az eddig felhalmozott szolgálati időd: **{hours} óra {minutes} perc**.", ephemeral=True)

async def send_activity_check(user: discord.Member, channel):
    await discord.utils.sleep_until(datetime.now() + timedelta(hours=1))
    cursor.execute("SELECT is_on_duty FROM duty WHERE user_id = ?", (user.id,))
    row = cursor.fetchone()
    if row and row[0]:
        view = ActivityCheckView(user.id)
        await channel.send(content=f"{user.mention} ⚠️ **Aktivitási ellenőrzés!** Még mindig szolgálatban vagy?", view=view)

class LeaderPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ki van szolgálatban?", style=discord.ButtonStyle.primary, custom_id="who_on_duty")
    async def who_on_duty(self, interaction: discord.Interaction, button: discord.ui.Button):
        cursor.execute("SELECT user_id FROM duty WHERE is_on_duty = 1")
        rows = cursor.fetchall()
        if not rows:
            await interaction.response.send_message("Jelenleg senki sincs szolgálatban.", ephemeral=True)
            return
        text = "🟢 **Jelenleg szolgálatban lévő tagok:**
"
        for r in rows:
            m = interaction.guild.get_member(r[0])
            text += f"• {m.mention if m else r[0]}
"
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Heti Ranglista", style=discord.ButtonStyle.success, custom_id="leader_top")
    async def show_top(self, interaction: discord.Interaction, button: discord.ui.Button):
        cursor.execute("SELECT user_id, total_seconds FROM duty ORDER BY total_seconds DESC LIMIT 10")
        rows = cursor.fetchall()
        embed = discord.Embed(title="🏆 Heti Szolgálati Ranglista", color=discord.Color.gold())
        for index, r in enumerate(rows, 1):
            m = interaction.guild.get_member(r[0])
            name = m.display_name if m else f"ID: {r[0]}"
            hours = r[1] // 3600
            minutes = (r[1] % 3600) // 60
            embed.add_field(name=f"{index}. {name}", value=f"{hours} óra {minutes} perc", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.hybrid_command(name="dutypanel", description="Duty panel kihelyezése")
@commands.has_permissions(administrator=True)
async def dutypanel(ctx):
    embed = discord.Embed(
        title="🛡️ Frakció Szolgálati Rendszer",
        description="Használd az alábbi gombokat a szolgálat kezeléséhez!

🟢 **Szolgálatba állás** — Elindítja a szolgálati idődet.
🔴 **Szolgálat leadás** — Elmenti az eltöltött idődet.
⏱️ **Szolgálati időm** — Megmutatja az összes idődet.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=MainDutyView())

@bot.hybrid_command(name="leaderpanel", description="Leader panel kihelyezése")
@commands.has_permissions(administrator=True)
async def leaderpanel(ctx):
    embed = discord.Embed(
        title="👑 Leader Vezérlő Panel",
        description="Itt ellenőrizheted a szolgálatban lévőket és a heti statisztikákat.

Időmódosításhoz használd a `/addtime`, `/remtime` és `/check` parancsokat!",
        color=discord.Color.purple()
    )
    await ctx.send(embed=embed, view=LeaderPanelView())

@bot.hybrid_command(name="addtime", description="Idő hozzáadása egy tag szolgálati idejéhez")
@commands.has_permissions(administrator=True)
async def addtime(ctx, member: discord.Member, hours: int, minutes: int):
    added_seconds = (hours * 3600) + (minutes * 60)
    cursor.execute("UPDATE duty SET total_seconds = total_seconds + ? WHERE user_id = ?", (added_seconds, member.id))
    conn.commit()
    await ctx.send(f"✅ Sikeresen hozzáadtál **{hours} óra {minutes} percet** {member.mention} idejéhez.")

@bot.hybrid_command(name="remtime", description="Idő levonása egy tag szolgálati idejéből")
@commands.has_permissions(administrator=True)
async def remtime(ctx, member: discord.Member, hours: int, minutes: int):
    rem_seconds = (hours * 3600) + (minutes * 60)
    cursor.execute("UPDATE duty SET total_seconds = MAX(0, total_seconds - ?) WHERE user_id = ?", (rem_seconds, member.id))
    conn.commit()
    await ctx.send(f"✅ Sikeresen levontál **{hours} óra {minutes} percet** {member.mention} idejéből.")

@bot.hybrid_command(name="check", description="Egy tag idejének ellenőrzése")
@commands.has_permissions(administrator=True)
async def check(ctx, member: discord.Member):
    cursor.execute("SELECT total_seconds, is_on_duty FROM duty WHERE user_id = ?", (member.id,))
    row = cursor.fetchone()
    if not row:
        await ctx.send(f"{member.mention} még nem rendelkezik szolgálati idővel.")
        return
    hours = row[0] // 3600
    minutes = (row[0] % 3600) // 60
    status = "🟢 Szolgálatban" if row[1] else "🔴 Nincs szolgálatban"
    await ctx.send(f"📊 **{member.display_name}** adatai:
Status: {status}
Összes idő: **{hours} óra {minutes} perc**")

@bot.hybrid_command(name="resetall", description="Minden tag heti idejének nullázása")
@commands.has_permissions(administrator=True)
async def resetall(ctx):
    cursor.execute("UPDATE duty SET total_seconds = 0")
    conn.commit()
    await ctx.send("🧹 **Minden tag szolgálati ideje sikeresen nullázva lett!**")

@tasks.loop(hours=24)
async def check_inactivity():
    if INACTIVITY_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(INACTIVITY_CHANNEL_ID)
    if not channel:
        return
    cursor.execute("SELECT user_id, last_duty_date FROM duty WHERE is_on_duty = 0")
    rows = cursor.fetchall()
    now = datetime.now()
    for user_id, last_date_str in rows:
        if last_date_str:
            last_date = datetime.fromisoformat(last_date_str)
            days_inactive = (now - last_date).days
            if days_inactive >= 2:
                member = channel.guild.get_member(user_id)
                if member:
                    await channel.send(f"⚠️ {member.mention} már **{days_inactive} napja** nem lépett szolgálatba!")

@bot.event
async def on_ready():
    print(f"Bot sikeresen elindult: {bot.user.name}")
    bot.add_view(MainDutyView())
    bot.add_view(LeaderPanelView())
    if not check_inactivity.is_running():
        check_inactivity.start()
    try:
        synced = await bot.tree.sync()
        print(f"Sikeresen szinkronizálva {len(synced)} slash parancs.")
    except Exception as e:
        print(f"Hiba a szinkronizálás során: {e}")

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("HIBA: Nincs megadva DISCORD_TOKEN a környezeti változókban!")
