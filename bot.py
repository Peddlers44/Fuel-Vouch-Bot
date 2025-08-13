# bot.py — Fuel Cart Vouch Bot (SQLite version)
# Files needed in the folder: bot.py, logo.png
# points.db will be created automatically on first run.

import os
import io
import json
import sqlite3
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image

# === BASIC CONFIG (edit these) ===
SERVER_NAME = "Fuel Cart"

# Discord IDs (update these to your server)
GUILD_ID = 1399270717807394937
TARGET_CHANNEL_ID = 1399270718247796744   # where users post vouches (images)
REVIEW_CHANNEL_ID = 1405065253129027584   # where staff review/verify/reject
ADMIN_USER_ID = 1403410639694598176
ALLOWED_ROLE_ID = 1399270717832429581
OWNER_ID = 1403411205330046987

# Files (keep these file names so your folder matches the screenshot)
LOGO_PATH = "logo.png"
POINTS_DB_PATH = "points.db"

# Bot token: set env var BOT_TOKEN or paste a literal string here (not recommended)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# === BOT SETUP ===
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# Ensure temp dir exists
os.makedirs("temp", exist_ok=True)

# === POINTS STORAGE: SQLite (points.db) ===
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(POINTS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS points(
            user_id INTEGER PRIMARY KEY,
            points  INTEGER NOT NULL DEFAULT 0
        )
    """)
    return conn

def get_points(user_id: int) -> int:
    conn = db()
    cur = conn.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def add_points(user_id: int, amount: int) -> int:
    conn = db()
    with conn:
        conn.execute("""
            INSERT INTO points(user_id, points)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET points = points + excluded.points
        """, (user_id, amount))
        cur = conn.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
        total = cur.fetchone()[0]
    conn.close()
    return total

def remove_points(user_id: int, amount: int) -> int:
    conn = db()
    with conn:
        cur = conn.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        current = row[0] if row else 0
        new_total = max(0, current - amount)
        conn.execute("""
            INSERT INTO points(user_id, points) VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET points = ?
        """, (user_id, new_total, new_total))
    conn.close()
    return new_total

def reset_points(user_id: int) -> None:
    conn = db()
    with conn:
        conn.execute("""
            INSERT INTO points(user_id, points) VALUES(?, 0)
            ON CONFLICT(user_id) DO UPDATE SET points = 0
        """, (user_id,))
    conn.close()

# === IMAGE PROCESSING ===
def overlay_logo(user_image_path: str, logo_path: str, output_path: str) -> bool:
    try:
        base = Image.open(user_image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")

        # Resize logo to ~25% of base width
        logo_width = max(1, base.width // 4)
        logo_height = int(logo.height * (logo_width / logo.width))
        logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)

        pos = ((base.width - logo.width) // 2, (base.height - logo.height) // 2)
        combined = base.copy()
        combined.paste(logo, pos, logo)
        combined.convert("RGB").save(output_path, "JPEG", quality=95)
        return True
    except Exception as e:
        print(f"[overlay_logo] Error: {e}")
        return False

# === REVIEW VIEW (Verify / Reject) ===
class VouchView(discord.ui.View):
    def __init__(self, member_id: int, vouch_text: str, image_path: str):
        super().__init__(timeout=None)
        self.member_id = member_id
        self.vouch_text = vouch_text
        self.image_path = image_path
        self._locked = False

    def _cleanup_local_file(self):
        try:
            if os.path.exists(self.image_path):
                os.remove(self.image_path)
        except Exception:
            pass

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.success)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._locked:
            await interaction.response.send_message("Already processing…", ephemeral=True)
            return
        self._locked = True
        await interaction.response.defer(ephemeral=True)

        try:
            total = add_points(self.member_id, 1)
            member = await interaction.guild.fetch_member(self.member_id)
            member_display = member.display_name

            public_embed = discord.Embed(
                title=f"{SERVER_NAME} Vouch by {member_display}",
                color=0x5c19ae
            )
            if self.vouch_text:
                public_embed.description = f"**{self.vouch_text}**"
            public_embed.set_image(url="attachment://processed.jpg")

            public_channel = bot.get_channel(TARGET_CHANNEL_ID)
            if public_channel and os.path.exists(self.image_path):
                await public_channel.send(
                    content=f"✅ Verified {SERVER_NAME} vouch for <@{self.member_id}>! They now have {total} points.",
                    embed=public_embed,
                    file=discord.File(self.image_path, filename="processed.jpg")
                )

            await interaction.message.edit(
                content=f"✅ **Verified by {interaction.user.mention}** for <@{self.member_id}>.",
                embed=None,
                view=None
            )
            await interaction.followup.send(f"{SERVER_NAME} vouch verified and posted.", ephemeral=True)

        except discord.NotFound:
            await interaction.followup.send("User not found.", ephemeral=True)
        except Exception as e:
            print(f"[verify_button] {e}")
            await interaction.followup.send(f"Unexpected error: {e}", ephemeral=True)
        finally:
            self._cleanup_local_file()

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._locked:
            await interaction.response.send_message("Already processing…", ephemeral=True)
            return
        self._locked = True
        await interaction.response.defer(ephemeral=True)

        try:
            target_channel = bot.get_channel(TARGET_CHANNEL_ID)
            if target_channel:
                await target_channel.send(f"❌ A {SERVER_NAME} vouch submission for <@{self.member_id}> was rejected.")

            await interaction.message.edit(
                content=f"❌ **Rejected by {interaction.user.mention}** for <@{self.member_id}>.",
                embed=None,
                view=None
            )
            await interaction.followup.send(f"{SERVER_NAME} vouch rejected.", ephemeral=True)

        except Exception as e:
            print(f"[reject_button] {e}")
            await interaction.followup.send(f"Unexpected error: {e}", ephemeral=True)
        finally:
            self._cleanup_local_file()

# === EVENTS ===
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online and ready!")
    # Ensure DB exists
    db().close()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Vouch posts only in TARGET_CHANNEL_ID: must include an image
    is_vouch_submission = (
        message.channel.id == TARGET_CHANNEL_ID and
        message.attachments and
        message.attachments[0].content_type and
        message.attachments[0].content_type.startswith("image/")
    )

    if is_vouch_submission:
        attachment = message.attachments[0]
        user_img = f"temp/user_{message.id}.png"
        out_img = f"temp/processed_{message.id}.jpg"
        review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
        if not review_channel:
            print(f"[CRITICAL] REVIEW_CHANNEL_ID {REVIEW_CHANNEL_ID} not found.")
            return

        try:
            await attachment.save(user_img)
            if not os.path.exists(LOGO_PATH):
                await message.channel.send("Logo missing. Please add logo.png.", delete_after=10)
                return

            if not overlay_logo(user_img, LOGO_PATH, out_img):
                await message.channel.send(f"{message.author.mention}, error processing your image.", delete_after=10)
                return

            vouch_text = message.content.strip()
            embed = discord.Embed(title=f"New {SERVER_NAME} Vouch Submission", color=0x5c19ae)
            if vouch_text:
                embed.description = f"**{vouch_text}**"

            avatar_url = message.author.avatar.url if message.author.avatar else message.author.default_avatar.url
            embed.set_author(name=message.author.display_name, icon_url=avatar_url)
            embed.set_image(url="attachment://processed.jpg")

            view = VouchView(member_id=message.author.id, vouch_text=vouch_text, image_path=out_img)

            await review_channel.send(embed=embed, file=discord.File(out_img, filename="processed.jpg"), view=view)
            await message.delete()

        except Exception as e:
            print(f"[on_message] {e}")
        finally:
            try:
                if os.path.exists(user_img):
                    os.remove(user_img)
            except Exception:
                pass

        return  # stop here so it doesn't get treated as a command

    await bot.process_commands(message)

# === COMMANDS ===
@bot.command(name="addpoints")
@commands.has_permissions(administrator=True)
async def cmd_addpoints(ctx, member: discord.Member, points: int):
    total = add_points(member.id, points)
    await ctx.send(f"Added {points} points to {member.mention}. They now have {total} points.")

@bot.command(name="removepoints")
@commands.has_permissions(administrator=True)
async def cmd_removepoints(ctx, member: discord.Member, points: int):
    total = remove_points(member.id, points)
    await ctx.send(f"Removed {points} points from {member.mention}. They now have {total} points.")

@bot.command(name="points")
async def cmd_points(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    total = get_points(member.id)
    await ctx.send(f"{member.mention} has {total} point{'s' if total != 1 else ''}.")

@bot.command(name="resetpoints")
@commands.has_permissions(administrator=True)
async def cmd_resetpoints(ctx, member: discord.Member):
    reset_points(member.id)
    await ctx.send(f"{member.mention}'s points have been reset.")

@bot.command(name="redeem")
@commands.has_permissions(administrator=True)
async def cmd_redeem(ctx, user: discord.Member):
    total = get_points(user.id)
    if total > 0:
        reset_points(user.id)
        await ctx.send(f"{user.mention}'s points have been reset for their reward.")
    else:
        await ctx.send(f"{user.mention} has no points to redeem.")

# === ERROR HANDLER ===
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument. Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to run this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(f"Member not found.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"[on_command_error] in '{getattr(ctx, 'command', None)}': {error}")

# === RUN ===
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
    raise RuntimeError("Set your Discord bot token in the BOT_TOKEN env var or replace the placeholder in the code.")
bot.run(BOT_TOKEN)
