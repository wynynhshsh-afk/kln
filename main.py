import os
import re
import signal
import logging
import asyncio
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("forward-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x.strip()) for x in os.environ["ADMINS"].split(",") if x.strip()}
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "forwardbot")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

# Registry of currently-running route (worker) bot Applications, keyed by DB row id.
WORKER_APPS: dict[int, Application] = {}

GROUP_TYPES = ("group", "supergroup")
CHANNEL_TYPES = ("channel",)

# Each "mode" is a category of forwarding routes. Every route within a mode has its
# own dedicated bot token, one source chat, and one target chat (one bot per source).
MODES: dict[str, dict] = {
    "channel_channel": {
        "label": "فوروارد کانال به کانال",
        "short": "کانال به کانال",
        "source_types": CHANNEL_TYPES,
        "target_types": CHANNEL_TYPES,
        "source_desc": "کانال",
        "target_desc": "کانال",
    },
    "group_channel": {
        "label": "فوروارد گروه به کانال",
        "short": "گروه به کانال",
        "source_types": GROUP_TYPES,
        "target_types": CHANNEL_TYPES,
        "source_desc": "گروه/سوپرگروه",
        "target_desc": "کانال",
    },
    "channel_group": {
        "label": "فوروارد کانال به گروه",
        "short": "کانال به گروه",
        "source_types": CHANNEL_TYPES,
        "target_types": GROUP_TYPES,
        "source_desc": "کانال",
        "target_desc": "گروه/سوپرگروه",
    },
    "group_group": {
        "label": "فوروارد گروه به گروه",
        "short": "گروه به گروه",
        "source_types": GROUP_TYPES,
        "target_types": GROUP_TYPES,
        "source_desc": "گروه/سوپرگروه",
        "target_desc": "گروه/سوپرگروه",
    },
}
MODE_ORDER = ["channel_channel", "group_channel", "channel_group", "group_group"]


# ============================== Settings (singleton row: delay + pending actions) ==============================

def get_settings() -> dict:
    res = supabase.table("settings").select("*").eq("id", 1).execute()
    if res.data:
        row = res.data[0]
        row.setdefault("pending_actions", {})
        row.setdefault("delay_seconds", 6)
        return row
    supabase.table("settings").insert(
        {"id": 1, "delay_seconds": 6, "pending_actions": {}}
    ).execute()
    return {"id": 1, "delay_seconds": 6, "pending_actions": {}}


def update_settings(**kwargs) -> None:
    supabase.table("settings").update(kwargs).eq("id", 1).execute()


# --- Pending action state (persisted in DB, survives restarts/deploys) ---

def get_pending_action(user_id: int) -> str | None:
    settings = get_settings()
    pending = settings.get("pending_actions") or {}
    return pending.get(str(user_id))


def set_pending_action(user_id: int, action: str) -> None:
    settings = get_settings()
    pending = dict(settings.get("pending_actions") or {})
    pending[str(user_id)] = action
    update_settings(pending_actions=pending)


def clear_pending_action(user_id: int) -> None:
    settings = get_settings()
    pending = dict(settings.get("pending_actions") or {})
    if pending.pop(str(user_id), None) is not None:
        update_settings(pending_actions=pending)


# ============================== Routes table ==============================

def list_routes(mode: str | None = None) -> list[dict]:
    q = supabase.table("routes").select("*")
    if mode:
        q = q.eq("mode", mode)
    res = q.order("id").execute()
    return res.data or []


def get_route(route_id: int) -> dict | None:
    res = supabase.table("routes").select("*").eq("id", route_id).execute()
    return res.data[0] if res.data else None


def insert_route(mode: str, token: str, label: str) -> dict:
    res = supabase.table("routes").insert(
        {
            "mode": mode,
            "token": token,
            "label": label,
            "source_chat_id": None,
            "source_chat_type": None,
            "target_chat_id": None,
            "target_chat_type": None,
            "is_active": False,
        }
    ).execute()
    return res.data[0]


