import os
import random
import string
import time
import logging
import asyncio
import asyncpg
from datetime import datetime, date
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
    filters,
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8296620712:AAFQhebqqLLcjJgSjEbC9NkxvoT6DncrC7o")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "2125320923"))
ORDER_COOLDOWN = 24 * 60 * 60
HELP_COOLDOWN = 24 * 60 * 60

try:
    from zoneinfo import ZoneInfo
    TZ_EST = ZoneInfo("America/New_York")
except Exception:
    TZ_EST = None

# ===== DATA =====
ORDERS_LOG = []             # Pending orders
COMPLETED_ORDERS = []       # Finished orders
USER_PROFILES = {}          # user_id -> {"joined": ts, "spent": float, "completed": int}

# ===== MENU DATA =====
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

# ===== HELPERS =====
def fmt_ts(ts: float) -> str:
    """Format timestamp in readable EST/local time."""
    if TZ_EST:
        dt = datetime.fromtimestamp(ts, TZ_EST)
        return dt.strftime("%b %d, %Y – %I:%M %p %Z")
    return datetime.fromtimestamp(ts).strftime("%b %d, %Y – %I:%M %p")

def generate_order_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

async def _send_photo_or_link(message, url, caption, mode="Markdown", markup=None):
    """Try to send an image; fallback to text with preview if fails."""
    try:
        return await message.reply_photo(photo=url, caption=caption, parse_mode=mode, reply_markup=markup)
    except Exception:
        return await message.reply_text(f"{caption}\n{url}", parse_mode=mode, reply_markup=markup)

# ===== MENUS =====
def build_main_menu(order_count=0):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in MENU_STRUCTURE]
    keyboard.append([
        InlineKeyboardButton(f"🛒 View Cart ({order_count})", callback_data="view_cart"),
        InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")
    ])
    keyboard.append([InlineKeyboardButton("👤 View Profile", callback_data="view_profile")])
    return InlineKeyboardMarkup(keyboard)

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command — initialize user profile."""
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    if user.id not in USER_PROFILES:
        USER_PROFILES[user.id] = {"joined": time.time(), "spent": 0.0, "completed": 0}
    await _send_photo_or_link(
        update.message,
        MENU_IMAGE_URL,
        f"👋 Hi {user.first_name}! Browse our categories below:",
        None,
        build_main_menu()
    )

# ===== PROFILE =====
async def show_profile(query, user_id):
    """Display user profile with stats + view orders button."""
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
    """Display all completed orders for a user."""
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
    """Handle all inline button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "view_profile":
        await show_profile(query, user.id)
    elif data == "view_orders":
        await show_user_orders(query, user.id)
    elif data == "back":
        await query.message.reply_photo(MENU_IMAGE_URL, caption="👋 Choose a category:", reply_markup=build_main_menu())

# ===== ADMIN: SHIP ORDER =====
async def ship_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: mark order as shipped."""
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

    # update profile stats
    if user_id not in USER_PROFILES:
        USER_PROFILES[user_id] = {"joined": time.time(), "spent": 0, "completed": 0}
    USER_PROFILES[user_id]["completed"] += 1
    USER_PROFILES[user_id]["spent"] += order.get("total", 0)

    await context.bot.send_message(
        user_id,
        f"🚚 *Order complete!* Your tracking number is `{tracking}`.\nThank you for your order!",
        parse_mode="Markdown"
    )
    await update.message.reply_text(f"✅ Order #{order['id']} marked as shipped and saved to profile.")

# ===== MAIN LOOP =====
if __name__ == "__main__":
    async def main():
        pool = await connect_db()
        await setup_tables(pool)

        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ship", ship_order))
        app.add_handler(CallbackQueryHandler(handle_selection))

        print("✅ Bot running... Press Ctrl+C to stop.")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

    asyncio.get_event_loop().run_until_complete(main())




