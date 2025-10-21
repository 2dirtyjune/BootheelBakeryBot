import os
import random
import string
import time
import logging
import os
import asyncio
import asyncpg

# === DATABASE CONFIG ===
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
HELP_COOLDOWN = 24 * 60 * 60  # 24h cooldown for /requesthelp

# ===== MENU =====
MENU_STRUCTURE = {
    "🖊️": ["Turn", "Jeeter Juice", "Dabwoods", "Crybaby", "Buzzbar"],
    "🍃": ["1"],
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
    "1": {"1": 100, "1/4": 350, "1/2": 650, "1": 1000, "2": 1800, "5 (Free One)": 4000}
}

MENU_IMAGE_URL = "https://ibb.co/JRKtV7Vc"
CONFIRMATION_IMAGE_URL = "https://ibb.co/Y4tTxcHG"
INSTRUCTIONS_IMAGE_URL = "https://ibb.co/PSZ5py2"

# ===== NEW INFO COMMAND IMAGES (your exact URLs) =====
FAQ_IMAGE_URL = "https://ibb.co/ZtZv3Yy"
MUSTREAD_IMAGE_URL = "https://ibb.co/S7Z9DGfX"

# ===== DATA =====
ORDERS_LOG = []            # PENDING orders (current)
COMPLETED_ORDERS = []      # COMPLETED orders
USER_STATS = {}
KNOWN_USERS = set()
PENDING_PAYMENTS = {}      # user_id -> order_id (awaiting payment)
LAST_ORDER_BY_USER = {}    # user_id -> last order dict (pending or completed)


def fmt_ts(ts: float) -> str:
    """Format timestamp in EST if available, else server local time."""
    if TZ_EST:
        dt = datetime.fromtimestamp(ts, TZ_EST)
        return dt.strftime("%b %d, %Y – %I:%M %p %Z")
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%b %d, %Y – %I:%M %p")


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
    # Row1: Current | Completed
    # Row2: Stats | Accept
    # Row3: Ship
    # Row4: Delete | Reset
    # Row5: Back
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
        # Fallback: at least send the text and (if provided) the URL so Telegram shows a link preview
        if photo:
            await query.message.reply_text(f"{text}\n\n{photo}", reply_markup=markup, parse_mode=mode, disable_web_page_preview=False)
        else:
            await query.message.reply_text(text, reply_markup=markup, parse_mode=mode)


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


def est_today_date() -> date:
    if TZ_EST:
        return datetime.now(TZ_EST).date()
    return datetime.now().date()


# ===== helper: try photo; else send URL with preview =====
async def _send_photo_or_link(message, url: str, caption: str, mode: str = "Markdown", markup=None):
    try:
        return await message.reply_photo(photo=url, caption=caption, parse_mode=mode, reply_markup=markup)
    except Exception as e:
        log.warning(f"reply_photo failed for {url}: {e}")
        # Fallback so the user still sees the image via link preview
        return await message.reply_text(f"{caption}\n{url}", parse_mode=mode, reply_markup=markup, disable_web_page_preview=False)


# ===== INFO COMMANDS =====
async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send FAQ image with caption."""
    await _send_photo_or_link(
        update.message,
        FAQ_IMAGE_URL,
        "📘 *Frequently Asked Questions*\n\nRead this before ordering — it covers everything you need to know.",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )

async def mustread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send must-read image with caption."""
    await _send_photo_or_link(
        update.message,
        MUSTREAD_IMAGE_URL,
        "⚠️ *MUST READ BEFORE ORDERING*\n\nPlease review this info carefully to avoid mistakes or delays.",
        "Markdown",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )


# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type != "private":
        return
    context.user_data["order"] = []
    KNOWN_USERS.add(user.id)
    USER_STATS[user.id] = USER_STATS.get(user.id, 0)
    await _send_photo_or_link(
        update.message,
        MENU_IMAGE_URL,
        f"👋 Hi {user.first_name}! Browse our categories below:",
        None,  # no markdown needed; caption is plain
        build_main_menu()
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
    # clear any pending admin state when opening
    context.user_data.pop("admin_waiting", None)
    await update.message.reply_text(
        "🛠️ *Admin Console*\nChoose an action below:",
        parse_mode="Markdown",
        reply_markup=build_admin_menu()
    )


# ===== HANDLE SELECTION =====
async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    order = context.user_data.get("order", [])

    # === User flow ===
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
        items = "\n".join([f"• {i['qty']} {i['item']} - ${i['price']}" for i in order])
        context.user_data["pending_order"] = {"id": order_id, "items": items, "total": total}
        context.user_data["order"] = []
        context.user_data["collecting_address"] = "first_name"
        await query.message.reply_text("📦 Please enter your *first name*:", parse_mode="Markdown")

    # === Admin panel buttons ===
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
            await query.message.reply_text("💳 Use /accept <user_id> to confirm payment.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_ship":
            await query.message.reply_text("🚚 Use /ship <user_id> <tracking_number> to send shipping info.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_delete":
            context.user_data["admin_waiting"] = {"type": "delete"}  # expects a user id next
            await query.message.reply_text("🗑️ Send the *user ID* whose most recent order you want to delete (pending only).",
                                           parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_reset":
            context.user_data["admin_waiting"] = {"type": "reset"}   # expects a user id next
            await query.message.reply_text("🔄 Send the *user ID* to reset their session (cart, address, cooldown).",
                                           parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
        elif data == "admin_back":
            # clear any pending admin state
            context.user_data.pop("admin_waiting", None)
            await query.message.reply_text(
                "🛠️ *Admin Console*\nChoose an action below:",
                parse_mode="Markdown",
                reply_markup=build_admin_menu()
            )

        # Confirm buttons
        elif data.startswith("confirm_delete:"):
            uid = int(data.split(":")[1])
            # delete latest pending order for this user
            candidates = [o for o in ORDERS_LOG if o.get("user_id") == uid]
            if not candidates:
                await query.message.reply_text("❌ No pending order found for that user.",
                                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
            else:
                target = sorted(candidates, key=lambda o: o.get("ts", 0), reverse=True)[0]
                try:
                    ORDERS_LOG.remove(target)
                except ValueError:
                    pass
                PENDING_PAYMENTS.pop(uid, None)
                # notify user
                await context.bot.send_message(uid, "⚠️ Your last order has been reset by the admin. You can start a new one anytime with /start.")
                # notify admin
                await query.message.reply_text(f"🗑️ Order for user {uid} deleted.",
                                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))

        elif data.startswith("confirm_reset:"):
            uid = int(data.split(":")[1])
            # clear user session
            if uid in context.application.user_data:
                context.application.user_data[uid].clear()
            PENDING_PAYMENTS.pop(uid, None)
            await context.bot.send_message(uid, "🔄 Your session has been reset. You can start again with /start.")
            await query.message.reply_text(f"✅ User {uid} reset successfully.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))

        elif data.startswith("cancel_admin"):
            context.user_data.pop("admin_waiting", None)
            await query.message.reply_text("❌ Cancelled.",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))


async def send_orders_list(send_func, title: str, orders: list):
    if not orders:
        await send_func(f"{title}\n\nNo orders found.", parse_mode="Markdown")
        return

    # newest first
    sorted_orders = sorted(orders, key=lambda o: o.get("ts", 0), reverse=True)
    lines = []
    for o in sorted_orders:
        addr = o.get("address", {})
        lines.append(
            "────────────\n"
            f"#{o['id']}  |  🕒 {fmt_ts(o['ts'])}\n"
            f"🔁 Return #: {addr.get('return_number','—')}\n"
            f"👤 {o.get('name','')}  |  🆔 {o.get('user_id','')}\n"
            f"{o['items']}\n"
            f"💰 Total: ${o['total']}"
        )
    full = f"{title}\n\n" + "\n\n".join(lines)
    for part in chunk_text(full):
        await send_func(part, parse_mode="Markdown")


async def send_stats(send_func):
    total_rev = sum(o.get("total", 0) for o in COMPLETED_ORDERS)
    today = est_today_date()
    today_rev = 0
    for o in COMPLETED_ORDERS:
        ts = o.get("ts", time.time())
        dt = datetime.fromtimestamp(ts, TZ_EST) if TZ_EST else datetime.fromtimestamp(ts)
        if dt.date() == today:
            today_rev += o.get("total", 0)

    text = (
        "📊 *Admin Stats Report*\n"
        "────────────────────\n"
        f"🧾 Total Orders: {len(COMPLETED_ORDERS) + len(ORDERS_LOG)}\n"
        f"✅ Completed Orders: {len(COMPLETED_ORDERS)}\n"
        f"⌛ Pending Orders: {len(ORDERS_LOG)}\n"
        f"💰 Total Revenue (All Time): ${total_rev}\n"
        f"💵 Revenue (Today): ${today_rev}\n"
        f"🕒 Last Update: {fmt_ts(time.time())}"
    )
    await send_func(text, parse_mode="Markdown")


def get_last_order_for_user(user_id: int):
    """Return the user's latest order (from current or completed)."""
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


# ===== ADDRESS COLLECTION (Shipping + required Return #) =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    # Admin awaiting a user id for delete/reset
    if user.id == ADMIN_ID and context.user_data.get("admin_waiting"):
        waiting = context.user_data["admin_waiting"]
        if not text.isdigit():
            await update.message.reply_text("❗ Please send a numeric user ID.",
                                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="admin_back")]]))
            return
        target_uid = int(text)
        if waiting["type"] == "delete":
            # show confirm buttons
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Delete", callback_data=f"confirm_delete:{target_uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin")]
            ])
            await update.message.reply_text(f"⚠️ Are you sure you want to delete the most recent *pending* order for user {target_uid}?",
                                            parse_mode="Markdown", reply_markup=kb)
        elif waiting["type"] == "reset":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Reset", callback_data=f"confirm_reset:{target_uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin")]
            ])
            await update.message.reply_text(f"⚠️ Are you sure you want to reset *all* session data for user {target_uid}?",
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
        context.user_data["collecting_address"] = "town"
        await update.message.reply_text("🏙️ Enter your *town/city*:", parse_mode="Markdown")

    elif stage == "town":
        addr["town"] = text
        context.user_data["collecting_address"] = "state"
        await update.message.reply_text("🌎 Enter your *state*:", parse_mode="Markdown")

    elif stage == "state":
        addr["state"] = text
        context.user_data["collecting_address"] = "zip"
        await update.message.reply_text("🔢 Enter your *ZIP code*:", parse_mode="Markdown")

    elif stage == "zip":
        addr["zip"] = text
        context.user_data["collecting_address"] = "full"
        await update.message.reply_text("🏠 Enter your *full street address (apt/unit if any)*:", parse_mode="Markdown")

    elif stage == "full":
        addr["full"] = text
        context.user_data["collecting_address"] = "return_number"
        await update.message.reply_text("📬 Please enter your *Return #* (required):", parse_mode="Markdown")

    elif stage == "return_number":
        addr["return_number"] = text
        context.user_data["collecting_address"] = None

        order = context.user_data.get("pending_order", {})
        order_id = order.get("id")
        total = order.get("total")
        items = order.get("items")

        summary = (
            f"✅ *Order #{order_id} Complete!*\n\n"
            f"{items}\n\n"
            f"💰 *Total:* ${total}\n"
            f"🔁 *Return #:* {addr['return_number']}\n\n"
            f"📍 *Shipping Address:*\n"
            f"{addr['first_name']} {addr['last_name']}\n"
            f"{addr['full']}\n"
            f"{addr['town']}, {addr['state']} {addr['zip']}"
        )

        # Use same fallback mechanism so your ibb.co URLs still show via preview if needed
        await _send_photo_or_link(update.message, CONFIRMATION_IMAGE_URL, summary, "Markdown")

        await _send_photo_or_link(
            update.message,
            INSTRUCTIONS_IMAGE_URL,
            "🧾 *Thank you for your order!*\nPlease follow the instructions in the image to complete your payment.\n\n💬 If you made a mistake or need help with your order, type /requesthelp <your message> to contact the admin.",
            "Markdown",
        )

        order_record = {
            "id": order_id,
            "user_id": user.id,
            "name": user.first_name,
            "items": items,
            "total": total,
            "address": addr.copy(),
            "ts": time.time(),
        }
        ORDERS_LOG.append(order_record)
        LAST_ORDER_BY_USER[user.id] = order_record
        PENDING_PAYMENTS[user.id] = order_id

        admin_msg = (
            f"📦 *New Order #{order_id}*\n"
            f"🔁 Return #: {addr['return_number']}\n"
            f"👤 Buyer: {user.first_name} ({user.username or 'no username'})\n"
            f"🆔 ID: {user.id}\n\n"
            f"{items}\n💰 *Total:* ${total}\n\n"
            f"📍 *Shipping Address:*\n"
            f"{addr['first_name']} {addr['last_name']}\n"
            f"{addr['full']}\n"
            f"{addr['town']}, {addr['state']} {addr['zip']}\n"
            f"🕒 {fmt_ts(order_record['ts'])}\n"
            f"⌛ Awaiting payment."
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Once your payment is received, you'll get a confirmation message.")

    else:
        await update.message.reply_text("Sorry, I didn’t catch that. Please try again.")


# ===== ADMIN COMMANDS =====
async def accept_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /accept <user_id>")
        return
    user_id = int(context.args[0])
    if user_id not in PENDING_PAYMENTS:
        await update.message.reply_text("❌ No pending payment found for this user.")
        return
    await context.bot.send_message(
        user_id,
        "💳 *Payment accepted!* Please wait while we prepare your shipment.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(f"✅ Payment confirmed for user {user_id}.")


def find_latest_pending_order_for_user(user_id: int):
    if not ORDERS_LOG:
        return None
    candidates = [o for o in ORDERS_LOG if o.get("user_id") == user_id]
    if not candidates:
        return None
    return sorted(candidates, key=lambda o: o.get("ts", 0), reverse=True)[0]


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

    await context.bot.send_message(
        user_id,
        f"🚚 *Order complete!* Your tracking number is `{tracking_number}`.\nThank you for your order!",
        parse_mode="Markdown"
    )

    addr = order.get("address", {})
    admin_notice = (
        f"✅ *Order Shipped*\n"
        f"#{order.get('id')} | 🕒 {fmt_ts(order_completed_ts)}\n"
        f"👤 Buyer: {order.get('name','')} (ID: {order.get('user_id')})\n"
        f"🔁 Return #: {addr.get('return_number','—')}\n"
        f"{order.get('items','')}\n"
        f"💰 Total: ${order.get('total',0)}\n"
        f"🚚 Tracking: `{tracking_number}`"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_notice, parse_mode="Markdown")

    await update.message.reply_text(f"✅ Order #{order.get('id')} marked completed and shipping sent.")


# ===== /requesthelp COMMAND =====
async def request_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Users can DM /requesthelp <optional message>. Admin is DM'd; 24h cooldown enforced."""
    user = update.message.from_user
    now = time.time()

    last_t = context.user_data.get("last_help_time")
    if last_t and (now - last_t) < HELP_COOLDOWN:
        remaining = int((HELP_COOLDOWN - (now - last_t)) / 3600)
        await update.message.reply_text(f"⏳ Please wait {remaining}h before sending another help request.")
        return
    context.user_data["last_help_time"] = now

    msg_text = update.message.text
    parts = msg_text.split(" ", 1)
    user_msg = parts[1].strip() if len(parts) > 1 else "(no message provided)"

    latest_order = LAST_ORDER_BY_USER.get(user.id) or get_last_order_for_user(user.id)
    order_id = f"#{latest_order['id']}" if latest_order else "N/A"
    return_num = latest_order.get("address", {}).get("return_number", "—") if latest_order else "—"

    admin_alert = (
        "🚨 *Help Request*\n"
        f"👤 From: @{user.username or 'no_username'} ({user.id})\n"
        f"🧾 Order ID: {order_id}\n"
        f"🔁 Return #: {return_num}\n"
        f"💬 Message: {user_msg}"
    )
    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_alert, parse_mode="Markdown")

    await update.message.reply_text("✅ Help request sent! The admin will contact you soon.")


if __name__ == "__main__":
    # === Initialize the Neon database ===
    pool = asyncio.run(connect_db())
    asyncio.run(setup_tables(pool))

    # === Build and run the bot ===
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("accept", accept_payment))
    app.add_handler(CommandHandler("ship", ship_order))
    app.add_handler(CommandHandler("requesthelp", request_help))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("mustread", mustread))
    app.add_handler(CallbackQueryHandler(handle_selection))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Bot running... Press Ctrl+C to stop.")
    app.run_polling()
