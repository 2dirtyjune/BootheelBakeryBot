import os
import time
import json
import random
import string
import logging
import asyncio
from datetime import datetime, date

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG / ENV
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8296620712:AAFQhebqqLLcjJgSjEbC9NkxvoT6DncrC7o")
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require"
)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "2125320923"))  # your Telegram user id
ORDER_COOLDOWN = 24 * 60 * 60
HELP_COOLDOWN = 24 * 60 * 60  # 24h cooldown for /requesthelp

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# =========================
# STATIC CONTENT (replace with your legal products & images)
# =========================
# Menu -> categories -> items
MENU_STRUCTURE = {
    "🧩 Gadgets": ["Widget A", "Widget B", "Widget C"],
    "🧦 Accessories": ["Cap", "Socks"],
}

# Optional images (can be page links; bot will fallback to link preview)
PRODUCT_IMAGES = {
    "Widget A": "https://example.com/a.jpg",
    "Widget B": "https://example.com/b.jpg",
    "Widget C": "https://example.com/c.jpg",
    "Cap": "https://example.com/cap.jpg",
    "Socks": "https://example.com/socks.jpg",
}

# Prices per item with labeled quantities
PRODUCT_PRICES = {
    "Widget A": {"1x": 25, "5x": 120, "10x": 230},
    "Widget B": {"1x": 35, "5x": 170, "10x": 330},
    "Widget C": {"1x": 20, "5x": 95, "10x": 180},
    "Cap": {"1x": 15, "3x": 40},
    "Socks": {"1x": 10, "5x": 45},
}

# Header / confirmation / instruction images (optional)
MENU_IMAGE_URL = "https://example.com/menu.jpg"
CONFIRMATION_IMAGE_URL = "https://example.com/confirm.jpg"
INSTRUCTIONS_IMAGE_URL = "https://example.com/checkout.jpg"

FAQ_IMAGE_URL = "https://example.com/faq.jpg"
MUSTREAD_IMAGE_URL = "https://example.com/mustread.jpg"

# =========================
# IN-MEMORY STATE
# =========================
ORDERS_LOG = []            # current (pending) orders
COMPLETED_ORDERS = []      # completed orders
USER_STATS = {}
KNOWN_USERS = set()
PENDING_PAYMENTS = {}      # user_id -> order_id
LAST_ORDER_BY_USER = {}    # user_id -> latest order dict

# =========================
# TIMEZONE HELPERS
# =========================
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    TZ_LOCAL = ZoneInfo("America/New_York")
except Exception:
    TZ_LOCAL = None  # fallback if not available

def fmt_ts(ts: float) -> str:
    if TZ_LOCAL:
        dt = datetime.fromtimestamp(ts, TZ_LOCAL)
        return dt.strftime("%b %d, %Y – %I:%M %p %Z")
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%b %d, %Y – %I:%M %p")

def est_today_date() -> date:
    if TZ_LOCAL:
        return datetime.now(TZ_LOCAL).date()
    return datetime.now().date()

# =========================
# DB HELPERS
# =========================
async def connect_db() -> asyncpg.pool.Pool:
    pool = await asyncpg.create_pool(DB_URL)
    log.info("✅ Connected to Postgres/Neon")
    return pool

async def setup_tables(pool: asyncpg.pool.Pool):
    async with pool.acquire() as conn:
        # Items stored as TEXT for simplicity (we store a human-readable summary).
        # Address stored as JSONB.
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
            items TEXT,
            total NUMERIC,
            address JSONB,
            status TEXT DEFAULT 'pending', -- pending | paid | shipped
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS stats (
            date DATE PRIMARY KEY,
            total_orders INT DEFAULT 0,
            revenue NUMERIC DEFAULT 0
        );
        """)
    log.info("✅ Tables are ready")

async def save_user(pool, user_id: int, username: str, balance=0, cart=None):
    if cart is None:
        cart = {}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, balance, cart)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                balance  = EXCLUDED.balance,
                cart     = EXCLUDED.cart
            """,
            user_id, username, balance, json.dumps(cart),
        )

