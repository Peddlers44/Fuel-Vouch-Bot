import os
import logging
import sqlite3
from io import BytesIO

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from dotenv import load_dotenv

# ------------------ CONFIG ------------------
VOUCH_CHANNEL_ID = 1399270718247796744
POINTS_PER_IMAGE = 1
LOGO_PATH = "logo.png"      # falls back to text if not found
DB_PATH = "points.db"
COMMAND_PREFIX = "!"
# --------------------------------------------

# ---- logging ----
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fuelcart")

# ---- env ----
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN missing in .env")

# ---- db ----
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS points(
            user_id INTEGER PRIMARY KEY,
            points  INTEGER NOT NULL DEFAULT 0
        )
    """)
    return conn

def add_points(user_id: int, amount: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO points(user_id, points) VALUES(?, 0)", (user_id,))
    cur.execute("UPDATE points SET points = points + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    cur.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
    total = cur.fetchone()[0]
    conn.close()
    return total

def get_points(user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT points FROM points WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

# ---- helpers ----
def is_image(attachment: discord.Attachment) -> bool:
    # Prefer reliable MIME check over extension
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    # fallback to extension
    name = attachment.filename.lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp"))

def overlay_logo(img_bytes: bytes) -> BytesIO | None:
    """
    Center a large semi-transparent watermark.
    Uses logo.png if present, otherwise draws FuelCart text.
    """
    try:
        base = Image.open(BytesIO(img_bytes)).convert("RGBA")
    except UnidentifiedImageError:
        log.warning("Attachment wasn't a decodable image.")
        return None
    except Exception as e:
        log.exception("Opening image failed: %s", e)
        return None

    bw, bh = base.size

    try:
        if os.path.exists(LOGO_PATH):
            logo = Image.open(LOGO_PATH).convert("RGBA")
            # ~35% of the width
            target_w = int(bw * 0.35)
            ratio = target_w / logo.width
            logo = logo.resize((target_w, int(logo.height * ratio)), Image.LANCZOS)

            # semi-transparent
            alpha = logo.split()[3].point(lambda p: int(p * 0.35))
            logo.putalpha(alpha)

            x = (bw - logo.width) // 2
            y = (bh - logo.height) // 2
            base.paste(logo, (x, y), logo)
        else:
            # Text fallback
            txt_layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt_layer)
            try:
                font = ImageFont.truetype("arial.ttf", int(bh * 0.09))
            except Exception:
                font = ImageFont.load_default()
            text = "FuelCart"
            tw, th = draw.textsize(text, font=font)
            x = (bw - tw) // 2
            y = (bh - th) // 2
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 150))
            base = Image.alpha_composite(base, txt_layer)

        out = BytesIO()
        base.convert("RGB").save(out, format="PNG")
        out.seek(0)
        return out

    except Exception as e:
        log.exception("Watermarking crashed: %s", e)
        return None

# ---- bot ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id == VOUCH_CHANNEL_ID and message.attachments:
        log.info("Got message %s with %d attachments from %s",
                 message.id, len(message.attachments), message.author)

        processed = False

        for att in message.attachments:
            if not is_image(att):
                log.info("Skipped non-image attachment: %s (%s)", att.filename, att.content_type)
                continue

            try:
                img_bytes = await att.read()
                watermarked = overlay_logo(img_bytes)

                if watermarked:
                    await message.channel.send(file=discord.File(watermarked, filename="fuelcart_watermarked.png"))
                    await message.delete()
                    total = add_points(message.author.id, POINTS_PER_IMAGE)
                    await message.channel.send(
                    f"🎉 {message.author.mention} earned **{POINTS_PER_IMAGE}** point! Total: **{total}**"
)
                    processed = True
                else:
                    log.warning("Watermark returned None for %s", att.filename)

            except Exception as e:
                log.exception("Failed to process attachment %s: %s", att.filename, e)

        if not processed:
            await message.channel.send("❌ Couldn't watermark any attachment in that message.")

    await bot.process_commands(message)

# ---- commands ----
@bot.command(name="points")
async def points_cmd(ctx: commands.Context, member: discord.Member | None = None):
    member = member or ctx.author
    total = get_points(member.id)
    await ctx.send(f"🏆 **{member.display_name}** has **{total}** point(s).")

@bot.command(name="addpoints")
@commands.has_permissions(manage_guild=True)
async def addpoints_cmd(ctx: commands.Context, member: discord.Member, amount: int):
    total = add_points(member.id, amount)
    await ctx.send(f"✅ Added **{amount}** to {member.mention}. Total: **{total}**")
@bot.command(name="resetpoints")
@commands.has_permissions(administrator=True)
async def resetpoints_cmd(ctx: commands.Context):
    conn = db()
    conn.execute("UPDATE points SET points = 0")
    conn.commit()
    conn.close()
    await ctx.send("🔄 All points have been reset.")

@bot.command(name="removepoints")
@commands.has_permissions(administrator=True)
async def removepoints_cmd(ctx: commands.Context, member: discord.Member, amount: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT points FROM points WHERE user_id = ?", (member.id,))
    row = cur.fetchone()
    if row:
        new_total = max(row[0] - amount, 0)
        cur.execute("UPDATE points SET points = ? WHERE user_id = ?", (new_total, member.id))
        conn.commit()
        await ctx.send(f"❌ Removed **{amount}** points from {member.mention}. New total: **{new_total}**")
    else:
        await ctx.send(f"⚠️ {member.mention} has no points.")
    conn.close()

bot.run(TOKEN)