def update_route(route_id: int, **kwargs) -> None:
    supabase.table("routes").update(kwargs).eq("id", route_id).execute()


def delete_route_row(route_id: int) -> None:
    supabase.table("routes").delete().eq("id", route_id).execute()


# ============================== Chat parsing helpers ==============================

def normalize_username(text: str) -> str:
    t = text.strip()
    for prefix in (
        "https://telegram.me/", "https://t.me/", "http://t.me/",
        "http://telegram.me/", "telegram.me/", "t.me/",
    ):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
            break
    t = t.lstrip("@").split("/")[0].split("?")[0].strip()
    return f"@{t}" if t else ""


def parse_chat_ref(text: str) -> str | int | None:
    t = text.strip()
    if t.lstrip("-").isdigit():
        return int(t)
    username = normalize_username(t)
    if not username or username == "@":
        return None
    return username


def is_invite_link(text: str) -> bool:
    t = text.strip().lower()
    for prefix in (
        "https://telegram.me/", "https://t.me/", "http://t.me/",
        "http://telegram.me/", "telegram.me/", "t.me/",
    ):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return t.startswith("+") or t.startswith("joinchat/")


def chat_from_forward(update: Update):
    msg = update.message
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is not None:
            return chat
    forward_from_chat = getattr(msg, "forward_from_chat", None)
    if forward_from_chat:
        return forward_from_chat
    return None


async def resolve_chat_from_message(bot, update: Update):
    """Resolve a Chat from the admin's message: forward, numeric id, or link/username.
    Returns (chat, error_text); chat is None when error_text is set."""
    msg = update.message
    chat = chat_from_forward(update)
    if chat is not None:
        return chat, None

    text = (msg.text or "").strip()

    if is_invite_link(text):
        return None, (
            "❌ لینک‌های دعوت خصوصی (t.me/+...) به‌تنهایی برای ربات قابل‌استفاده نیستند.\n\n"
            "به‌جایش یکی از این دو راه را انجام بده:\n"
            "۱️⃣ یک پیام از همان گروه/کانال را همینجا فوروارد کن (ربات خودش تشخیص می‌دهد)\n"
            "۲️⃣ ربات را در آن چت عضو/ادمین کن و آیدی عددی چت (مثل -1001234567890) را بفرست"
        )

    ref = parse_chat_ref(text)
    if ref is None:
        return None, (
            "❌ ورودی نامعتبر است.\n\n"
            "یکی از این‌ها را بفرست:\n"
            "- فوروارد یک پیام از گروه/کانال موردنظر\n"
            "- آیدی عددی (مثلا -1001234567890)\n"
            "- یوزرنیم یا لینک عمومی (مثلا @name یا https://t.me/name)"
        )

    try:
        chat = await bot.get_chat(ref)
    except TelegramError as e:
        log.warning("get_chat failed for %r: %s", ref, e)
        return None, (
            "❌ چت پیدا نشد.\n\n"
            "• یوزرنیم/آیدی را چک کن\n"
            "• مطمئن شو ربات عضو آن است\n"
            "• یا به‌جای تایپ آیدی، یک پیام از آن چت را همینجا فوروارد کن"
        )
    return chat, None


async def check_bot_membership(bot, chat, require_admin: bool) -> str | None:
    """Returns an error message if the bot isn't (properly) a member of chat, else None."""
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError:
        return (
            "❌ ربات عضو این چت نیست!\n\n"
            "۱. ربات را به این گروه/کانال اضافه کن"
            + (" و ادمینش کن (دسترسی Post Messages)" if chat.type == "channel" else "")
            + "\n۲. دوباره همین شناسه را بفرست"
        )
    if require_admin and member.status not in ("administrator", "creator"):
        return (
            "❌ ربات عضو این چت هست ولی ادمین نیست!\n\n"
            "۱. ربات را در تنظیمات ادمین کن (حداقل دسترسی Post Messages)\n"
            "۲. دوباره همین شناسه را بفرست"
        )
    return None


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMINS


# ============================== Main bot: menus ==============================

def main_menu_kb(settings: dict) -> InlineKeyboardMarkup:
    delay = settings.get("delay_seconds")
    rows = []
    for mode_key in MODE_ORDER:
        mode = MODES[mode_key]
        routes = list_routes(mode_key)
        active = sum(1 for r in routes if r.get("is_active"))
        rows.append([InlineKeyboardButton(
            f"📡 {mode['label']} ({active}/{len(routes)} فعال)",
            callback_data=f"mode:{mode_key}",
            style="primary")])
    rows.append([InlineKeyboardButton(f"⏱ تاخیر ارسال: {delay} ثانیه", callback_data="set_delay",
                                       style="primary")])
    rows.append([InlineKeyboardButton("ℹ️ وضعیت کلی", callback_data="status", style="primary")])
    return InlineKeyboardMarkup(rows)


async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, edit_message=None):
    settings = get_settings()
    text = (
        "🤖 پنل مدیریت ربات‌های فوروارد\n\n"
        "سه نوع فوروارد داری؛ هرکدوم رو باز کن و «منبع» جدید اضافه کن.\n"
        "هر منبع (source) یک ربات و توکن مخصوص خودش می‌خواد؛ یعنی اگر ۵ تا منبع داری، "
        "۵ تا توکن ربات جدا از @BotFather لازم داری — هرکدوم یک مبدأ و یک مقصد."
    )
    kb = main_menu_kb(settings)
    if edit_message is not None:
        await edit_message.edit_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


def route_status_label(row: dict) -> str:
    if not row.get("source_chat_id"):
        return "⏳ منتظر تنظیم مبدأ"
    if not row.get("target_chat_id"):
        return "⏳ منتظر تنظیم مقصد"
    return "🟢 فعال" if row.get("is_active") else "🔴 غیرفعال"


async def show_mode_menu(query, context: ContextTypes.DEFAULT_TYPE, mode_key: str):
    mode = MODES[mode_key]
    routes = list_routes(mode_key)
    lines = [f"📡 {mode['label']}\n", f"مبدأ: {mode['source_desc']} → مقصد: {mode['target_desc']}\n"]
    kb_rows = []

    if not routes:
        lines.append("هنوز هیچ منبعی اضافه نشده.")

    for row in routes:
        label = row.get("label") or f"route#{row['id']}"
        lines.append(f"• {label} — {route_status_label(row)}")

        btn_row = []
        if row.get("target_chat_id"):
            if row.get("is_active"):
                btn_row.append(InlineKeyboardButton(
                    "⏸ غیرفعال کن", callback_data=f"route_toggle:{row['id']}", style="danger"))
            else:
                btn_row.append(InlineKeyboardButton(
                    "▶️ فعال کن", callback_data=f"route_toggle:{row['id']}", style="success"))
        btn_row.append(InlineKeyboardButton(
            "🗑 حذف", callback_data=f"route_del:{row['id']}", style="danger"))
        kb_rows.append(btn_row)

    kb_rows.append([InlineKeyboardButton(
        "➕ افزودن منبع جدید (توکن ربات جدید)", callback_data=f"mode_add:{mode_key}",
        style="success")])
    kb_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back", style="danger")])

    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))


