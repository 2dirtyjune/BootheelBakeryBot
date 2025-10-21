import os, random, string, time, logging, asyncio, asyncpg
from datetime import datetime, date
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ===== DATABASE =====
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_HwxTk65vqgMW@ep-spring-water-ad4np5eb-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require"
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
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            items JSONB,
            total NUMERIC,
            address JSONB,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS stats (
            date DATE PRIMARY KEY,
            total_orders INT DEFAULT 0,
            revenue NUMERIC DEFAULT 0
        );
        """)
        print("✅ Tables are ready")

async def save_order(pool, order_id, user_id, items, total, address, status="pending"):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO orders (order_id, user_id, items, total, address, status)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (order_id)
        DO UPDATE SET status=$6, items=$3, total=$4, address=$5;
        """, order_id, user_id, items, total, address, status)

# ===== TIMEZONE =====
try:
    from zoneinfo import ZoneInfo
    TZ_EST = ZoneInfo("America/New_York")
except Exception:
    TZ_EST = None

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8296620712:AAFQhebqqLLcjJgSjEbC9NkxvoT6DncrC7o")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "2125320923"))
ORDER_COOLDOWN = 24 * 60 * 60
HELP_COOLDOWN = 24 * 60 * 60

# ===== MENU =====
MENU_STRUCTURE = {
    "🖊️": ["Turn", "Jeeter Juice", "Dabwoods", "Crybaby", "Buzzbar"],
    "🍃": ["1"]
}
PRODUCT_IMAGES = {
    "Turn": "https://ibb.co/G4M71k9n",
    "Jeeter Juice": "https://ibb.co/gBLBy9W",
    "Dabwoods": "https://ibb.co/FkmqZ1d7",
    "Crybaby": "https://ibb.co/zhQdsVJF",
    "Buzzbar": "https://ibb.co/7tcTq6JJ",
    "1": "https://ibb.co/ZtZv3Yy"
}
PRODUCT_PRICES = {
    "Turn": {"1x": 35, "25x": 350, "50x": 650, "100x": 1200},
    "Jeeter Juice": {"1x": 35, "25x": 350, "50x": 650, "100x": 1200},
    "Dabwoods": {"1x": 40, "50x": 700},
    "Crybaby": {"1x": 35, "50x": 650, "100x": 1100},
    "Buzzbar": {"1x": 35, "50x": 650},
    "1": {"1oz": 100, "1/4": 350, "1/2": 650, "1lb": 1000, "2lb": 1800, "5lb (Free One)": 4000}
}
MENU_IMAGE_URL = "https://ibb.co/JRKtV7Vc"
CONFIRMATION_IMAGE_URL = "https://ibb.co/Y4tTxcHG"
INSTRUCTIONS_IMAGE_URL = "https://ibb.co/PSZ5py2"
FAQ_IMAGE_URL = "https://ibb.co/ZtZv3Yy"
MUSTREAD_IMAGE_URL = "https://ibb.co/S7Z9DGfX"

# ===== DATA =====
ORDERS_LOG, COMPLETED_ORDERS, USER_STATS = [], [], {}
KNOWN_USERS, PENDING_PAYMENTS, LAST_ORDER_BY_USER = set(), {}, {}

# ===== HELPERS =====
def fmt_ts(ts):
    dt = datetime.fromtimestamp(ts, TZ_EST) if TZ_EST else datetime.fromtimestamp(ts)
    return dt.strftime("%b %d, %Y – %I:%M %p %Z")

def generate_order_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def est_today_date():
    return (datetime.now(TZ_EST) if TZ_EST else datetime.now()).date()

def chunk_text(s, max_len=3500):
    chunks = []
    while len(s) > max_len:
        split_at = s.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(s[:split_at])
        s = s[split_at:].lstrip()
    if s: chunks.append(s)
    return chunks

async def _send_photo_or_link(message, url, caption, mode="Markdown", markup=None):
    try:
        return await message.reply_photo(photo=url, caption=caption, parse_mode=mode, reply_markup=markup)
    except:
        return await message.reply_text(f"{caption}\n{url}", parse_mode=mode, reply_markup=markup)

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private": return
    context.user_data["order"] = []
    KNOWN_USERS.add(user.id)
    await _send_photo_or_link(update.message, MENU_IMAGE_URL, f"👋 Hi {user.first_name}! Browse our categories below:", None,
                              InlineKeyboardMarkup([[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in MENU_STRUCTURE]))

async def faq(update, context):
    await _send_photo_or_link(update.message, FAQ_IMAGE_URL, "📘 *FAQ — read before ordering*", "Markdown")

async def mustread(update, context):
    await _send_photo_or_link(update.message, MUSTREAD_IMAGE_URL, "⚠️ *Must Read Before Ordering*", "Markdown")

# ===== SHIP COMMAND (fixed) =====
def find_latest_pending_order_for_user(uid):
    c = [o for o in ORDERS_LOG if o.get("user_id") == uid]
    return sorted(c, key=lambda o: o.get("ts", 0), reverse=True)[0] if c else None

async def ship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /ship <user_id> <tracking_number>")
        return
    user_id = int(context.args[0])
    tracking = context.args[1]
    order = find_latest_pending_order_for_user(user_id)
    if not order:
        await update.message.reply_text("❌ No pending order found.")
        return

    try: ORDERS_LOG.remove(order)
    except ValueError: pass

    ts = time.time()
    order_done = dict(order)
    order_done.update({"tracking": tracking, "completed_ts": ts})
    COMPLETED_ORDERS.append(order_done)
    LAST_ORDER_BY_USER[user_id] = order_done

    # ✅ update DB
    try:
        await save_order(context.bot_data["db_pool"], order["id"], user_id, order["items"], order["total"], order.get("address", {}), "shipped")
        print(f"📦 Order {order['id']} marked shipped in Neon DB.")
    except Exception as e:
        print(f"⚠️ DB update failed: {e}")

    await context.bot.send_message(user_id, f"🚚 *Order complete!* Tracking: `{tracking}`", parse_mode="Markdown")
    await update.message.reply_text(f"✅ Order #{order['id']} shipped.")

# ===== MAIN =====
if __name__ == "__main__":
    import asyncio

    async def runner():
        pool = await connect_db()
        await setup_tables(pool)
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.bot_data["db_pool"] = pool

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("faq", faq))
        app.add_handler(CommandHandler("mustread", mustread))
        app.add_handler(CommandHandler("ship", ship))

        print("✅ Bot connected to Neon & running...")
        # ✅ Correct way: no new event loop, just await polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await app.updater.idle()

    try:
        asyncio.get_event_loop().run_until_complete(runner())
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually.")



