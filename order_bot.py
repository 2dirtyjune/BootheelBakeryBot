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

# ===== DATABASE CONFIG =====
DB_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_HwxTk65vqgMW@ep-spring-water-ad4np5eb-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require")

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
ORDERS_LOG = []
COMPLETED_ORDERS = []
USER_STATS = {}
KNOWN_USERS = set()
PENDING_PAYMENTS = {}
LAST_ORDER_BY_USER = {}
USER_PROFILES = {}  # user_id -> {"joined": ts, "spent": float, "completed": int}

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

MENU_IMAGE_URL = "https://ibb.co/h1MtmWf0"
CONFIRMATION_IMAGE_URL = "https://ibb.co/Y4tTxcHG"
INSTRUCTIONS_IMAGE_URL = "https://ibb.co/PSZ5py2"
FAQ_IMAGE_URL = "https://ibb.co/ZtZv3Yy"
MUSTREAD_IMAGE_URL = "https://ibb.co/S7Z9DGfX"

# ===== HELPERS =====
def fmt_ts(ts: float) -> str:
    if TZ_EST:
        dt = datetime.fromtimestamp(ts, TZ_EST)
        return dt.strftime("%b %d, %Y – %I:%M %p %Z")
    return datetime.fromtimestamp(ts).strftime("%b %d, %Y – %I:%M %p")

def generate_order_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

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

def est_today_date() -> date:
    if TZ_EST:
        return datetime.now(TZ_EST).date()
    return datetime.now().date()

async def _send_photo_or_link(message, url, caption, mode="Markdown", markup=None):
    try:
        return await message.reply_photo(photo=url, caption=caption, parse_mode=mode, reply_markup=markup)
    except Exception as e:
        log.warning(f"reply_photo failed for {url}: {e}")
        return await message.reply_text(f"{caption}\n{url}", parse_mode=mode, reply_markup=markup, disable_web_page_preview=False)

# ===== MENUS =====
def build_main_menu(order_count=0):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in MENU_STRUCTURE]
    keyboard.append([
        InlineKeyboardButton(f"🛒 View Cart ({order_count})", callback_data="view_cart"),
        InlineKeyboardButton("✅ Place Order", callback_data="confirm_order")
    ])
    keyboard.append([
        InlineKeyboardButton("👤 View Profile", callback_data="view_profile")
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

def build_cart_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Clear Cart", callback_data="clear_cart")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back")]
    ])

def build_admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Current Orders", callback_data="admin_current"),
         InlineKeyboardButton("✅ Completed Orders", callback_data="admin_completed")],
        [InlineKeyboardButton("📊 View Stats", callback_data="admin_stats"),
         InlineKeyboardButton("💳 Accept Payment", callback_data="admin_accept")],
        [InlineKeyboardButton("🚚 Ship Order", callback_data="admin_ship")],
        [InlineKeyboardButton("🗑️ Delete Order", callback_data="admin_delete"),
         InlineKeyboardButton("🔄 Reset User", callback_data="admin_reset")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")],
    ])

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    context.user_data["order"] = []
    KNOWN_USERS.add(user.id)
    USER_STATS[user.id] = USER_STATS.get(user.id, 0)
    if user.id not in USER_PROFILES:
        USER_PROFILES[user.id] = {"joined": time.time(), "spent": 0.0, "completed": 0}
    await _send_photo_or_link(
        update.message,
        MENU_IMAGE_URL,
        f"👋 Hi {user.first_name}! Browse our categories below:",
        None,
        build_main_menu()
    )

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_photo_or_link(
        update.message,
        FAQ_IMAGE_URL,
        "📘 *Frequently Asked Questions*\n\nRead this before ordering — it covers everything you need to know.",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )

async def mustread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_photo_or_link(
        update.message,
        MUSTREAD_IMAGE_URL,
        "⚠️ *MUST READ BEFORE ORDERING*\n\nPlease review this info carefully to avoid mistakes or delays.",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )

# ===== HANDLE BUTTONS =====
async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    order = context.user_data.get("order", [])

    # === View Profile ===
    if data == "view_profile":
        profile = USER_PROFILES.get(user.id, {})
        joined_ts = profile.get("joined", time.time())
        joined_fmt = fmt_ts(joined_ts)
        spent = profile.get("spent", 0)
        completed = profile.get("completed", 0)
        text = (
            f"👤 *Your Profile*\n"
            f"──────────────────\n"
            f"🧾 Orders Completed: {completed}\n"
            f"💰 Total Spent: ${spent:.2f}\n"
            f"📅 Member Since: {joined_fmt}\n"
        )
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ]))
        return

    # === Normal Menu Navigation ===
    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        await query.message.reply_photo(MENU_IMAGE_URL, caption=f"📦 *{cat} Menu:*", parse_mode="Markdown", reply_markup=build_category_menu(cat, len(order)))

    elif data.startswith("item:"):
        product = data.split(":", 1)[1]
        await query.message.reply_photo(PRODUCT_IMAGES.get(product, MENU_IMAGE_URL),
                                        caption=f"🛍️ *{product}*\nSelect a quantity:",
                                        parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup([
                                            *[[InlineKeyboardButton(f"{qty} - ${price}", callback_data=f"add:{product}:{qty}:{price}")]
                                              for qty, price in PRODUCT_PRICES.get(product, {}).items()],
                                            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
                                        ]))
    elif data.startswith("add:"):
        _, product, qty, price = data.split(":")
        price = int(price)
        order.append({"item": product, "qty": qty, "price": price})
        context.user_data["order"] = order
        await query.answer(f"Added {qty} {product} ✅")

    elif data == "view_cart":
        if not order:
            await query.message.reply_text("🛒 Your cart is empty!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        else:
            lines = [f"• {i['qty']} {i['item']} - ${i['price']}" for i in order]
            total = sum(i["price"] for i in order)
            await query.message.reply_text(f"🛒 *Your Cart:*\n\n" + "\n".join(lines) + f"\n\n💰 *Total:* ${total}",
                                           parse_mode="Markdown",
                                           reply_markup=build_cart_menu())

    elif data == "clear_cart":
        context.user_data["order"] = []
        await query.message.reply_text("🗑️ Cart cleared!")

    elif data == "back":
        await query.message.reply_photo(MENU_IMAGE_URL, caption="👋 Choose a category:", reply_markup=build_main_menu(len(order)))

# ===== ADMIN + SHIPPING =====
async def ship_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /ship <user_id> <tracking_number>")
        return

    user_id = int(context.args[0])
    tracking_number = context.args[1]
    order = next((o for o in ORDERS_LOG if o.get("user_id") == user_id), None)
    if not order:
        await update.message.reply_text("❌ No pending order found.")
        return

    ORDERS_LOG.remove(order)
    order_completed_ts = time.time()
    order_completed = dict(order)
    order_completed["tracking"] = tracking_number
    order_completed["completed_ts"] = order_completed_ts
    COMPLETED_ORDERS.append(order_completed)
    LAST_ORDER_BY_USER[user_id] = order_completed

    # === Update profile ===
    if user_id in USER_PROFILES:
        USER_PROFILES[user_id]["completed"] += 1
        USER_PROFILES[user_id]["spent"] += order_completed.get("total", 0)
    else:
        USER_PROFILES[user_id] = {"joined": time.time(), "completed": 1, "spent": order_completed.get("total", 0)}

    await context.bot.send_message(
        user_id,
        f"🚚 *Order complete!* Your tracking number is `{tracking_number}`.\nThank you for your order!",
        parse_mode="Markdown"
    )
    await update.message.reply_text(f"✅ Order #{order.get('id')} marked as shipped.")

# ===== MAIN LOOP =====
if __name__ == "__main__":
    async def main():
        pool = await connect_db()
        await setup_tables(pool)

        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ship", ship_order))
        app.add_handler(CommandHandler("faq", faq))
        app.add_handler(CommandHandler("mustread", mustread))
        app.add_handler(CallbackQueryHandler(handle_selection))

        print("✅ Bot running... Press Ctrl+C to stop.")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

    asyncio.get_event_loop().run_until_complete(main())

