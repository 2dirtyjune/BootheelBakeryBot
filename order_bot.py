import os
import random
import string
import time
import logging
import asyncio
import asyncpg
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    TZ_EST = ZoneInfo("America/New_York")
except Exception:
    TZ_EST = None  # fall back to local if zoneinfo isn't available

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8296620712:AAFQhebqqLLcjJgSjEbC9NkxvoT6DncrC7o")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "2125320923"))
ORDER_COOLDOWN = 24 * 60 * 60
HELP_COOLDOWN = 24 * 60 * 60

# ===== DATABASE CONFIG =====
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_HwxTk65vqgMW@ep-spring-water-ad4np5eb-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require",
)

async def connect_db():
    pool = await asyncpg.create_pool(DB_URL)
    print("✅ Connected to Neon database")
    return pool

async def setup_tables(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance NUMERIC DEFAULT 0,
                cart JSONB DEFAULT '{}'::jsonb
            );
        """)
    print("✅ Tables are ready")

async def save_user(pool, user_id, username, balance=0, cart=None):
    if cart is None:
        cart = {}
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, balance, cart)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id)
            DO UPDATE SET username=$2, balance=$3, cart=$4
        """, user_id, username, balance, cart)

async def load_user(pool, user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(row) if row else None

# ===== MENU =====
MENU_STRUCTURE = {
    "🖊️": ["Turn", "Jeeter Juice", "Dabwoods", "Crybaby", "Buzzbar"],
    "🍃": ["8-strain Mix n Match Light dep Smalls"],
    "🍄": ["Bluie Vuitton"],
}

PRODUCT_IMAGES = {
    "Turn": "https://ibb.co/G4M71k9n",
    "Jeeter Juice": "https://ibb.co/gBLBy9W",
    "Dabwoods": "https://ibb.co/FkmqZ1d7",
    "Crybaby": "https://ibb.co/zhQdsVJF",
    "Buzzbar": "https://ibb.co/7tcTq6JJ",
    "8-strain Mix n Match Light dep Smalls": "https://ibb.co/ZtZv3Yy",
    "Bluie Vuitton": "https://ibb.co/fd3F9vd5"
}

PRODUCT_PRICES = {
    "Turn": {"1x": 35, "25x": 350, "50x": 500, "100x": 1000},
    "Jeeter Juice": {"1x": 35, "25x": 350, "50x": 500, "100x": 1000},
    "Dabwoods": {"1x": 40, "50x": 700},
    "Crybaby": {"1x": 35, "50x": 500, "100x": 1000},
    "Buzzbar": {"1x": 35, "50x": 600},
    "8-strain Mix n Match Light Dep Smalls": {"1oz": 100, "1/4LB": 250, "1/2LB": 450, "1LB": 800, "2LB": 1600},
    "Bluie Vuitton": {"1oz": 100, "1/4LB": 300, "1/2LB": 550, "1LB": 800}
}

MENU_IMAGE_URL = "https://ibb.co/JRKtV7Vc"
CONFIRMATION_IMAGE_URL = "https://ibb.co/Y4tTxcHG"
INSTRUCTIONS_IMAGE_URL = "https://ibb.co/PSZ5py2"
FAQ_IMAGE_URL = "https://ibb.co/ZtZv3Yy"
MUSTREAD_IMAGE_URL = "https://ibb.co/S7Z9DGfX"

# ===== DATA =====
ORDERS_LOG = []
COMPLETED_ORDERS = []
USER_STATS = {}
KNOWN_USERS = set()
PENDING_PAYMENTS = {}
LAST_ORDER_BY_USER = {}

# ===== HELPERS =====
def fmt_ts(ts: float) -> str:
    if TZ_EST:
        dt = datetime.fromtimestamp(ts, TZ_EST)
    else:
        dt = datetime.fromtimestamp(ts)
    return dt.strftime("%b %d, %Y – %I:%M %p")

def chunk_text(s: str, max_len: int = 3500):
    chunks = []
    while len(s) > max_len:
        split_at = s.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(s[:split_at])
        s = s[split_at:].lstrip()
    if s:
        chunks.append(s)
    return chunks

def generate_order_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ===== MENUS =====
def build_main_menu(order_count=0):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in MENU_STRUCTURE]
    keyboard.append([
        InlineKeyboardButton(f"🛒 View Cart ({order_count})", callback_data="view_cart"),
        InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_category_menu(category, order_count=0):
    items = MENU_STRUCTURE.get(category, [])
    keyboard = [[InlineKeyboardButton(item, callback_data=f"item:{item}")] for item in items]
    keyboard.append([
        InlineKeyboardButton("⬅️ Back", callback_data="back"),
        InlineKeyboardButton(f"🛒 View Cart ({order_count})", callback_data="view_cart")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_price_menu(product, order_count=0):
    price_data = PRODUCT_PRICES.get(product, {})
    keyboard = [
        [InlineKeyboardButton(f"{qty} - ${price}", callback_data=f"add:{product}:{qty}:{price}")]
        for qty, price in price_data.items()
    ]
    keyboard.append([
        InlineKeyboardButton("⬅️ Back", callback_data="back"),
        InlineKeyboardButton(f"🛒 View Cart ({order_count})", callback_data="view_cart")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_cart_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Clear Cart", callback_data="clear_cart")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back")]
    ])

# ===== SAFE EDIT =====
async def safe_edit(query, text, markup=None, photo=None, mode=None):
    try:
        if photo:
            await query.edit_message_media(
                InputMediaPhoto(media=photo, caption=text, parse_mode=mode),
                reply_markup=markup
            )
        else:
            if query.message.caption:
                await query.edit_message_caption(caption=text, reply_markup=markup, parse_mode=mode)
            else:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode=mode)
    except Exception as e:
        log.warning(f"safe_edit fallback due to: {e}")
        if photo:
            await query.message.reply_text(f"{text}\n\n{photo}", reply_markup=markup, parse_mode=mode)
        else:
            await query.message.reply_text(text, reply_markup=markup, parse_mode=mode)

# ===== INFO COMMANDS =====
async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(FAQ_IMAGE_URL, caption="📘 *FAQ*", parse_mode="Markdown")

async def mustread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(MUSTREAD_IMAGE_URL, caption="⚠️ *Must Read Before Ordering*", parse_mode="Markdown")

# ===== START COMMAND =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    context.user_data["order"] = []
    KNOWN_USERS.add(user.id)
    USER_STATS[user.id] = USER_STATS.get(user.id, 0)
    await update.message.reply_photo(
        MENU_IMAGE_URL, caption=f"👋 Hi {user.first_name}! Browse our categories below:",
        reply_markup=build_main_menu()
    )

# ===== MAIN ENTRY (FIXED) =====
if __name__ == "__main__":
    import asyncio

    async def main():
        pool = await connect_db()
        await setup_tables(pool)

        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("faq", faq))
        app.add_handler(CommandHandler("mustread", mustread))

        print("✅ Bot is live and running...")
        await app.run_polling(close_loop=False)

    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
            print("💤 Bot still running inside existing event loop...")
        else:
            raise

