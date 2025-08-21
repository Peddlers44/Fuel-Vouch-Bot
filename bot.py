# bot.py — Fuel Cart Vouch Bot (PostgreSQL with auto-migration)

import os
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image
import psycopg2

# ========== CONFIG ==========
SERVER_NAME = "Fuel Cart"

# Discord IDs (for THIS server)
GUILD_ID = 1399270717807394937
TARGET_CHANNEL_ID = 1399270718247796744   # vouches channel (users post images)
REVIEW_CHANNEL_ID = 1405065253129027584   # staff review channel
ADMIN_USER_ID = 1403410639694598176
ALLOWED_ROLE_ID = 1399270717832429581
OWNER_ID = 1403411205330046987

LOGO_PATH = "logo.png"

# ========== ENV VARS ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PER_GUILD = (os.getenv("PER_GUILD", "true").lower() in ("1", "true", "yes"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL env var")

# ========== DB SETUP (auto-migrate) ==========
pg_conn = psycopg2.connect(DATABASE_URL, sslmode="require")
pg_conn.autocommit = True

def init_db():
    """
    Ensure the 'points' table exists. If an old global schema exists, migrate it
    to (guild_id, user_id) PK when PER_GUILD=True. Safe to run repeatedly.
    """
    with pg_conn.cursor() as cur:
        # Create table if missing (start with per-guild or global shape)
        if PER_GUILD:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS points (
                guild_id   BIGINT NOT NULL,
                user_id    BIGINT NOT NULL,
                points     INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            );
            """)
            # If an old global table exists, it may lack columns/PK
            # 1) ensure columns exist
            cur.execute("ALTER TABLE points ADD COLUMN IF NOT EXISTS guild_id BIGINT;")
            cur.execute("ALTER TABLE points ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            # 2) if guild_id is NULL (from old rows), backfill with this bot's guild
            cur.execute("UPDATE points SET guild_id=%s WHERE guild_id IS NULL;", (GUILD_ID,))
            # 3) ensure PK is (guild_id, user_id)
            # drop any existing PK and re-add
            cur.execute("""
            DO $$
            DECLARE pkname text;
            BEGIN
              SELECT conname INTO pkname
              FROM   pg_constraint c
              JOIN   pg_class t ON t.oid = c.conrelid
              WHERE  t.relname = 'points' AND c.contype = 'p'
              LIMIT 1;
              IF pkname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE points DROP CONSTRAINT %I', pkname);
              END IF;
            END$$;
            """)
            cur.execute("ALTER TABLE points ADD PRIMARY KEY (guild_id, user_id);")
        else:
            # Global points
            cur.execute("""
            CREATE TABLE IF NOT EXISTS points (
                user_id    BIGINT PRIMARY KEY,
                points     INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            # If table was per-guild, collapse to global by summing points
            # (only if there's a guild_id column)
            cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='points' AND column_name='guild_id';
            """)
            if cur.fetchone():
                # Build a temp table with summed totals
                cur.execute("""
                CREATE TEMP TABLE _tmp_points AS
                  SELECT user_id, SUM(points)::int AS points
                  FROM points
                  GROUP BY user_id;
                """)
                # Replace real table with global shape
                cur.execute("DROP TABLE IF EXISTS points;")
                cur.execute("""
                CREATE TABLE points (
                    user_id    BIGINT PRIMARY KEY,
                    points     INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """)
                cur.execute("""
                INSERT INTO points (user_id, points) SELECT user_id, points FROM _tmp_points;
                """)

def get_points(user_id: int, guild_id: Optional[int] = None) -> int:
    with pg_conn.cursor() as cur:
        if PER_GUILD:
            if guild_id is None: raise ValueError("guild_id required (PER_GUILD=True)")
            cur.execute("SELECT points FROM points WHERE guild_id=%s AND user_id=%s;", (guild_id, user_id))
        else:
            cur.execute("SELECT points FROM points WHERE user_id=%s;", (user_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

def add_points(user_id: int, amount: int, guild_id: Optional[int] = None) -> int:
    with pg_conn.cursor() as cur:
        if PER_GUILD:
            if guild_id is None: raise ValueError("guild_id required (PER_GUILD=True)")
            cur.execute("""
                INSERT INTO points (guild_id, user_id, points)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET points = points.points + EXCLUDED.points,
                              updated_at = NOW()
                RETURNING points;
            """, (guild_id, user_id, amount))
        else:
            cur.execute("""
                INSERT INTO points (user_id, points)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET points = points.points + EXCLUDED.points,
                              updated_at = NOW()
                RETURNING points;
            """, (user_id, amount))
        return int(cur.fetchone()[0])

def remove_points(user_id: int, amount: int, guild_id: Optional[int] = None) -> int:
    current = get_points(user_id, guild_id if PER_GUILD else None)
    new_total = max(0, current - amount)
    with pg_conn.cursor() as cur:
        if PER_GUILD:
            if guild_id is None: raise ValueError("guild_id required (PER_GUILD=True)")
            cur.execute("""
                INSERT INTO points (guild_id, user_id, points)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET points = EXCLUDED.points,
                              updated_at = NOW()
                RETURNING points;
            """, (guild_id, user_id, new_total))
        else:
            cur.execute("""
                INSERT INTO points (user_id, points)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET points = EXCLUDED.points,
                              updated_at = NOW()
                RETURNING points;
            """, (user_id, new_total))
        return int(cur.fetchone()[0])

def reset_points(user_id: int, guild_id: Optional[int] = None) -> None:
    with pg_conn.cursor() as cur:
        if PER_GUILD:
            if guild_id is None: raise ValueError("guild_id required (PER_GUILD=True)")
            cur.execute("""
                INSERT INTO points (guild_id, user_id, points)
                VALUES (%s, %s, 0)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET points = 0,
                              updated_at = NOW();
            """, (guild_id, user_id))
        else:
            cur.execute("""
                INSERT INTO points (user_id, points)
                VALUES (%s, 0)
                ON CONFLICT (user_id)
                DO UPDATE SET points = 0,
                              updated_at = NOW();
            """, (user_id,))

# ========== BOT ==========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("!"),
    intents=intents,
    case_insensitive=True,
)
bot.remove_command("help")

os.makedirs("temp", exist_ok=True)

# ========== IMAGE ==========
def overlay_logo(user_image_path: str, logo_path: str, output_path: str) -> bool:
    try:
        base = Image.open(user_image_path).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
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

# ========== REVIEW UI ==========
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
            total = add_points(self.member_id, 1, guild_id=interaction.guild.id if PER_GUILD else None)
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
                content=f"❌ **Rejected by {interaction.user.mention)}** for <@{self.member_id}>.",
                embed=None,
                view=None
            )
            await interaction.followup.send(f"{SERVER_NAME} vouch rejected.", ephemeral=True)

        except Exception as e:
            print(f"[reject_button] {e}")
            await interaction.followup.send(f"Unexpected error: {e}", ephemeral=True)
        finally:
            self._cleanup_local_file()

# ========== EVENTS ==========
@bot.event
async def on_ready():
    init_db()
    print(f"✅ {bot.user} ready | PER_GUILD={PER_GUILD} | message_content={bot.intents.message_content}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

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
        return

    await bot.process_commands(message)

# ========== COMMANDS ==========
@bot.command(name="addpoints")
@commands.has_permissions(administrator=True)
async def cmd_addpoints(ctx, member: discord.Member, points: int):
    total = add_points(member.id, points, guild_id=ctx.guild.id if PER_GUILD else None)
    await ctx.send(f"Added {points} points to {member.mention}. They now have {total} points.")

@bot.command(name="removepoints")
@commands.has_permissions(administrator=True)
async def cmd_removepoints(ctx, member: discord.Member, points: int):
    total = remove_points(member.id, points, guild_id=ctx.guild.id if PER_GUILD else None)
    await ctx.send(f"Removed {points} points from {member.mention}. They now have {total} points.")

@bot.command(name="points")
async def cmd_points(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    total = get_points(member.id, guild_id=ctx.guild.id if PER_GUILD else None)
    await ctx.send(f"{member.mention} has {total} point{'s' if total != 1 else ''}.")

@bot.command(name="resetpoints")
@commands.has_permissions(administrator=True)
async def cmd_resetpoints(ctx, member: discord.Member):
    reset_points(member.id, guild_id=ctx.guild.id if PER_GUILD else None)
    await ctx.send(f"{member.mention}'s points have been reset.")

@bot.command(name="redeem")
@commands.has_permissions(administrator=True)
async def cmd_redeem(ctx, user: discord.Member):
    total = get_points(user.id, guild_id=ctx.guild.id if PER_GUILD else None)
    if total > 0:
        reset_points(user.id, guild_id=ctx.guild.id if PER_GUILD else None)
        await ctx.send(f"{user.mention}'s points have been reset for their reward.")
    else:
        await ctx.send(f"{user.mention} has no points to redeem.")

# Better error feedback
@bot.event
async def on_command_error(ctx, error):
    from discord.ext.commands import MissingRequiredArgument, MissingPermissions, MemberNotFound, CommandNotFound
    if isinstance(error, MissingRequiredArgument):
        return await ctx.send(f"Missing argument. Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
    if isinstance(error, MissingPermissions):
        return await ctx.send("You need **Administrator** to run that command here.")
    if isinstance(error, MemberNotFound):
        return await ctx.send("Member not found.")
    if isinstance(error, CommandNotFound):
        return  # stay quiet on typos
    print(f"[on_command_error] {type(error).__name__}: {error}")

# ========== RUN ==========
bot.run(BOT_TOKEN)