# ============================== Main bot: command / callback handlers ==============================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ شما دسترسی استفاده از این ربات را ندارید.")
        return
    await send_main_menu(update.effective_chat.id, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return

    data = query.data

    if data == "status":
        s = get_settings()
        lines = ["📊 وضعیت فعلی:\n", f"تاخیر ارسال: {s.get('delay_seconds')} ثانیه\n"]
        for mode_key in MODE_ORDER:
            mode = MODES[mode_key]
            routes = list_routes(mode_key)
            active = sum(1 for r in routes if r.get("is_active"))
            lines.append(f"{mode['short']}: {active} فعال از {len(routes)}")
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data="back", style="danger")]]
        )
        await query.edit_message_text("\n".join(lines), reply_markup=kb)
        return

    if data == "back":
        await send_main_menu(query.message.chat_id, context, edit_message=query.message)
        return

    if data == "set_delay":
        set_pending_action(query.from_user.id, "delay")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data="back", style="danger")]])
        await query.edit_message_text(
            "تعداد ثانیه تاخیر قبل از ارسال به مقصد را به‌صورت عدد ارسال کنید (مثلا 6).\n"
            "این تاخیر بین همه‌ی منابع/ربات‌ها مشترک است.",
            reply_markup=kb,
        )
        return

    if data.startswith("mode:"):
        mode_key = data.split(":", 1)[1]
        if mode_key not in MODES:
            return
        await show_mode_menu(query, context, mode_key)
        return

    if data.startswith("mode_add:"):
        mode_key = data.split(":", 1)[1]
        if mode_key not in MODES:
            return
        mode = MODES[mode_key]
        set_pending_action(query.from_user.id, f"route_token:{mode_key}")
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 انصراف", callback_data=f"mode:{mode_key}", style="danger")]]
        )
        await query.edit_message_text(
            f"➕ افزودن منبع جدید برای «{mode['label']}»\n\n"
            "توکن یک ربات جدید (از @BotFather) رو بفرست. این ربات فقط برای همین یک منبع "
            f"استفاده میشه: یک {mode['source_desc']} مبدأ → یک {mode['target_desc']} مقصد.\n\n"
            "چیزی شبیه:\n123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            reply_markup=kb,
        )
        return

    if data.startswith("route_toggle:"):
        route_id = int(data.split(":", 1)[1])
        row = get_route(route_id)
        if row is None:
            await query.answer("این منبع دیگه وجود نداره.", show_alert=True)
            return
        if row.get("is_active"):
            await stop_worker_bot(route_id)
            update_route(route_id, is_active=False)
        else:
            if not row.get("target_chat_id"):
                await query.answer("اول باید مبدأ و مقصد این منبع تنظیم بشه.", show_alert=True)
                return
            started = await start_worker_bot(row)
            if started:
                update_route(route_id, is_active=True)
            else:
                await query.answer("روشن کردن ربات با خطا مواجه شد (لاگ‌ها را ببین).", show_alert=True)
        await show_mode_menu(query, context, row["mode"])
        return

    if data.startswith("route_del:"):
        route_id = int(data.split(":", 1)[1])
        row = get_route(route_id)
        if row is None:
            return
        await stop_worker_bot(route_id)
        delete_route_row(route_id)
        await show_mode_menu(query, context, row["mode"])
        return


# ============================== Main bot: private chat flows ==============================

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    action = get_pending_action(user_id)
    if not action:
        return

    msg = update.message

    if action == "delay":
        if msg.text and msg.text.strip().isdigit():
            update_settings(delay_seconds=int(msg.text.strip()))
            clear_pending_action(user_id)
            await msg.reply_text("✅ تاخیر ذخیره شد.")
            await send_main_menu(update.effective_chat.id, context)
        else:
            await msg.reply_text("لطفا فقط یک عدد ارسال کنید.")
        return

    if action.startswith("route_token:"):
        mode_key = action.split(":", 1)[1]
        await handle_route_token(update, context, mode_key)
        return

    if action.startswith("route_source:"):
        route_id = int(action.split(":", 1)[1])
        await handle_route_source(update, context, route_id)
        return

    if action.startswith("route_target:"):
        route_id = int(action.split(":", 1)[1])
        await handle_route_target(update, context, route_id)
        return


