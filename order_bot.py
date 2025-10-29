




import os
import random
import string
import time
import logging
import asyncio
import asyncpg
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===== DATABASE CONFIG =====
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
        """)
        print("✅ Tables are ready")

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "2125320923"))

try:
    from zoneinfo import ZoneInfo
    TZ_EST = ZoneInfo("America/New_York")
except Exception:
    TZ_EST = None

# ===== DATA =====
ORDERS_LOG = []
COMPLETED_ORDERS = []
USER_PROFILES = {}

# ===== PRODUCT MENU =====
MENU_STRUCTURE = {
    "🖊️": ["Turn", "Jeeter Juice", "Dabwoods", "Crybaby", "Buzzbar"],
    "🍃": ["8-strain Mix n Match Light dep Smalls"],
    "🍄": ["Bluie Vuitton"],
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

# ===== UTILITIES =====
def fmt_ts(ts: float) -> str:
    if TZ_EST:
        dt = datetime.fromtimestamp(ts, TZ_EST)
        return dt.strftime("%b %d, %Y – %I:%M %p %Z")
    return datetime.fromtimestamp(ts).strftime("%b %d, %Y – %I:%M %p")

def generate_order_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ===== MENU BUILDERS =====
def build_main_menu():
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in MENU_STRUCTURE]
    keyboard.append([
        InlineKeyboardButton("🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")
    ])
    keyboard.append([InlineKeyboardButton("👤 View Profile", callback_data="view_profile")])
    return InlineKeyboardMarkup(keyboard)

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    if user.id not in USER_PROFILES:
        USER_PROFILES[user.id] = {"joined": time.time(), "spent": 0.0, "completed": 0}
    await update.message.reply_photo(
        photo=MENU_IMAGE_URL,
        caption=f"👋 Hi {user.first_name}! Browse our categories below:",
        reply_markup=build_main_menu()
    )

# ===== PROFILE SYSTEM =====
async def show_profile(query, user_id):
    profile = USER_PROFILES.get(user_id, {})
    joined = fmt_ts(profile.get("joined", time.time()))
    spent = profile.get("spent", 0)
    completed = profile.get("completed", 0)
    text = (
        f"👤 *Your Profile*\n"
        f"──────────────────\n"
        f"🧾 Orders Completed: {completed}\n"
        f"💰 Total Spent: ${spent:.2f}\n"
        f"📅 Member Since: {joined}\n"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 View Orders", callback_data="view_orders")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")]
    ])
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

async def show_user_orders(query, user_id):
    user_orders = [o for o in COMPLETED_ORDERS if o.get("user_id") == user_id]
    if not user_orders:
        await query.message.reply_text("📦 You have no completed orders yet.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="view_profile")]
        ]))
        return
    user_orders = sorted(user_orders, key=lambda o: o.get("completed_ts", 0), reverse=True)
    lines = []
    for o in user_orders:
        lines.append(
            f"──────────────\n"
            f"🧾 *Order #{o['id']}*\n"
            f"🕒 {fmt_ts(o.get('completed_ts', o.get('ts', time.time())))}\n"
            f"💰 ${o['total']}\n"
            f"🚚 Tracking: `{o.get('tracking', '—')}`\n"
            f"{o['items']}"
        )
    text = "📦 *Your Completed Orders:*\n\n" + "\n\n".join(lines)
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Profile", callback_data="view_profile")]
    ]))

# ===== CALLBACK HANDLER =====
async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    if data == "view_profile":
        await show_profile(query, user.id)
    elif data == "view_orders":
        await show_user_orders(query, user.id)
    elif data == "back":
        await query.message.reply_photo(photo=MENU_IMAGE_URL, caption="👋 Choose a category:", reply_markup=build_main_menu())

# ===== ADMIN SHIP COMMAND =====
async def ship_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /ship <user_id> <tracking_number>")
        return

    user_id = int(context.args[0])
    tracking = context.args[1]
    order = next((o for o in ORDERS_LOG if o.get("user_id") == user_id), None)
    if not order:
        await update.message.reply_text("❌ No pending order found.")
        return

    ORDERS_LOG.remove(order)
    order["tracking"] = tracking
    order["completed_ts"] = time.time()
    COMPLETED_ORDERS.append(order)

    if user_id not in USER_PROFILES:
        USER_PROFILES[user_id] = {"joined": time.time(), "spent": 0, "completed": 0}
    USER_PROFILES[user_id]["completed"] += 1
    USER_PROFILES[user_id]["spent"] += order.get("total", 0)

    await context.bot.send_message(
        user_id,
        f"🚚 *Order complete!* Your tracking number is `{tracking}`.\nThank you for your order!",
        parse_mode="Markdown"
    )
    await update.message.reply_text(f"✅ Order #{order['id']} marked as shipped.")

# ===== KEEP ALIVE =====
async def keep_alive():
    while True:
        print("💤 Bot still running...")
        await asyncio.sleep(60)

# ===== RUN BOT =====
async def main():
    pool = await connect_db()
    await setup_tables(pool)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ship", ship_order))
    app.add_handler(CallbackQueryHandler(handle_selection))

    asyncio.create_task(keep_alive())
    print("✅ Bot is live and running...")
    await app.run_polling()

# ===== SAFE RENDER LOOP FIX =====
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(main())
            loop.run_forever()
        else:
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("🟥 Bot manually stopped")