async def save_order(pool, order_id: str, user_id: int, items_text: str, total: float, address: dict, status="pending"):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orders (order_id, user_id, items, total, address, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (order_id) DO UPDATE
            SET items=$3, total=$4, address=$5, status=$6
            """,
            order_id, user_id, items_text, total, json.dumps(address), status
        )

async def update_order_status(pool, order_id: str, status: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status=$1 WHERE order_id=$2",
            status, order_id
        )

async def increment_stats(pool, amount: float):
    today = est_today_date()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO stats (date, total_orders, revenue)
            VALUES ($1, 1, $2)
            ON CONFLICT (date)
            DO UPDATE SET
              total_orders = stats.total_orders + 1,
              revenue = stats.revenue + EXCLUDED.revenue
            """,
            today, amount
        )

# =========================
# UI BUILDERS
# =========================
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

# =========================
# MISC HELPERS
# =========================
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

async def _send_photo_or_link(message, url: str, caption: str, mode: str = "Markdown", markup=None):
    try:
        return await message.reply_photo(photo=url, caption=caption, parse_mode=mode, reply_markup=markup)
    except Exception as e:
        log.warning(f"reply_photo failed for {url}: {e}")
        return await message.reply_text(
            f"{caption}\n{url}",
            parse_mode=mode,
            reply_markup=markup,
            disable_web_page_preview=False
        )

async def safe_edit(query, text, markup=None, photo=None, mode=None):
    try:
        if photo:
            await query.edit_message_media(InputMediaPhoto(media=photo, caption=text, parse_mode=mode), reply_markup=markup)
        else:
            if query.message.caption:
                await query.edit_message_caption(caption=text, reply_markup=markup, parse_mode=mode)
            else:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode=mode)
    except Exception as e:
        log.warning(f"safe_edit fallback due to: {e}")
        if photo:
            await query.message.reply_text(f"{text}\n\n{photo}", reply_markup=markup, parse_mode=mode, disable_web_page_preview=False)
        else:
            await query.message.reply_text(text, reply_markup=markup, parse_mode=mode)

def get_last_order_for_user(user_id: int):
    latest = None
    for o in ORDERS_LOG:
        if o.get("user_id") == user_id:
            if (latest is None) or (o.get("ts", 0) > latest.get("ts", 0)):
                latest = o
    for o in COMPLETED_ORDERS:
        if o.get("user_id") == user_id:
            if (latest is None) or (o.get("ts", 0) > latest.get("ts", 0)):
                latest = o
    return latest

def find_latest_pending_order_for_user(user_id: int):
    candidates = [o for o in ORDERS_LOG if o.get("user_id") == user_id]
    if not candidates:
        return None
    return sorted(candidates, key=lambda o: o.get("ts", 0), reverse=True)[0]

# =========================
# COMMANDS: INFO
# =========================
async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_photo_or_link(
        update.message,
        FAQ_IMAGE_URL,
        "📘 *Frequently Asked Questions*\n\nRead this before ordering.",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )

async def mustread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_photo_or_link(
        update.message,
        MUSTREAD_IMAGE_URL,
        "⚠️ *Important Info Before Ordering*",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )

# =========================
# COMMANDS: START / ADMIN
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    context.user_data["order"] = []
    KNOWN_USERS.add(user.id)
    USER_STATS[user.id] = USER_STATS.get(user.id, 0)

    # upsert user in DB
    try:
        await save_user(context.application.bot_data["db_pool"], user.id, user.username or "", 0, {})
    except Exception as e:
        log.warning(f"Failed to save user: {e}")

    await _send_photo_or_link(
        update.message,
        MENU_IMAGE_URL,
        f"👋 Hi {user.first_name}! Browse our categories below:",
        None,
        build_main_menu(),
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
    context.user_data.pop("admin_waiting", None)
    await update.message.reply_text(
        "🛠️ *Admin Console*\nChoose an action below:",
        parse_mode="Markdown",
        reply_markup=build_admin_menu()
    )

# =========================
# CALLBACKS: MENU FLOW
# =========================
async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    order = context.user_data.get("order", [])

    # User flow
    if data.startswith("cat:"):
        cat = data.split(":", 1)[1]
        await safe_edit(query, f"📦 *{cat} Menu:*", build_category_menu(cat, len(order)), MENU_IMAGE_URL, "Markdown")

    elif data.startswith("item:"):
        product = data.split(":", 1)[1]
        await safe_edit(query, f"🛍️ *{product}*\nSelect a quantity:", build_price_menu(product, len(order)), PRODUCT_IMAGES.get(product, MENU_IMAGE_URL), "Markdown")

    elif data.startswith("add:"):
        _, product, qty, price = data.split(":")
        price = int(price)
        order.append({"item": product, "qty": qty, "price": price})
        context.user_data["order"] = order
        await query.answer(f"Added {qty} {product} ✅")
        markup = build_price_menu(product, len(order))
        await query.edit_message_reply_markup(reply_markup=markup)

    elif data == "view_cart":
        if not order:
            await safe_edit(query, "🛒 Your cart is empty!", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        else:
            cart_lines = [f"• {i['qty']} {i['item']} - ${i['price']}" for i in order]
            total = sum(i['price'] for i in order)
            await safe_edit(query, f"🛒 *Your Cart:*\n\n" + "\n".join(cart_lines) + f"\n\n💰 *Total:* ${total}", build_cart_menu(), None, "Markdown")

    elif data == "clear_cart":
        context.user_data["order"] = []
        await safe_edit(query, "🗑️ Cart cleared!", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "back":
        await safe_edit(query, "👋 Choose a category:", build_main_menu(len(order)), MENU_IMAGE_URL)

    elif data == "confirm_order":
        if not order:
            await safe_edit(query, "You didn’t pick anything 😅")
            return
        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, finalize", callback_data="done")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="back")]
        ])
        await safe_edit(query, "⚠️ Are you sure you’re ready to finalize your order?", confirm_markup)

    elif data == "done":
        now = time.time()
        if (t := context.user_data.get("last_order_time")) and now - t < ORDER_COOLDOWN:
            hrs = int((ORDER_COOLDOWN - (now - t)) / 3600)
            await safe_edit(query, f"⏳ Wait {hrs}h before another order.")
            return
        context.user_data["last_order_time"] = now
        order_id = generate_order_id()
        total = sum(i['price'] for i in order)
        items_text = "\n".join([f"• {i['qty']} {i['item']} - ${i['price']}" for i in order])
        context.user_data["pending_order"] = {"id": order_id, "items": items_text, "total": total}
        context.user_data["order"] = []
        context.user_data["collecting_address"] = "first_name"
        await query.message.reply_text("📦 Please enter your *first name*:", parse_mode="Markdown")

    # Admin panel (buttons only)
    elif user.id == ADMIN_ID:
        if data == "admin_current":
            await send_orders_list(query.message.reply_text, "📦 *Current Orders*", ORDERS_LOG)
            await query.message.reply_text("⬅️ Back to Main Menu", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_completed":
            await send_orders_list(query.message.reply_text, "✅ *Completed Orders*", COMPLETED_ORDERS)
            await query.message.reply_text("⬅️ Back to Main Menu", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_stats":
            await send_stats(query.message.reply_text)
            await query.message.reply_text("⬅️ Back to Main Menu", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_accept":
            await query.message.reply_text("💳 Use /accept <user_id> to mark last pending order as *paid*.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_ship":
            await query.message.reply_text("🚚 Use /ship <user_id> <tracking_number> to mark as *shipped*.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_delete":
            context.user_data["admin_waiting"] = {"type": "delete"}
            await query.message.reply_text("🗑️ Send the *user ID* whose most recent pending order you want to delete.",
                                           parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_reset":
            context.user_data["admin_waiting"] = {"type": "reset"}
            await query.message.reply_text("🔄 Send the *user ID* to reset their session (cart, address, cooldown).",
                                           parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_back":
            context.user_data.pop("admin_waiting", None)
            await query.message.reply_text("🛠️ *Admin Console*\nChoose an action below:", parse_mode="Markdown", reply_markup=build_admin_menu())

# =========================
# TEXT HANDLER: ADDRESS FLOW + ADMIN WAITING
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    # Admin asking for user id (delete/reset)
    if user.id == ADMIN_ID and context.user_data.get("admin_waiting"):
        waiting = context.user_data["admin_waiting"]
        if not text.isdigit():
            await update.message.reply_text("❗ Please send a numeric user ID.",
                                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
            return
        target_uid = int(text)
        if waiting["type"] == "delete":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Delete", callback_data=f"confirm_delete:{target_uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
            ])
            await update.message.reply_text(f"⚠️ Delete the most recent *pending* order for user {target_uid}?",
                                            parse_mode="Markdown", reply_markup=kb)
        elif waiting["type"] == "reset":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Reset", callback_data=f"confirm_reset:{target_uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
            ])
            await update.message.reply_text(f"⚠️ Reset *all* session data for user {target_uid}?",
                                            parse_mode="Markdown", reply_markup=kb)
        return

    # User address flow
    stage = context.user_data.get("collecting_address")
    if not stage:
        return

    addr = context.user_data.setdefault("address", {})

    if stage == "first_name":
        addr["first_name"] = text
        context.user_data["collecting_address"] = "last_name"
        await update.message.reply_text("📝 Enter your *last name*:", parse_mode="Markdown")

    elif stage == "last_name":
        addr["last_name"] = text
        context.user_data["collecting_address"] = "city"
        await update.message.reply_text("🏙️ Enter your *city*:", parse_mode="Markdown")

    elif stage == "city":
        addr["city"] = text
        context.user_data["collecting_address"] = "state"
        await update.message.reply_text("🌎 Enter your *state/region*:", parse_mode="Markdown")

    elif stage == "state":
        addr["state"] = text
        context.user_data["collecting_address"] = "zip"
        await update.message.reply_text("🔢 Enter your *ZIP/Postal code*:", parse_mode="Markdown")

    elif stage == "zip":
        addr["zip"] = text
        context.user_data["collecting_address"] = "street"
        await update.message.reply_text("🏠 Enter your *street address* (apt/unit if any):", parse_mode="Markdown")

    elif stage == "street":
        addr["street"] = text
        context.user_data["collecting_address"] = "reference"
        await update.message.reply_text("📬 Enter any *order reference / notes* (optional). Send '-' to skip:", parse_mode="Markdown")

    elif stage == "reference":
        if text != "-":
            addr["reference"] = text
        context.user_data["collecting_address"] = None

        # finalize order
        order = context.user_data.get("pending_order", {})
        order_id = order.get("id")
        total = order.get("total", 0)
        items_text = order.get("items", "")

        summary = (
            f"✅ *Order #{order_id} Created!*\n\n"
            f"{items_text}\n\n"
            f"💰 *Total:* ${total}\n\n"
            f"📍 *Shipping Address:*\n"
            f"{addr.get('first_name','')} {addr.get('last_name','')}\n"
            f"{addr.get('street','')}\n"
            f"{addr.get('city','')}, {addr.get('state','')} {addr.get('zip','')}"
        )
        await _send_photo_or_link(update.message, CONFIRMATION_IMAGE_URL, summary, "Markdown")
        await _send_photo_or_link(
            update.message,
            INSTRUCTIONS_IMAGE_URL,
            "🧾 *Thank you for your order!* Follow the instructions shown here to complete checkout.\n\nIf you need help, use /requesthelp <message>.",
            "Markdown",
        )

        order_record = {
            "id": order_id,
            "user_id": user.id,
            "name": user.first_name,
            "items": items_text,
            "total": total,
            "address": addr.copy(),
            "ts": time.time(),
        }
        ORDERS_LOG.append(order_record)
        LAST_ORDER_BY_USER[user.id] = order_record
        PENDING_PAYMENTS[user.id] = order_id

        # Save to DB
        try:
            pool = context.application.bot_data["db_pool"]
            await save_user(pool, user.id, user.username or "", 0, {})
            await save_order(pool, order_id, user.id, items_text, total, addr.copy(), "pending")
            await increment_stats(pool, float(total))
        except Exception as e:
            log.error(f"DB save error: {e}")

        # Notify admin
        admin_msg = (
            f"📦 *New Order #{order_id}*\n"
            f"👤 Buyer: {user.first_name} (@{user.username or '—'})\n"
            f"🆔 ID: {user.id}\n\n"
            f"{items_text}\n💰 *Total:* ${total}\n\n"
            f"📍 *Address:*\n"
            f"{addr.get('first_name','')} {addr.get('last_name','')}\n"
            f"{addr.get('street','')}\n"
            f"{addr.get('city','')}, {addr.get('state','')} {addr.get('zip','')}\n"
            f"🕒 {fmt_ts(order_record['ts'])}\n"
            f"⌛ Status: pending"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown")
        except Exception:
            pass

        await update.message.reply_text("✅ Once your payment is received, you'll get a confirmation message.")

    else:
        await update.message.reply_text("Sorry, I didn’t catch that. Please try again.")

# =========================
# ADMIN COMMANDS
# =========================
async def accept_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /accept <user_id>")
        return
    user_id = int(context.args[0])

    # find latest pending for this user
    order = find_latest_pending_order_for_user(user_id)
    if not order:
        await update.message.reply_text("❌ No pending order found for that user.")
        return

    # mark paid in DB
    try:
        await update_order_status(context.application.bot_data["db_pool"], order["id"], "paid")
    except Exception as e:
        log.error(f"Failed to update DB status to paid: {e}")

    # notify buyer
    try:
        await context.bot.send_message(
            user_id,
            "💳 *Payment accepted!* We’re preparing your shipment.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ Payment confirmed for user {user_id} (order {order['id']}).")

async def ship_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /ship <user_id> <tracking_number>")
        return
    user_id = int(context.args[0])
    tracking_number = context.args[1]

    order = find_latest_pending_order_for_user(user_id)
    if not order:
        await update.message.reply_text("❌ No pending order found for that user.")
        return

    # move to completed
    try:
        ORDERS_LOG.remove(order)
    except ValueError:
        pass

    order_completed_ts = time.time()
    order_completed = dict(order)
    order_completed["tracking"] = tracking_number
    order_completed["completed_ts"] = order_completed_ts
    COMPLETED_ORDERS.append(order_completed)
    LAST_ORDER_BY_USER[user_id] = order_completed
    PENDING_PAYMENTS.pop(user_id, None)

    # mark shipped in DB
    try:
        await update_order_status(context.application.bot_data["db_pool"], order["id"], "shipped")
    except Exception as e:
        log.error(f"Failed to update DB status to shipped: {e}")

    # notify buyer
    await context.bot.send_message(
        user_id,
        f"🚚 *Order shipped!* Your tracking number is `{tracking_number}`.\nThank you!",
        parse_mode="Markdown"
    )

    # notify admin
    addr = order.get("address", {})
    admin_notice = (
        f"✅ *Order Shipped*\n"
        f"#{order.get('id')} | 🕒 {fmt_ts(order_completed_ts)}\n"
        f"👤 Buyer: {order.get('name','')} (ID: {order.get('user_id')})\n"
        f"{order.get('items','')}\n"
        f"💰 Total: ${order.get('total',0)}\n"
        f"🚚 Tracking: `{tracking_number}`"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_notice, parse_mode="Markdown")

    await update.message.reply_text(f"✅ Order #{order.get('id')} marked shipped and user notified.")

# =========================
# ADMIN LISTS / STATS
# =========================
async def send_orders_list(send_func, title: str, orders: list):
    if not orders:
        await send_func(f"{title}\n\nNo orders found.", parse_mode="Markdown")
        return

    sorted_orders = sorted(orders, key=lambda o: o.get("ts", 0), reverse=True)
    lines = []
    for o in sorted_orders:
        addr = o.get("address", {})
        lines.append(
            "────────────\n"
            f"#{o['id']}  |  🕒 {fmt_ts(o['ts'])}\n"
            f"👤 {o.get('name','')}  |  🆔 {o.get('user_id','')}\n"
            f"{o['items']}\n"
            f"💰 Total: ${o['total']}\n"
            f"📍 {addr.get('city','')}, {addr.get('state','')} {addr.get('zip','')}"
        )
    full = f"{title}\n\n" + "\n\n".join(lines)
    for part in chunk_text(full):
        await send_func(part, parse_mode="Markdown")

async def send_stats(send_func):
    total_rev = sum(o.get("total", 0) for o in COMPLETED_ORDERS)
    today = est_today_date()
    today_rev = 0
    for o in COMPLETED_ORDERS:
        ts = o.get("completed_ts", o.get("ts", time.time()))
        dt = datetime.fromtimestamp(ts, TZ_LOCAL) if TZ_LOCAL else datetime.fromtimestamp(ts)
        if dt.date() == today:
            today_rev += o.get("total", 0)

    text = (
        "📊 *Admin Stats Report*\n"
        "────────────────────\n"
        f"🧾 Total Orders (in-memory): {len(COMPLETED_ORDERS) + len(ORDERS_LOG)}\n"
        f"✅ Completed Orders: {len(COMPLETED_ORDERS)}\n"
        f"⌛ Pending Orders: {len(ORDERS_LOG)}\n"
        f"💰 Total Revenue (Completed/In-Mem): ${total_rev}\n"
        f"💵 Revenue (Today): ${today_rev}\n"
        f"🕒 Last Update: {fmt_ts(time.time())}"
    )
    await send_func(text, parse_mode="Markdown")

# =========================
# HELP REQUEST
# =========================
async def request_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    now = time.time()

    last_t = context.user_data.get("last_help_time")
    if last_t and (now - last_t) < HELP_COOLDOWN:
        remaining = int((HELP_COOLDOWN - (now - last_t)) / 3600)
        await update.message.reply_text(f"⏳ Please wait {remaining}h before sending another help request.")
        return
    context.user_data["last_help_time"] = now

    parts = update.message.text.split(" ", 1)
    user_msg = parts[1].strip() if len(parts) > 1 else "(no message provided)"

    latest_order = LAST_ORDER_BY_USER.get(user.id) or get_last_order_for_user(user.id)
    order_id = f"#{latest_order['id']}" if latest_order else "N/A"

    admin_alert = (
        "🚨 *Help Request*\n"
        f"👤 From: @{user.username or 'no_username'} ({user.id})\n"
        f"🧾 Order ID: {order_id}\n"
        f"💬 Message: {user_msg}"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_alert, parse_mode="Markdown")
    await update.message.reply_text("✅ Help request sent! The admin will contact you soon.")

# =========================
# APP ENTRY (PTB v21+)
# =========================

if __name__ == "__main__":
    async def main():
        # ✅ Connect to Neon Postgres
        pool = await connect_db()
        await setup_tables(pool)

        # ✅ Initialize Telegram bot
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.bot_data["db_pool"] = pool

        # ✅ Handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("admin", admin))
        app.add_handler(CommandHandler("accept", accept_payment))
        app.add_handler(CommandHandler("ship", ship_order))
        app.add_handler(CommandHandler("requesthelp", request_help))
        app.add_handler(CommandHandler("faq", faq))
        app.add_handler(CommandHandler("mustread", mustread))
        app.add_handler(CallbackQueryHandler(handle_selection))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        log.info("✅ Connected to Postgres/Neon")
        log.info("✅ Tables are ready")
        log.info("✅ Bot connected to DB & starting polling...")

        # ✅ Correct loop-safe polling for PTB 21+
        await app.initialize()
        await app.start()
        await app.run_polling(stop_signals=None)  # prevents Render loop closing issue
        await app.stop()
        await app.shutdown()

    # ✅ Safe for both Render & local runs
    try:
        asyncio.run(main())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())