async def handle_route_token(update: Update, context: ContextTypes.DEFAULT_TYPE, mode_key: str):
    user_id = update.effective_user.id
    msg = update.message
    text = (msg.text or "").strip()
    mode = MODES[mode_key]

    if not BOT_TOKEN_RE.match(text):
        await msg.reply_text(
            "❌ این یک توکن معتبر ربات تلگرام به نظر نمی‌رسه.\n\n"
            "توکن رو از @BotFather بگیر (چیزی شبیه "
            "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx) و بفرست."
        )
        return

    for row in list_routes():
        if row.get("token") == text:
            await msg.reply_text("❌ این توکن قبلاً برای یک منبع دیگه ثبت شده.")
            return
    if text == BOT_TOKEN:
        await msg.reply_text("❌ این توکن، توکن همین ربات مدیریتیه. یک توکن دیگه بفرست.")
        return

    try:
        async with Bot(token=text) as tmp_bot:
            username = tmp_bot.username
    except TelegramError as e:
        log.warning("Invalid bot token provided: %s", e)
        await msg.reply_text(f"❌ توکن نامعتبره یا ربات در دسترس نیست:\n{e}")
        return

    try:
        row = insert_route(mode=mode_key, token=text, label=f"@{username}" if username else "bot")
    except Exception:
        log.exception("Failed to save new route")
        await msg.reply_text("خطا در ذخیره‌سازی منبع جدید در دیتابیس.")
        return

    set_pending_action(user_id, f"route_source:{row['id']}")
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 انصراف", callback_data=f"mode:{mode_key}", style="danger")]]
    )
    await msg.reply_text(
        f"✅ ربات @{username} شناسایی شد.\n\n"
        f"حالا چت مبدأ ({mode['source_desc']}) رو مشخص کن:\n"
        "• یک پیام از آن چت را برای من فوروارد کن\n"
        "• یا آیدی عددی آن را بفرست\n"
        "• یا لینک/یوزرنیم آن را بفرست\n\n"
        f"⚠️ توجه: باید ربات @{username} رو خودت داخل آن چت عضو/ادمین کنی. "
        + ("چون مبدأ کانال است، ربات باید در آن کانال ادمین باشد (وگرنه پیامی نمی‌بیند)."
           if mode["source_types"] == CHANNEL_TYPES else
           "چون مبدأ گروه است، عضویت کافیه ولی Privacy Mode ربات را در @BotFather خاموش کن."),
        reply_markup=kb,
    )


async def handle_route_source(update: Update, context: ContextTypes.DEFAULT_TYPE, route_id: int):
    user_id = update.effective_user.id
    msg = update.message

    row = get_route(route_id)
    if row is None:
        clear_pending_action(user_id)
        await msg.reply_text("❌ این منبع دیگه وجود نداره (شاید حذف شده). دوباره از منو شروع کن.")
        return

    mode = MODES[row["mode"]]

    try:
        async with Bot(token=row["token"]) as tmp_bot:
            chat, err = await resolve_chat_from_message(tmp_bot, update)
            if err:
                await msg.reply_text(err)
                return
            if chat.type not in mode["source_types"]:
                await msg.reply_text(
                    f"❌ چت مبدأ برای «{mode['label']}» باید {mode['source_desc']} باشد؛ "
                    f"چیزی که فرستادی از نوع {chat.type} است."
                )
                return
            mem_err = await check_bot_membership(tmp_bot, chat, require_admin=(chat.type == "channel"))
            if mem_err:
                await msg.reply_text(mem_err)
                return
            chat_id = chat.id
            chat_title = chat.title
            chat_type = chat.type
    except TelegramError as e:
        log.warning("Error while setting source for route %s: %s", route_id, e)
        await msg.reply_text(f"❌ خطا در ارتباط با تلگرام:\n{e}")
        return

    try:
        update_route(route_id, source_chat_id=chat_id, source_chat_type=chat_type)
    except Exception:
        log.exception("Failed to save route source")
        await msg.reply_text("خطا در ذخیره‌سازی در دیتابیس. دوباره تلاش کن.")
        return

    set_pending_action(user_id, f"route_target:{route_id}")
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 انصراف", callback_data=f"mode:{row['mode']}", style="danger")]]
    )
    await msg.reply_text(
        f"✅ مبدأ «{chat_title or chat_id}» ثبت شد.\n\n"
        f"حالا چت مقصد ({mode['target_desc']}) رو مشخص کن:\n"
        "• یک پیام از آن چت را برای من فوروارد کن\n"
        "• یا آیدی عددی آن را بفرست\n"
        "• یا لینک/یوزرنیم آن را بفرست\n\n"
        f"⚠️ باید همین ربات ({row.get('label')}) رو داخل چت مقصد ادمین کنی "
        "(برای کانال: دسترسی Post Messages، برای گروه: اجازه ارسال پیام).",
        reply_markup=kb,
    )


