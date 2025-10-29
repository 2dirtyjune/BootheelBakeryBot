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
                cart JSONB DEFAULT '{}'::jsonb,
                joined_at TIMESTAMP DEFAULT NOW(),
                total_spent NUMERIC DEFAULT 0
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                total NUMERIC,
                return_number TEXT,
                ts TIMESTAMP DEFAULT NOW()
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

async def get_user_profile(pool, user_id):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        orders = await conn.fetch("SELECT * FROM orders WHERE user_id=$1 ORDER BY ts DESC", user_id)
        return user, orders
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
    "🍃": ["8-strain Mix n Match Light dep Smalls"],
}

PRODUCT_IMAGES = {
    "Turn": "https://ibb.co/G4M71k9n",
    "Jeeter Juice": "https://ibb.co/gBLBy9W",
    "Dabwoods": "https://ibb.co/FkmqZ1d7",
    "Crybaby": "https://ibb.co/zhQdsVJF",
    "Buzzbar": "https://ibb.co/7tcTq6JJ",
    "8-strain Mix n Match Light dep Smalls": "https://ibb.co/ZtZv3Yy"
}

PRODUCT_PRICES = {
    "Turn": {"1x": 35, "25x": 350, "50x": 650, "100x": 1200},
    "Jeeter Juice": {"1x": 35, "25x": 350, "50x": 650, "100x": 1200},
    "Dabwoods": {"1x": 40, "50x": 700},
    "Crybaby": {"1x": 35, "50x": 650, "100x": 1100},
    "Buzzbar": {"1x": 35, "50x": 650},
    "8-strain Mix n Match Light dep Smalls": {"1oz": 100, "1/4LB": 350, "1/2LB": 650, "1LB": 1000, "2LB": 1800, "5LB (Free One)": 4000}
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
    keyboard.append([
        InlineKeyboardButton("👤 View My Profile", callback_data="view_profile")
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
# ===== ADMIN SHIP COMMAND =====
async def ship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    # Expect: /ship <user_id> <tracking_number>
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("❗ Usage: /ship <user_id> <tracking_number>")
        return

    target_id = int(parts[1])
    tracking_number = parts[2].strip()

    pool = await connect_db()

    # ✅ Update the most recent order for that user with this tracking number
    await pool.execute("""
        UPDATE orders
        SET return_number = $1
        WHERE user_id = $2
        ORDER BY ts DESC
        LIMIT 1
    """, tracking_number, target_id)

    # 📨 Notify the user
    try:
        await context.bot.send_message(
            target_id,
            f"📦 Your order has been shipped!\nTracking #: `{tracking_number}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not message the user: {e}")

    await update.message.reply_text(f"✅ Order updated and tracking # sent to {target_id}.")


# ===== HANDLE SELECTION =====
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

        # 🧾 Save order record and update total spent
        pool = await connect_db()
        await pool.execute(
            "INSERT INTO orders (id, user_id, total, return_number) VALUES ($1, $2, $3, $4)",
            order_id, user.id, total, None
        )
        await pool.execute(
            "UPDATE users SET total_spent = total_spent + $1 WHERE user_id = $2",
            total, user.id
        )

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


            # ===== PROFILE VIEW HANDLER =====
async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    pool = await connect_db()
    user_data, orders = await get_user_profile(pool, user.id)

    if not user_data:
        await query.message.reply_text("❌ Profile not found. Use /start first.")
        return

    joined_at = user_data["joined_at"].strftime("%b %d, %Y") if user_data["joined_at"] else "Unknown"
    total_spent = float(user_data["total_spent"] or 0)
    text = f"👤 *Your Profile*\n\n"
    text += f"🪪 Username: {user.username or 'N/A'}\n"
    text += f"📅 Joined: {joined_at}\n"
    text += f"💰 Total Spent: ${total_spent:.2f}\n\n"

    if not orders:
        text += "_You have no completed orders yet._"
    else:
        text += "📦 *Your Orders:*\n"
        for o in orders[:10]:  # show last 10 orders
            text += f"• #{o['id']} — ${o['total']} — Return #: {o['return_number'] or '—'}\n"

    await query.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        )
    )



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
        await update.message.reply_text("📬 Please enter your *Return #*(BTC ADDRESS) (required):", parse_mode="Markdown")
        await update.message.reply_text("📬 Please enter your *Return #* (BTC ADDRESS required):", parse_mode="Markdown")

    elif stage == "return_number":
        addr["return_number"] = text

# ===== MAIN ENTRY POINT =====
import asyncio

async def main():
    pool = await connect_db()
    await setup_tables(pool)
    print("✅ Tables are ready")
    print("✅ Bot is live and running...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("mustread", mustread))
    app.add_handler(CallbackQueryHandler(handle_selection))
    app.add_handler(CallbackQueryHandler(view_profile, pattern="^view_profile$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("💤 Bot still running...")
    await app.run_polling(close_loop=False)


if __name__ == "__main__":
    try:
        # If there's already a loop running (Render or Jupyter environment)
        loop = asyncio.get_running_loop()
        loop.create_task(main())
        loop.run_forever()
    except RuntimeError:
        # Normal case for standard environments
        asyncio.run(main())