async def handle_route_target(update: Update, context: ContextTypes.DEFAULT_TYPE, route_id: int):
    user_id = update.effective_user.id
    msg = update.message

    row = get_route(route_id)
    if row is None:
        clear_pending_action(user_id)
        await msg.reply_text("❌ این منبع دیگه وجود نداره (شاید حذف شده). دوباره از منو شروع کن.")
        return

    mode = MODES[row["mode"]]

    try:
        async with Bot(token=row["token"]) as tmp_bot:
            chat, err = await resolve_chat_from_message(tmp_bot, update)
            if err:
                await msg.reply_text(err)
                return
            if chat.type not in mode["target_types"]:
                await msg.reply_text(
                    f"❌ چت مقصد برای «{mode['label']}» باید {mode['target_desc']} باشد؛ "
                    f"چیزی که فرستادی از نوع {chat.type} است."
                )
                return
            mem_err = await check_bot_membership(tmp_bot, chat, require_admin=(chat.type == "channel"))
            if mem_err:
                await msg.reply_text(mem_err)
                return
            chat_id = chat.id
            chat_title = chat.title
            chat_type = chat.type
    except TelegramError as e:
        log.warning("Error while setting target for route %s: %s", route_id, e)
        await msg.reply_text(f"❌ خطا در ارتباط با تلگرام:\n{e}")
        return

    try:
        update_route(route_id, target_chat_id=chat_id, target_chat_type=chat_type, is_active=True)
    except Exception:
        log.exception("Failed to save route target")
        await msg.reply_text("خطا در ذخیره‌سازی در دیتابیس. دوباره تلاش کن.")
        return

    clear_pending_action(user_id)
    row["target_chat_id"] = chat_id
    row["target_chat_type"] = chat_type
    row["is_active"] = True
    started = await start_worker_bot(row)
    if started:
        await msg.reply_text(f"✅ مقصد «{chat_title or chat_id}» ثبت شد و منبع فعال شد.")
    else:
        await msg.reply_text(
            f"✅ مقصد «{chat_title or chat_id}» ثبت شد ولی روشن کردن ربات با خطا مواجه شد.\n"
            "از منوی همین دسته دوباره فعالش کن."
        )
    await send_main_menu(update.effective_chat.id, context)


# ============================== Relay logic (shared by every route bot) ==============================

async def relay_after_delay(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int,
                             message_id: int, target_chat_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await context.bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
    except BadRequest as e:
        text = str(e).lower()
        if "not found" in text and "forward" in text:
            log.info("Skipping message %s (deleted before delay elapsed): %s", message_id, e)
            return
        log.warning("Failed to relay message %s: %s", message_id, e)
        await notify_admins(context, f"⚠️ فوروارد پیام {message_id} با خطا مواجه شد:\n{e}")
    except TelegramError as e:
        log.warning("Failed to relay message %s: %s", message_id, e)
        await notify_admins(context, f"⚠️ فوروارد پیام {message_id} با خطا مواجه شد:\n{e}")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError:
            pass


async def route_relay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A route bot's dedicated source -> target relay (one source per bot)."""
    msg = update.effective_message
    if msg is None:
        return

    source_chat_id = context.bot_data.get("source_chat_id")
    target_chat_id = context.bot_data.get("target_chat_id")
    if not source_chat_id or not target_chat_id:
        return
    if update.effective_chat.id != source_chat_id:
        return

    settings = get_settings()
    delay = settings.get("delay_seconds") or 6

    dedupe_key = (update.effective_chat.id, msg.message_id)
    sched = context.bot_data.setdefault("_scheduled", set())
    if dedupe_key in sched:
        return
    if len(sched) >= 2000:
        sched.clear()
    sched.add(dedupe_key)

    context.application.create_task(
        relay_after_delay(context, source_chat_id, msg.message_id, target_chat_id, delay)
    )


async def worker_private_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "این ربات فقط برای فوروارد پیام از چت مبدأ به چت مقصد استفاده می‌شود.\n"
        "مدیریتش از طریق ربات اصلی انجام می‌شود."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception while processing update: %s", update, exc_info=context.error)

    # Surface the failure to the admin instead of failing silently.
    try:
        chat_id = None
        if isinstance(update, Update) and update.effective_chat is not None:
            if update.effective_chat.type == "private" and is_admin(update.effective_user.id if update.effective_user else None):
                chat_id = update.effective_chat.id
        if chat_id is not None:
            err_text = f"{type(context.error).__name__}: {context.error}"
            await context.bot.send_message(
                chat_id,
                f"⚠️ خطای داخلی رخ داد و پردازش نشد:\n`{err_text}`\n\nلاگ کامل در Render ثبت شد.",
                parse_mode="Markdown",
            )
    except Exception:
        log.exception("Failed to notify admin about the error")


# ============================== Application builders ==============================

def build_app() -> Application:
    """The management/control-panel bot. It does no forwarding itself — every
    route below has its own dedicated bot for that."""
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & ~filters.COMMAND,
            private_message_handler,
        )
    )

    return app


async def start_worker_bot(row: dict) -> bool:
    """Build, initialize and start polling for a route's dedicated bot. Idempotent."""
    route_id = row["id"]
    if route_id in WORKER_APPS:
        return True

    source_chat_id = row.get("source_chat_id")
    source_chat_type = row.get("source_chat_type")
    target_chat_id = row.get("target_chat_id")
    if not source_chat_id or not target_chat_id:
        return False

    try:
        worker_app = ApplicationBuilder().token(row["token"]).build()
    except Exception:
        log.exception("Failed to build worker application for route %s", route_id)
        return False

    worker_app.bot_data["source_chat_id"] = source_chat_id
    worker_app.bot_data["target_chat_id"] = target_chat_id
    worker_app.add_error_handler(error_handler)

    # Channel posts arrive as a different update type than group messages.
    if source_chat_type == "channel":
        relay_filter = filters.ChatType.CHANNEL & filters.UpdateType.CHANNEL_POST
    else:
        relay_filter = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL

    worker_app.add_handler(MessageHandler(relay_filter, route_relay_handler))
    worker_app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, worker_private_handler)
    )

    try:
        await worker_app.initialize()
        await worker_app.start()
        await worker_app.updater.start_polling(drop_pending_updates=True)
    except Exception:
        log.exception("Failed to start worker bot for route %s", route_id)
        try:
            await worker_app.shutdown()
        except Exception:
            pass
        return False

    WORKER_APPS[route_id] = worker_app
    log.info("Route bot %s started (source=%s -> target=%s).", route_id, source_chat_id, target_chat_id)
    return True


async def stop_worker_bot(route_id: int) -> None:
    app = WORKER_APPS.pop(route_id, None)
    if app is None:
        return
    try:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("Error stopping worker bot for route %s", route_id)
    log.info("Route bot %s stopped.", route_id)


# ============================== Entry point ==============================

async def run():
    main_app = build_app()

    await main_app.initialize()
    await main_app.start()
    await main_app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_SECRET,
        webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET,
    )
    log.info("Main bot webhook started on port %s.", PORT)

    for row in list_routes():
        if row.get("is_active") and row.get("target_chat_id"):
            await start_worker_bot(row)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()

    log.info("Shutting down...")
    for route_id in list(WORKER_APPS):
        await stop_worker_bot(route_id)
    await main_app.updater.stop()
    await main_app.stop()
    await main_app.shutdown()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
