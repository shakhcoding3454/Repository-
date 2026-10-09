import asyncio
import html
import os
import re
import random
import logging
import uuid
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram import BaseMiddleware
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import ErrorEvent

# ==========================================
# ⚙️ SOZLAMALAR (O'zingiznikini kiriting)
# ==========================================
def load_env_file():
    env_path = Path(__file__).resolve().with_name(".env")
    if not env_path.is_file():
        return

    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file()


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set.")
    return value


BOT_TOKEN = required_env("BOT_TOKEN")
ADMIN_ID = int(required_env("ADMIN_ID"))
API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = None

# ==========================================
# ==========================================
# 🗄 MA'LUMOTLAR BAZASI
# ==========================================
@asynccontextmanager
async def database():
    async with aiosqlite.connect("bot_data.db", timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("PRAGMA synchronous=NORMAL")
        yield db


async def init_db():
    async with database() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_blocked INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT,
            is_sold INTEGER DEFAULT 0,
            owner_id INTEGER DEFAULT NULL,
            purchased_at TIMESTAMP DEFAULT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            unique_amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS manual_payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, amount, status)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS admin_balance_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_token TEXT NOT NULL UNIQUE,
            admin_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        
        default_settings = [('price', '25000'), ('card_number', '9860196619398957'), ('card_owner', 'B.SH')]
        for key, val in default_settings:
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
        await db.execute(
            "UPDATE transactions SET status='cancelled' "
            "WHERE status='pending' AND datetime(created_at) <= datetime('now', '-5 minutes')"
        )
        await db.commit()

async def get_setting(key):
    async with database() as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else None

# ==========================================
# 🔘 TUGMALAR VA STATE'LAR
# ==========================================
class UserStates(StatesGroup):
    waiting_for_topup_amount = State()
    waiting_for_purchase_quantity = State()

class AdminStates(StatesGroup):
    waiting_for_new_link = State()
    waiting_for_sales_user_id = State()
    waiting_for_balance_user_id = State()
    waiting_for_balance_amount = State()
    waiting_for_broadcast = State()
    waiting_for_price = State()
    waiting_for_card_number = State()
    waiting_for_card_owner = State()
    waiting_for_phone = State()
    waiting_for_auth_code = State()
    waiting_for_2fa = State()


class BlockedUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id == ADMIN_ID:
            return await handler(event, data)
        if isinstance(event, types.Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)

        async with database() as db:
            async with db.execute(
                "SELECT is_blocked FROM users WHERE user_id=?",
                (user.id,),
            ) as cur:
                row = await cur.fetchone()
        if not row or not row[0]:
            return await handler(event, data)

        if isinstance(event, types.CallbackQuery):
            await event.answer("🚫 Siz botdan bloklangansiz.", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("🚫 Siz botdan bloklangansiz.")
        return None


dp.message.outer_middleware(BlockedUserMiddleware())
dp.callback_query.outer_middleware(BlockedUserMiddleware())


@dp.errors()
async def global_error_handler(event: ErrorEvent):
    error = event.exception
    logging.error(
        "Unhandled Telegram update error",
        exc_info=(type(error), error, error.__traceback__),
    )
    callback = event.update.callback_query
    if callback:
        try:
            await callback.answer(
                "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
                show_alert=True,
            )
        except TelegramAPIError:
            logging.info("Callback could not be answered; it may have expired.")
    return True


def inline_button(text, callback_data, style=ButtonStyle.PRIMARY):
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)

async def edit_text_if_changed(message: types.Message, text: str, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

def home_keyboard(is_admin=False):
    keyboard = [
        [inline_button("🛒 Sotib olish", "buy_links", ButtonStyle.SUCCESS)],
        [inline_button("💰 Balans", "show_balance"), inline_button("👤 Profil", "show_profile")],
        [InlineKeyboardButton(
            text="🆘 Yordam",
            url="https://t.me/bshaxriyor",
            style=ButtonStyle.PRIMARY,
        )],
    ]
    if is_admin:
        keyboard.append([inline_button("🛠 Admin panel", "adm_panel", ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def product_text(price):
    return (
        "🚀 <b>Google AI Pro — 18 oylik to'liq obuna</b>\n"
        "💸 <s>199.99$ (~2 500 000 so'm)</s> → <b>atigi "
        f"{price:,}".replace(",", " ")
        + " so'm</b>\n\n"
        "⏰ <b>Aksiya havolalar tugaguncha amal qiladi!</b>\n\n"
        "Dunyodagi eng kuchli sun'iy intellekt to'plami — Gemini Pro, Veo, "
        "NotebookLM va boshqa Google AI vositalaridan <b>1,5 yil</b> foydalaning. "
        "Bir martalik to'lov — obuna davomida oylik to'lov yo'q.\n\n"
        "💰 <b>Foydali taklif:</b>\n"
        "• Rasmiy narxi: <b>199.99$</b> (~2 500 000 so'm)\n"
        "• Sizning narxingiz: <b>"
        f"{price:,}".replace(",", " ")
        + " so'm</b> — <b>98% chegirma</b>\n\n"
        "🎁 <b>Obunaga nimalar kiradi:</b>\n\n"
        "<b>☁️ 5 TB bulutli xotira</b>\n"
        "Gmail, Google Photos va Drive uchun jami 5 TB joy. "
        "Xotirani yana 5 kishi bilan ulashish mumkin.\n\n"
        "<b>✨ Gemini AI imkoniyatlari</b>\n"
        "• Nano Banana Pro — professional rasm yaratish va tahrirlash\n"
        "• Veo — suratlaringizni videoga aylantirish\n"
        "• Matn va rasmlardan 30 soniyalik saundtreklar\n"
        "• Gmail, Docs va boshqa Google ilovalarida AI yordamchi\n"
        "• Gemini ilovasida 4 barobar ko'proq limit va Deep Research\n\n"
        "<b>🧩 Qo'shimcha vositalar</b>\n"
        "• NotebookLM — audio/video xulosalar va viktorinalar\n"
        "• Google Flow — AI yordamida kinematografik videolar\n"
        "• Gemini Code Assist va CLI — dasturchilar uchun\n\n"
        "⚡️ <b>Havolalar soni cheklangan.</b> Bugun xarid qiling 👇"
    )

def purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [inline_button(str(count), f"buy_qty:{count}", ButtonStyle.SUCCESS) for count in range(1, 6)],
        [inline_button(str(count), f"buy_qty:{count}", ButtonStyle.SUCCESS) for count in (10, 20, 40)],
        [inline_button("✍️ Boshqa qiymat", "buy_custom", ButtonStyle.PRIMARY)],
        [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
    ])

async def purchase_text(user_id):
    async with database() as db:
        async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=0") as cur:
            available = (await cur.fetchone())[0]
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            balance = row[0] if row else 0
    price = int(await get_setting("price"))
    text = (
        "🛒 <b>Google AI Pro havolalari</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ Amal qilish muddati: <b>18 oy</b>\n\n"
        "Obuna tarkibi:\n"
        "• ☁️ Gmail, Photos va Drive uchun <b>5 TB</b> xotira (5 kishi bilan ulashish mumkin)\n"
        "• ✨ Gemini Pro va Nano Banana Pro — rasm yaratish/tahrirlash\n"
        "• 🎬 Veo — suratlarni videoga aylantirish, 30 soniyalik saundtreklar\n"
        "• 📄 Gmail va Docs'da Gemini yordamchisi, 4x ko'proq limit\n"
        "• 🔎 Deep Research · 📚 NotebookLM · 🎞 Google Flow\n"
        "• 💻 Gemini Code Assist va CLI\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Mavjud: <b>{available}</b> ta\n"
        f"💎 Balans: <b>{balance:,}".replace(",", " ")
        + " so'm</b>\n"
        f"💲 Narxi: <b>{price:,}".replace(",", " ")
        + " so'm</b> / dona\n\nMiqdorni tanlang:"
    )
    return text

# ==========================================
# 👤 USER QISMI: START, PROFIL VA HAVOLALAR
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    async with database() as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        async with db.execute("SELECT is_blocked FROM users WHERE user_id=?", (message.from_user.id,)) as cursor:
            user = await cursor.fetchone()
            if user and user[0] == 1:
                return await message.answer(
                    "🚫 Siz botdan bloklangansiz.",
                    reply_markup=ReplyKeyboardRemove(),
                )
        await db.commit()
    
    price = int(await get_setting("price"))
    await message.answer(
        product_text(price),
        reply_markup=home_keyboard(message.from_user.id == ADMIN_ID),
        parse_mode="HTML",
    )

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "🛠 <b>Admin boshqaruv paneli</b>\nKerakli bo'limni tanlang:",
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            product_text(int(await get_setting("price"))),
            reply_markup=home_keyboard(message.from_user.id == ADMIN_ID),
            parse_mode="HTML",
        )

async def profile_menu(message: types.Message, user_id: int, edit=False):
    async with database() as db:
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            balance = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM links WHERE owner_id=?", (user_id,)) as cur:
            links_count = (await cur.fetchone())[0]
            
    text = (
        "👤 <b>Sizning profilingiz</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💎 Joriy balans: <b>{balance:,}".replace(",", " ")
        + " so'm</b>\n"
        f"🔗 Sotib olingan havolalar: <b>{links_count}</b> ta"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("🗂 Mening havolalarim", "my_links", ButtonStyle.PRIMARY)],
        [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
    ])
    if edit:
        await edit_text_if_changed(message, text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "show_profile")
async def show_profile_callback(call: types.CallbackQuery):
    await profile_menu(call.message, call.from_user.id, edit=True)
    await call.answer()

@dp.callback_query(F.data == "my_links")
async def show_my_links(call: types.CallbackQuery):
    async with database() as db:
        async with db.execute(
            "SELECT link, purchased_at FROM links WHERE owner_id=? "
            "ORDER BY purchased_at DESC, id DESC LIMIT 10",
            (call.from_user.id,),
        ) as cur:
            my_links = await cur.fetchall()
    
    if not my_links:
        return await call.answer("Sizda hali sotib olingan havolalar yo'q.", show_alert=True)
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)]])
    batches = []
    text = "🗂 <b>Oxirgi xarid qilingan havolalaringiz:</b>\n\n"
    for i, row in enumerate(my_links, 1):
        entry = (
            f"{i}. <code>{html.escape(row[0] or '')}</code>\n"
            f"⏳ <i>Sana: {html.escape(str(row[1]))}</i>\n\n"
        )
        if len(text) + len(entry) > 3500 and text.strip():
            batches.append(text.rstrip())
            text = ""
        text += entry
    if text:
        batches.append(text.rstrip())
    for index, batch in enumerate(batches):
        await call.message.answer(
            batch,
            reply_markup=kb if index == len(batches) - 1 else None,
            parse_mode="HTML",
        )
    await call.answer()

async def show_purchase_menu(message: types.Message, user_id: int, edit=False):
    text = await purchase_text(user_id)
    if edit:
        await edit_text_if_changed(
            message, text, reply_markup=purchase_keyboard(), parse_mode="HTML"
        )
    else:
        await message.answer(text, reply_markup=purchase_keyboard(), parse_mode="HTML")

# ==========================================
# 🛒 SOTIB OLISH (RACE-CONDITION HIMOYA)
# ==========================================
@dp.callback_query(F.data == "buy_links")
async def buy_link_menu(call: types.CallbackQuery):
    await show_purchase_menu(call.message, call.from_user.id, edit=True)
    await call.answer()

@dp.callback_query(F.data == "buy_custom")
async def ask_purchase_quantity(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_purchase_quantity)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
    ])
    await call.message.answer(
        "✍️ Nechta havola olmoqchisiz? Butun musbat son kiriting:",
        reply_markup=keyboard,
    )
    await call.answer()

@dp.message(UserStates.waiting_for_purchase_quantity)
async def custom_purchase_quantity(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        return await message.answer("⚠️ Iltimos, faqat butun musbat son kiriting.")
    quantity = int(message.text)
    if not 1 <= quantity <= 1_000_000:
        return await message.answer("⚠️ Miqdor 1 dan 1 000 000 gacha bo'lishi kerak.")
    await state.clear()
    await buy_quantity(message, quantity)

async def buy_quantity(message, quantity: int):
    if not 1 <= quantity <= 1_000_000:
        target = message.message if isinstance(message, types.CallbackQuery) else message
        await target.answer("⚠️ Miqdor noto'g'ri. 1 dan 1 000 000 gacha kiriting.")
        return
    user_id = message.from_user.id
    price = int(await get_setting("price"))
    total_price = price * quantity

    async with database() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=0") as cur:
            available = (await cur.fetchone())[0]
        if available < quantity:
            await db.rollback()
            admin_notified = await notify_admin_stock_request(user_id, quantity, available)
            notice = (
                "😔 Hozircha kerakli miqdorda havola yo'q. Tez orada zaxira qo'shiladi; "
                "administratorga xabar berildi."
                if admin_notified
                else "😔 Hozircha kerakli miqdorda havola yo'q. Tez orada zaxira qo'shiladi, "
                     "lekin administratorga xabar yuborilmadi."
            )
            if available:
                notice += f"\n\nHozir mavjud: <b>{available}</b> ta."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("🔄 Qayta tekshirish", "buy_links", ButtonStyle.SUCCESS)],
                [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
            ])
            target = message.message if isinstance(message, types.CallbackQuery) else message
            await target.answer(notice, reply_markup=keyboard, parse_mode="HTML")
            return

        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            user = await cur.fetchone()
        balance = user[0] if user else 0

        if balance < total_price:
            await db.rollback()
            text = (
                "⚠️ <b>Balans yetarli emas</b>\n\n"
                f"Buyurtma: <b>{quantity}</b> ta — <b>{total_price:,}".replace(",", " ")
                + " so'm</b>\n"
                f"Balansingiz: <b>{balance:,}".replace(",", " ")
                + " so'm</b>\n"
                f"Yetishmayapti: <b>{total_price - balance:,}".replace(",", " ")
                + " so'm</b>\n\nBalansni to'ldirib, qayta urinib ko'ring."
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("💳 Balansni to'ldirish", "show_balance", ButtonStyle.SUCCESS)],
                [inline_button("⬅️ Xaridga qaytish", "buy_links", ButtonStyle.DANGER)],
            ])
            if isinstance(message, types.CallbackQuery):
                await edit_text_if_changed(
                    message.message, text, reply_markup=keyboard, parse_mode="HTML"
                )
            else:
                await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            return

        async with db.execute(
            "SELECT id, link FROM links WHERE is_sold=0 ORDER BY id LIMIT ?",
            (quantity,),
        ) as cur:
            links = await cur.fetchall()
        if len(links) < quantity:
            await db.rollback()
            admin_notified = await notify_admin_stock_request(user_id, quantity, len(links))
            notice = (
                "😔 Hozircha kerakli miqdorda havola yo'q. Tez orada zaxira qo'shiladi; "
                "administratorga xabar berildi."
                if admin_notified
                else "😔 Hozircha kerakli miqdorda havola yo'q. Tez orada zaxira qo'shiladi, "
                     "lekin administratorga xabar yuborilmadi."
            )
            target = message.message if isinstance(message, types.CallbackQuery) else message
            await target.answer(
                f"{notice}\n\nHozir mavjud: <b>{len(links)}</b> ta.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [inline_button("🔄 Qayta tekshirish", "buy_links", ButtonStyle.SUCCESS)],
                    [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
                ]),
                parse_mode="HTML",
            )
            return

        link_ids = [link[0] for link in links]
        placeholders = ",".join("?" for _ in link_ids)
        await db.execute(
            f"UPDATE links SET is_sold=1, owner_id=?, purchased_at=CURRENT_TIMESTAMP "
            f"WHERE is_sold=0 AND id IN ({placeholders})",
            (user_id, *link_ids),
        )
        await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (total_price, user_id),
        )
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=0") as cur:
            available = (await cur.fetchone())[0]

    if available in [10, 5, 3, 1, 0]:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Havolalar kam qoldi!</b>\nHozir zaxirada <b>{available}</b> ta havola bor.",
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Adminga zaxira haqida xabar yuborilmadi")

    link_lines = [
        f"🔗 <b>Havola {index}</b>\n<code>{html.escape(link)}</code>"
        for index, (_, link) in enumerate(links, 1)
    ]
    link_batches = []
    current_batch = []
    batch_length = 0
    for line in link_lines:
        if current_batch and batch_length + len(line) + 2 > 2800:
            link_batches.append(current_batch)
            current_batch = []
            batch_length = 0
        current_batch.append(line)
        batch_length += len(line) + 2
    if current_batch:
        link_batches.append(current_batch)

    instructions = (
        "\n\n📋 Har bir havolani ustiga bosib nusxalashingiz mumkin.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>YO'RIQNOMA VA SHARTLAR</b>\n"
        "⚡ Havolani brauzerga joylang va “Activate Offer” tugmasini bosing.\n"
        "⚡ Havola buyurtmadan keyin <b>2 soat</b> ichida ishlatilishi kerak. "
        "Aks holda u eskirishi va obunani faollashtirib bo'lmasligi mumkin.\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "💎 Bizni tanlaganingiz uchun rahmat! Xaridingizdan rohatlaning."
    )
    success_header = (
        "✅ <b>Xarid muvaffaqiyatli!</b>\n\n"
        f"<b>Google AI Pro</b> — {quantity} ta havola:\n\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("🛒 Yana xarid qilish", "buy_links", ButtonStyle.SUCCESS)],
        [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
    ])
    if not link_batches:
        link_batches = [[]]
    send_following = (
        message.message.answer if isinstance(message, types.CallbackQuery) else message.answer
    )
    for index, batch in enumerate(link_batches):
        text = success_header if index == 0 else "🔗 <b>Havolalar (davomi)</b>\n\n"
        text += "\n\n".join(batch)
        is_last = index == len(link_batches) - 1
        if is_last:
            text += instructions
        markup = keyboard if is_last else None
        if index == 0 and isinstance(message, types.CallbackQuery):
            await edit_text_if_changed(
                message.message, text, reply_markup=markup, parse_mode="HTML"
            )
        else:
            await send_following(text, reply_markup=markup, parse_mode="HTML")

async def notify_admin_stock_request(user_id: int, requested: int, available: int) -> bool:
    try:
        await bot.send_message(
            ADMIN_ID,
            "📦 <b>Havola zaxirasi kerak</b>\n"
            f"Foydalanuvchi: <code>{user_id}</code>\n"
            f"So'ralgan: <b>{requested}</b> ta\n"
            f"Mavjud: <b>{available}</b> ta",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Havola zaxirasi so'rovi adminga yuborilmadi")
        return False
    return True

@dp.callback_query(F.data.startswith("buy_qty:"))
async def confirm_purchase(call: types.CallbackQuery):
    try:
        quantity = int(call.data.partition(":")[2])
    except ValueError:
        await call.answer("Miqdor noto'g'ri.", show_alert=True)
        return
    await buy_quantity(call, quantity)
    await call.answer()

@dp.callback_query(F.data == "home")
async def return_home(call: types.CallbackQuery):
    await edit_text_if_changed(
        call.message,
        product_text(int(await get_setting("price"))),
        reply_markup=home_keyboard(call.from_user.id == ADMIN_ID),
        parse_mode="HTML",
    )
    await call.answer()

@dp.callback_query(F.data == "cancel_flow")
async def cancel_flow(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if call.from_user.id == ADMIN_ID:
        await edit_text_if_changed(
            call.message,
            "🛠 <b>Admin boshqaruv paneli</b>\nKerakli bo'limni tanlang:",
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )
    else:
        await edit_text_if_changed(
            call.message,
            product_text(int(await get_setting("price"))),
            reply_markup=home_keyboard(call.from_user.id == ADMIN_ID),
            parse_mode="HTML",
        )
    await call.answer("Bekor qilindi.")

# ==========================================
# 💰 BALANS TO'LDIRISH
# ==========================================
@dp.callback_query(F.data == "add_funds")
async def add_funds_redirect(call: types.CallbackQuery):
    await balance_menu(call.message, call.from_user.id, edit=True)
    await call.answer()

@dp.callback_query(F.data == "show_balance")
async def show_balance_callback(call: types.CallbackQuery):
    await balance_menu(call.message, call.from_user.id, edit=True)
    await call.answer()

async def balance_menu(message: types.Message, user_id: int, edit=False):
    async with database() as db:
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = row[0] if row else 0

    text = (
        "💳 <b>Balansni to'ldirish</b>\n\n"
        f"💎 <b>Joriy balans: {balance:,}".replace(",", " ")
        + " so'm</b>\n\nQancha summa qo'shmoqchisiz?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("59 000 so'm", "pay_59000", ButtonStyle.SUCCESS), inline_button("100 000 so'm", "pay_100000", ButtonStyle.SUCCESS)],
        [inline_button("200 000 so'm", "pay_200000", ButtonStyle.SUCCESS), inline_button("500 000 so'm", "pay_500000", ButtonStyle.SUCCESS)],
        [inline_button("✍️ Boshqa summa", "pay_custom", ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text="👨‍💼 Admin orqali to'lov",
            url="https://t.me/bshaxriyor",
            style=ButtonStyle.PRIMARY,
        )],
        [inline_button("⬅️ Asosiy menyu", "home", ButtonStyle.DANGER)],
    ])
    if edit:
        await edit_text_if_changed(message, text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("pay_"))
async def process_payment_selection(call: types.CallbackQuery, state: FSMContext):
    action = call.data.split("_")[1]
    if action == "custom":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
        ])
        await call.message.answer(
            "📝 To'ldirmoqchi bo'lgan summangizni kiriting (masalan: 50000):",
            reply_markup=keyboard,
        )
        await state.set_state(UserStates.waiting_for_topup_amount)
    else:
        await generate_invoice(call.message, int(action), call.from_user.id)
    await call.answer()

@dp.message(UserStates.waiting_for_topup_amount)
async def custom_amount_handler(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) < 1000:
        return await message.answer("⚠️ Minimal to'lov summasi 1000 so'm. Faqat raqam kiriting.")
    await state.clear()
    await generate_invoice(message, int(message.text), message.from_user.id)

async def generate_invoice(message_obj: types.Message, base_amount: int, user_id: int):
    async with database() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE transactions SET status='cancelled' "
            "WHERE status='pending' AND datetime(created_at) <= datetime('now', '-5 minutes')"
        )
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT unique_amount FROM transactions WHERE status='pending' "
            "AND unique_amount>? AND unique_amount<=?",
            (base_amount, base_amount + 999),
        ) as cursor:
            used_suffixes = {
                row[0] - base_amount for row in await cursor.fetchall()
            }

        start_suffix = random.randint(1, 999)
        unique_suffix = next(
            (
                candidate
                for offset in range(999)
                if (candidate := (start_suffix - 1 + offset) % 999 + 1)
                not in used_suffixes
            ),
            None,
        )
        if unique_suffix is None:
            await db.rollback()
            return await message_obj.answer(
                "⚠️ Hozircha ushbu summa uchun bo'sh to'lov raqami yo'q. "
                "Bir necha daqiqadan so'ng qayta urinib ko'ring."
            )
        exact_amount = base_amount + unique_suffix

        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('card_number', 'card_owner')"
        ) as cursor:
            settings = dict(await cursor.fetchall())
        card_num = settings.get("card_number")
        card_owner = settings.get("card_owner")
        
        cursor = await db.execute(
            "INSERT INTO transactions (user_id, amount, unique_amount) VALUES (?, ?, ?)",
            (user_id, base_amount, exact_amount),
        )
        tx_id = cursor.lastrowid
        await db.commit()

    text = (
        "💳 <b>To'lov ma'lumotlari</b>\n\n"
        f"👤 Egasi: {html.escape(card_owner or '')}\n"
        f"💳 Karta: <code>{html.escape(card_num or '')}</code>\n\n"
        f"💰 Summa: <b>{exact_amount:,}".replace(",", " ")
        + f" so'm</b>\n📋 To'lov ID: #{tx_id}\n\n"
        "⏰ 5 daqiqada to'lov qilinmasa buyurtma bekor qilinadi.\n"
        "⚠️ Aynan ko'rsatilgan summani yuboring!"
    )
    
    # Bekor qilish tugmasi qo'shildi
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("❌ Bekor qilish", f"cancel_tx_{tx_id}", ButtonStyle.DANGER)]
    ])
    msg = await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)
    
    asyncio.create_task(cancel_invoice_timer(tx_id, msg.chat.id, msg.message_id))

@dp.callback_query(F.data.startswith("cancel_tx_"))
async def manual_cancel_tx(call: types.CallbackQuery):
    tx_id = int(call.data.split("_")[2])
    async with database() as db:
        cursor = await db.execute(
            "UPDATE transactions SET status='cancelled' "
            "WHERE id=? AND user_id=? AND status='pending'",
            (tx_id, call.from_user.id),
        )
        await db.commit()
    if cursor.rowcount:
        await call.message.edit_text("❌ To'lov siz tomoningizdan bekor qilindi.")
        await call.answer()
    else:
        await call.answer("Bu to'lovni bekor qilib bo'lmaydi.", show_alert=True)

async def cancel_invoice_timer(tx_id, chat_id, msg_id):
    await asyncio.sleep(300) # 5 minut
    async with database() as db:
        cursor = await db.execute(
            "UPDATE transactions SET status='cancelled' WHERE id=? AND status='pending'",
            (tx_id,),
        )
        await db.commit()
    if cursor.rowcount:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="⌛ To'lov muddati tugadi va avtomatik bekor qilindi.",
            )
        except TelegramBadRequest:
            pass


async def expire_pending_invoices():
    async with database() as db:
        await db.execute(
            "UPDATE transactions SET status='cancelled' "
            "WHERE status='pending' AND datetime(created_at) <= datetime('now', '-5 minutes')"
        )
        await db.commit()


async def invoice_expiry_worker():
    while True:
        await asyncio.sleep(30)
        try:
            await expire_pending_invoices()
        except aiosqlite.Error:
            logging.exception("Failed to expire old pending invoices.")

# ==========================================
# 🛠 ADMIN PANEL
# ==========================================
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    await message.answer(
        "🛠 <b>Admin boshqaruv paneli</b>\nKerakli bo'limni tanlang:",
        reply_markup=admin_keyboard(),
        parse_mode="HTML",
    )

async def pending_payments_markup():
    async with database() as db:
        async with db.execute(
            "SELECT id, user_id, amount, unique_amount FROM transactions "
            "WHERE status='pending' ORDER BY id DESC LIMIT 10"
        ) as cur:
            transactions = await cur.fetchall()
        async with db.execute(
            "SELECT id, user_id, amount FROM manual_payment_requests "
            "WHERE status='pending' ORDER BY id DESC LIMIT 10"
        ) as cur:
            manual_requests = await cur.fetchall()
    rows = [
        [inline_button(
            f"✅ Tasdiqlash #{tx_id} · {amount:,} so'm".replace(",", " "),
            f"adm_paid_{tx_id}",
            ButtonStyle.SUCCESS,
        )]
        for tx_id, _, amount, _ in transactions
    ]
    rows.extend(
        [inline_button(
            f"✅ Admin to'lovi #{request_id} · {amount:,} so'm".replace(",", " "),
            f"adm_manual_paid_{request_id}",
            ButtonStyle.SUCCESS,
        )]
        for request_id, _, amount in manual_requests
    )
    rows.append([inline_button("⬅️ Admin panel", "adm_panel", ButtonStyle.DANGER)])
    rows.insert(
        -1,
        [inline_button("➕ User balansiga pul qo'shish", "adm_balance_add", ButtonStyle.PRIMARY)],
    )
    text = "⏳ <b>Kutilayotgan to'lovlar</b>\n\n"
    if transactions:
        text += "<b>Karta to'lovlari:</b>\n"
        text += "".join(
            f"#{tx_id} · user <code>{user_id}</code> · {amount:,} so'm"
            f" (to'lovda: {unique_amount:,} so'm)\n".replace(",", " ")
            for tx_id, user_id, amount, unique_amount in transactions
        )
    if manual_requests:
        text += "\n<b>Admin orqali to'lov:</b>\n"
        text += "".join(
            f"#{request_id} · user <code>{user_id}</code> · {amount:,} so'm\n".replace(",", " ")
            for request_id, user_id, amount in manual_requests
        )
    if not transactions and not manual_requests:
        text += "Kutilayotgan to'lovlar yo'q."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

async def show_pending_payments(message: types.Message):
    text, markup = await pending_payments_markup()
    await edit_text_if_changed(message, text, reply_markup=markup, parse_mode="HTML")

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("📊 Statistika", "adm_stats", ButtonStyle.PRIMARY),
         inline_button("📦 Ombor", "adm_inventory", ButtonStyle.PRIMARY)],
        [inline_button("➕ Havola qo'shish", "adm_add_link", ButtonStyle.SUCCESS),
         inline_button("🗂 Sotuvlar", "adm_link_history", ButtonStyle.PRIMARY)],
        [inline_button("💵 Narxni o'zgartirish", "adm_price", ButtonStyle.SUCCESS),
         inline_button("💳 To'lov sozlamalari", "adm_payment_settings", ButtonStyle.PRIMARY)],
        [inline_button("⏳ Kutilayotgan to'lovlar", "adm_pending", ButtonStyle.SUCCESS),
         inline_button("➕ Balansga pul qo'shish", "adm_balance_add", ButtonStyle.PRIMARY)],
        [inline_button("👥 So'nggi foydalanuvchilar", "adm_users", ButtonStyle.PRIMARY)],
        [inline_button("📢 Xabar yuborish", "adm_broadcast", ButtonStyle.PRIMARY),
         inline_button("🔗 Userbot", "adm_userbot", ButtonStyle.PRIMARY)],
    ])

@dp.callback_query(F.data.startswith("adm_"), F.from_user.id == ADMIN_ID)
async def admin_callbacks(call: types.CallbackQuery, state: FSMContext):
    action = call.data.removeprefix("adm_")
    
    if action == "stats":
        async with database() as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur: users_count = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=0") as cur: links_avail = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=1") as cur: links_sold = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT "
                "(SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE status='paid') + "
                "(SELECT COALESCE(SUM(amount), 0) FROM manual_payment_requests WHERE status='approved')"
            ) as cur:
                total_money = (await cur.fetchone())[0]
        
        status = "🟢 Ulangan" if app and app.is_initialized else "🔴 Ulanmagan"
        text = (
            "📊 <b>Umumiy statistika</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{users_count}</b>\n"
            f"💳 Muvaffaqiyatli to'lovlar: <b>{total_money:,}".replace(",", " ")
            + " so'm</b>\n"
            f"📎 Mavjud havolalar: <b>{links_avail}</b>\n"
            f"✅ Sotilgan havolalar: <b>{links_sold}</b>\n"
            f"🤖 Userbot: {status}"
        )
        await edit_text_if_changed(
            call.message, text, reply_markup=admin_back_keyboard(), parse_mode="HTML"
        )

    elif action == "inventory":
        async with database() as db:
            async with db.execute("SELECT COUNT(*) FROM links") as cur:
                total = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=0") as cur:
                available = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM links WHERE is_sold=1") as cur:
                sold = (await cur.fetchone())[0]
        await edit_text_if_changed(
            call.message,
            f"📦 <b>Havolalar ombori</b>\n\nJami: <b>{total}</b>\n"
            f"🟢 Mavjud: <b>{available}</b>\n✅ Sotilgan: <b>{sold}</b>",
            reply_markup=admin_back_keyboard(),
            parse_mode="HTML",
        )

    elif action.startswith("users"):
        parts = action.split("_", 1)
        page = 0
        if len(parts) == 2 and parts[1].isdigit():
            page = int(parts[1])
        page_size = 10
        async with database() as db:
            async with db.execute(
                "SELECT user_id, balance, joined_at, is_blocked FROM users "
                "ORDER BY joined_at DESC, user_id DESC LIMIT ? OFFSET ?",
                (page_size, page * page_size),
            ) as cur:
                users = await cur.fetchall()
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                total_users = (await cur.fetchone())[0]

        text = f"👥 <b>So'nggi foydalanuvchilar</b> · jami: <b>{total_users}</b>\n\n"
        if users:
            for user_id, balance, joined_at, is_blocked in users:
                status = "🚫 bloklangan" if is_blocked else "🟢 faol"
                text += (
                    f"🆔 <code>{user_id}</code> · {status}\n"
                    f"💰 Balans: <b>{balance:,}".replace(",", " ")
                    + f" so'm</b>\n"
                    f"📅 Qo'shilgan: {html.escape(str(joined_at))}\n\n"
                )
        else:
            text += "Bu sahifada foydalanuvchilar yo'q."

        rows = []
        navigation = []
        if page > 0:
            navigation.append(inline_button("⬅️ Oldingi", f"adm_users_{page - 1}"))
        if (page + 1) * page_size < total_users:
            navigation.append(inline_button("Keyingi ➡️", f"adm_users_{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([inline_button("⬅️ Admin panel", "adm_panel", ButtonStyle.DANGER)])
        await edit_text_if_changed(
            call.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )

    elif action == "add_link":
        await call.message.answer("📝 Yangi havolalarni yuboring. Bir nechta havolani har birini yangi qatordan yozing.")
        await state.set_state(AdminStates.waiting_for_new_link)
        
    elif action == "link_history":
        async with database() as db:
            async with db.execute(
                "SELECT id, owner_id, link, purchased_at FROM links "
                "WHERE is_sold=1 ORDER BY purchased_at DESC, id DESC LIMIT 10"
            ) as cur:
                history = await cur.fetchall()

        batches = []
        current = "🗂 <b>Oxirgi sotilgan havolalar</b>\n\n"
        if history:
            for link_id, owner_id, link, purchased_at in history:
                entry = (
                    f"🆔 <b>Havola #{link_id}</b> · 👤 <code>{owner_id}</code>\n"
                    f"⏳ {html.escape(str(purchased_at))}\n"
                    f"<code>{html.escape(link or '')}</code>\n\n"
                )
                if len(current) + len(entry) > 3500 and current.strip():
                    batches.append(current)
                    current = ""
                current += entry
        else:
            current += "Hali sotuv bo'lmagan."
        batches.append(current.rstrip())

        sales_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [inline_button("🔎 User ID bo'yicha qidirish", "adm_sales_search", ButtonStyle.PRIMARY)],
            [inline_button("⬅️ Admin panel", "adm_panel", ButtonStyle.DANGER)],
        ])
        await edit_text_if_changed(
            call.message,
            batches[0],
            reply_markup=sales_keyboard,
            parse_mode="HTML",
        )
        for batch in batches[1:]:
            await call.message.answer(batch, parse_mode="HTML")

    elif action == "sales_search":
        await call.message.answer(
            "🔎 Sotib olgan havolalarini ko'rish uchun foydalanuvchining Telegram ID raqamini yuboring:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
            ]),
        )
        await state.set_state(AdminStates.waiting_for_sales_user_id)

    elif action == "balance_add":
        await call.message.answer(
            "👤 Balansiga pul qo'shiladigan foydalanuvchining Telegram ID raqamini yuboring:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
            ]),
        )
        await state.set_state(AdminStates.waiting_for_balance_user_id)

    elif action == "price":
        current_price = await get_setting("price")
        await call.message.answer(
            f"💵 Joriy narx: <b>{int(current_price):,}".replace(",", " ")
            + " so'm</b>\nYangi narxni so'mda yuboring:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
            ]),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_price)

    elif action in ("payment", "payment_settings"):
        card_number = html.escape(await get_setting("card_number") or "kiritilmagan")
        card_owner = html.escape(await get_setting("card_owner") or "kiritilmagan")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [inline_button("💳 Karta raqamini o'zgartirish", "adm_card_number", ButtonStyle.PRIMARY)],
            [inline_button("👤 Karta egasini o'zgartirish", "adm_card_owner", ButtonStyle.PRIMARY)],
            [inline_button("⬅️ Admin panel", "adm_panel", ButtonStyle.DANGER)],
        ])
        await edit_text_if_changed(
            call.message,
            f"💳 <b>To'lov sozlamalari</b>\n\nKarta: <code>{card_number}</code>\n"
            f"Egasi: <b>{card_owner}</b>",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    elif action in ("card_number", "card_owner"):
        field = call.data.removeprefix("adm_card_")
        if field == "number":
            prompt = "💳 Yangi karta raqamini yuboring (12–19 ta raqam):"
            await state.set_state(AdminStates.waiting_for_card_number)
        elif field == "owner":
            prompt = "👤 Karta egasining yangi ismini yuboring:"
            await state.set_state(AdminStates.waiting_for_card_owner)
        else:
            return await call.answer("Bo'lim topilmadi.", show_alert=True)
        await call.message.answer(
            prompt,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
            ]),
        )

    elif action == "pending":
        await show_pending_payments(call.message)

    elif action == "balance_confirm":
        data = await state.get_data()
        request_token = data.get("balance_adjustment_token")
        user_id = data.get("balance_adjustment_user_id")
        amount = data.get("balance_adjustment_amount")
        if not request_token or not user_id or not amount:
            await call.answer("Tasdiqlash muddati tugagan. Qaytadan boshlang.", show_alert=True)
            return

        already_applied = False
        async with database() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id FROM admin_balance_adjustments WHERE request_token=?",
                (request_token,),
            ) as cur:
                already_applied = await cur.fetchone() is not None
            if not already_applied:
                async with db.execute(
                    "SELECT 1 FROM users WHERE user_id=?",
                    (user_id,),
                ) as cur:
                    user_exists = await cur.fetchone() is not None
                if user_exists:
                    await db.execute(
                        "INSERT INTO admin_balance_adjustments "
                        "(request_token, admin_id, user_id, amount) VALUES (?, ?, ?, ?)",
                        (request_token, call.from_user.id, user_id, amount),
                    )
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (amount, user_id),
                    )
                    await db.commit()
                else:
                    await db.rollback()
                    await state.clear()
                    await call.answer("Foydalanuvchi bazada topilmadi.", show_alert=True)
                    return
            else:
                await db.rollback()

        if already_applied:
            await state.clear()
            await call.answer("Bu summa avval qo'shilgan.", show_alert=True)
            return

        await state.clear()
        try:
            await bot.send_message(
                user_id,
                "💰 <b>Balansingiz to'ldirildi.</b>\n"
                f"➕ {amount:,}".replace(",", " ")
                + " so'm balansingizga qo'shildi.",
                parse_mode="HTML",
            )
        except TelegramAPIError:
            logging.exception(
                "Admin balans qo'shdi, lekin foydalanuvchiga xabar yuborilmadi (user_id=%s)",
                user_id,
            )
        await edit_text_if_changed(
            call.message,
            "✅ <b>Balans to'ldirildi</b>\n\n"
            f"Foydalanuvchi: <code>{user_id}</code>\n"
            f"Qo'shilgan summa: <b>{amount:,}".replace(",", " ")
            + " so'm</b>",
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )
        await call.answer("Balans muvaffaqiyatli to'ldirildi.")
        return

    elif action.startswith("manual_paid_"):
        request_id = int(call.data.rsplit("_", 1)[1])
        request = None
        async with database() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT user_id, amount FROM manual_payment_requests "
                "WHERE id=? AND status='pending'",
                (request_id,),
            ) as cur:
                request = await cur.fetchone()
            if request:
                cursor = await db.execute(
                    "UPDATE manual_payment_requests SET status='approved' "
                    "WHERE id=? AND status='pending'",
                    (request_id,),
                )
                if cursor.rowcount == 1:
                    await db.execute(
                        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                        (request[0],),
                    )
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (request[1], request[0]),
                    )
                    await db.commit()
                else:
                    request = None
            if not request:
                await db.rollback()
        if request:
            await call.answer("Admin to'lovi tasdiqlandi.")
            try:
                await bot.send_message(
                    request[0],
                    "✅ <b>Admin to'lovingiz tasdiqlandi.</b>\n"
                    f"➕ {request[1]:,}".replace(",", " ")
                    + " so'm balansingizga qo'shildi.",
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                logging.exception(
                    "Admin to'lovi tasdiqlandi, lekin userga xabar yuborilmadi (user_id=%s)",
                    request[0],
                )
        else:
            await call.answer(
                "So'rov topilmadi yoki oldin tasdiqlangan.",
                show_alert=True,
            )
        await show_pending_payments(call.message)
        return

    elif action.startswith("paid_"):
        tx_id = int(call.data.rsplit("_", 1)[1])
        transaction = None
        async with database() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT user_id, amount FROM transactions WHERE id=? AND status='pending'",
                (tx_id,),
            ) as cur:
                transaction = await cur.fetchone()
            if transaction:
                cursor = await db.execute(
                    "UPDATE transactions SET status='paid' WHERE id=? AND status='pending'",
                    (tx_id,),
                )
                if cursor.rowcount != 1:
                    transaction = None
                else:
                    await db.execute(
                        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                        (transaction[0],),
                    )
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (transaction[1], transaction[0]),
                    )
                    await db.commit()
            if not transaction:
                await db.rollback()
        if transaction:
            await call.answer("To'lov tasdiqlandi.")
            try:
                await bot.send_message(
                    transaction[0],
                    "✅ To'lov tasdiqlandi!\n"
                    f"➕ {transaction[1]:,} so'm balansingizga qo'shildi.".replace(",", " "),
                )
            except TelegramAPIError:
                logging.exception(
                    "To'lov tasdiqlandi, lekin userga xabar yuborilmadi (user_id=%s)",
                    transaction[0],
                )
        else:
            await call.answer(
                "To'lov topilmadi yoki oldin tasdiqlangan.",
                show_alert=True,
            )
        await show_pending_payments(call.message)
        return

    elif action == "panel":
        await edit_text_if_changed(
            call.message,
            "🛠 <b>Admin boshqaruv paneli</b>\nKerakli bo'limni tanlang:",
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )

    elif action == "broadcast":
        await call.message.answer("📢 Barcha userlarga yuboriladigan xabarni (rasm/video/matn) yuboring:")
        await state.set_state(AdminStates.waiting_for_broadcast)

    elif action == "userbot":
        if app and app.is_initialized:
            await call.message.answer("🟢 Userbot allaqachon ulangan va ishlamoqda!")
        else:
            await call.message.answer("📱 Userbot uchun telefon raqamni kiriting (Masalan: +998901234567):")
            await state.set_state(AdminStates.waiting_for_phone)
    else:
        await call.answer("Bu admin bo'limi topilmadi.", show_alert=True)
        return

    await call.answer()

def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [inline_button("⬅️ Admin panel", "adm_panel", ButtonStyle.DANGER)],
    ])

@dp.message(AdminStates.waiting_for_sales_user_id, F.from_user.id == ADMIN_ID)
async def show_user_sold_links(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("⚠️ User ID raqamini faqat sonlarda yuboring:")
    user_id = int(message.text.strip())
    async with database() as db:
        async with db.execute(
            "SELECT id, link, purchased_at FROM links "
            "WHERE is_sold=1 AND owner_id=? ORDER BY purchased_at, id",
            (user_id,),
        ) as cur:
            links = await cur.fetchall()

    await state.clear()
    if not links:
        return await message.answer(
            f"📭 <code>{user_id}</code> ID foydalanuvchida sotib olingan havola topilmadi.",
            reply_markup=admin_back_keyboard(),
            parse_mode="HTML",
        )

    header = (
        f"🗂 <b>Foydalanuvchi <code>{user_id}</code> sotib olgan havolalar</b>\n"
        f"Jami: <b>{len(links)}</b> ta\n\n"
    )
    batches = []
    current = header
    for link_id, link, purchased_at in links:
        entry = (
            f"🔗 <b>Havola #{link_id}</b>\n"
            f"⏳ {html.escape(str(purchased_at))}\n"
            f"<code>{html.escape(link or '')}</code>\n\n"
        )
        if len(current) + len(entry) > 3500 and current.strip():
            batches.append(current.rstrip())
            current = ""
        current += entry
    if current:
        batches.append(current.rstrip())

    for index, batch in enumerate(batches):
        await message.answer(
            batch,
            reply_markup=admin_back_keyboard() if index == len(batches) - 1 else None,
            parse_mode="HTML",
        )

@dp.message(AdminStates.waiting_for_balance_user_id, F.from_user.id == ADMIN_ID)
async def set_balance_adjustment_user(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("⚠️ User ID raqamini faqat sonlarda yuboring:")
    user_id = int(message.text.strip())
    async with database() as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            exists = await cur.fetchone()
    if not exists:
        return await message.answer(
            "⚠️ Bu ID bot foydalanuvchilari orasida topilmadi. ID ni tekshirib qayta yuboring:"
        )

    await state.update_data(balance_adjustment_user_id=user_id)
    await state.set_state(AdminStates.waiting_for_balance_amount)
    await message.answer(
        f"💰 <code>{user_id}</code> foydalanuvchisiga qo'shiladigan summani so'mda yuboring:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)]
        ]),
        parse_mode="HTML",
    )

@dp.message(AdminStates.waiting_for_balance_amount, F.from_user.id == ADMIN_ID)
async def confirm_balance_adjustment(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("⚠️ Summani musbat butun sonlarda yuboring:")
    amount = int(message.text.strip())
    if not 1 <= amount <= 1_000_000_000:
        return await message.answer("⚠️ Summa 1 dan 1 000 000 000 so'mgacha bo'lishi kerak:")

    data = await state.get_data()
    user_id = data.get("balance_adjustment_user_id")
    if not user_id:
        await state.clear()
        return await message.answer("⚠️ Foydalanuvchi ID si topilmadi. Jarayonni qaytadan boshlang.")
    request_token = uuid.uuid4().hex
    await state.update_data(
        balance_adjustment_token=request_token,
        balance_adjustment_amount=amount,
    )
    await state.set_state(None)
    await message.answer(
        "⚠️ <b>Balansni to'ldirishni tasdiqlang</b>\n\n"
        f"Foydalanuvchi ID: <code>{user_id}</code>\n"
        f"Summa: <b>{amount:,}".replace(",", " ")
        + " so'm\n\nBu amal foydalanuvchi balansiga darhol qo'shiladi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [inline_button("✅ Tasdiqlash va pul qo'shish", "adm_balance_confirm", ButtonStyle.SUCCESS)],
            [inline_button("❌ Bekor qilish", "cancel_flow", ButtonStyle.DANGER)],
        ]),
        parse_mode="HTML",
    )

# --- Havola qo'shish ---
@dp.message(AdminStates.waiting_for_new_link, F.from_user.id == ADMIN_ID)
async def process_new_links(message: types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("⚠️ Havolalarni matn ko'rinishida yuboring.")
    submitted_links = [
        link.strip() for link in message.text.splitlines() if link.strip()
    ]
    links = list(dict.fromkeys(submitted_links))
    count = 0
    async with database() as db:
        for link in links:
            async with db.execute("SELECT 1 FROM links WHERE link=? LIMIT 1", (link,)) as cur:
                exists = await cur.fetchone()
            if not exists:
                await db.execute("INSERT INTO links (link) VALUES (?)", (link,))
                count += 1
        await db.commit()
    await message.answer(
        f"✅ {count} ta yangi havola qo'shildi.\n"
        f"⏭ Takroriy havola: {len(submitted_links) - count} ta."
    )
    await state.clear()

@dp.message(AdminStates.waiting_for_price, F.from_user.id == ADMIN_ID)
async def process_new_price(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("⚠️ Narx musbat butun son bo'lishi kerak. Qayta kiriting:")
    price = int(message.text)
    async with database() as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('price', ?)",
            (str(price),),
        )
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Mahsulot narxi {price:,} so'm qilib saqlandi.".replace(",", " "))

@dp.message(AdminStates.waiting_for_card_number, F.from_user.id == ADMIN_ID)
async def process_new_card_number(message: types.Message, state: FSMContext):
    card_number = re.sub(r"\s+", "", message.text or "")
    if not card_number.isdigit() or not 12 <= len(card_number) <= 19:
        return await message.answer("⚠️ Karta raqami 12–19 ta raqamdan iborat bo'lishi kerak.")
    async with database() as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_number', ?)",
            (card_number,),
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Karta raqami yangilandi.")

@dp.message(AdminStates.waiting_for_card_owner, F.from_user.id == ADMIN_ID)
async def process_new_card_owner(message: types.Message, state: FSMContext):
    card_owner = (message.text or "").strip()
    if not card_owner or len(card_owner) > 80:
        return await message.answer("⚠️ Karta egasining ismini kiriting (1–80 belgi).")
    async with database() as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_owner', ?)",
            (card_owner,),
        )
        await db.commit()
    await state.clear()
    await message.answer("✅ Karta egasining ismi yangilandi.")

# --- Xabar tarqatish (Broadcast) To'liq ishlaydigan varianti ---
@dp.message(AdminStates.waiting_for_broadcast, F.from_user.id == ADMIN_ID)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    async with database() as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            users = await cur.fetchall()
            
    sent, failed = 0, 0
    progress_msg = await message.answer(f"⏳ Xabar yuborilmoqda... (Kuting, Jami: {len(users)} ta user)")
    
    for (user_id,) in users:
        while True:
            try:
                await message.send_copy(chat_id=user_id)
                sent += 1
                break
            except TelegramRetryAfter as error:
                logging.warning(
                    "Broadcast rate limit reached; retrying after %s seconds.",
                    error.retry_after,
                )
                await asyncio.sleep(error.retry_after)
            except Exception:
                logging.exception("Broadcast failed for user_id=%s", user_id)
                failed += 1
                break
        await asyncio.sleep(0.05) # Telegram bloklamasligi uchun kichik pauza
        
    try:
        await progress_msg.edit_text(
            f"✅ Tarqatish yakunlandi!\n\n"
            f"🟢 Muvaffaqiyatli: {sent} ta\n"
            f"🔴 Xatolik (Bloklaganlar): {failed} ta"
        )
    except TelegramAPIError:
        logging.exception("Broadcast finished but its progress message could not be updated.")

# --- Userbot Auth Mantiqasi ---
@dp.message(AdminStates.waiting_for_phone, F.from_user.id == ADMIN_ID)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    try:
        await ensure_userbot_client()
        sent_code = await app.send_code(phone)
        await state.update_data(phone=phone, phone_code_hash=sent_code.phone_code_hash)
        await message.answer("📩 Kod yuborildi! Iltimos kodni kiriting (12.345 qilsangiz ham bo'ladi):")
        await state.set_state(AdminStates.waiting_for_auth_code)
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {e}")
        await state.clear()

@dp.message(AdminStates.waiting_for_auth_code, F.from_user.id == ADMIN_ID)
async def process_auth_code(message: types.Message, state: FSMContext):
    code = re.sub(r'\D', '', message.text)
    data = await state.get_data()
    try:
        await ensure_userbot_client()
        await app.sign_in(data['phone'], data['phone_code_hash'], code)
        await restart_userbot_after_authorization()
        await message.answer(f"✅ Akkaunt muvaffaqiyatli ulandi!")
        await state.clear()
    except SessionPasswordNeeded:
        await message.answer("🔐 Akkauntda 2FA parol o'rnatilgan. Parolni kiriting:")
        await state.set_state(AdminStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ Kod noto'g'ri: {e}")
        await state.clear()

@dp.message(AdminStates.waiting_for_2fa, F.from_user.id == ADMIN_ID)
async def process_2fa(message: types.Message, state: FSMContext):
    try:
        await ensure_userbot_client()
        await app.check_password(message.text)
        await restart_userbot_after_authorization()
        await message.answer("✅ 2FA qabul qilindi, akkaunt ulandi!")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Parol noto'g'ri: {e}")

# ==========================================
# 🤖 AVTO TO'LOV MANTIQASI (PYROGRAM)
# ==========================================
PAYMENT_BOTS = ["HUMOcardbot", "Uzcard_bot", "TBC_uz_bot", "agrobank_bot"] 


async def notify_userbot_unavailable():
    try:
        await bot.send_message(
            ADMIN_ID,
            "⚠️ <b>Avto-to'lov kuzatuvchisi ishlamayapti.</b>\n"
            "Bot buyurtmalarga javob beradi, ammo karta to'lovlarini avtomatik "
            "tasdiqlamaydi. Admin panel → Userbot bo'limidan ulanishni tekshiring.",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        logging.exception("Admin could not be notified that the payment userbot is unavailable.")


def load_pyrogram():
    global Client, filters, MessageHandler, SessionPasswordNeeded
    from pyrogram import Client, filters
    from pyrogram.errors import SessionPasswordNeeded
    from pyrogram.handlers import MessageHandler


def create_userbot_client():
    load_pyrogram()
    client = Client(
        "admin_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=False,
    )
    client.add_handler(MessageHandler(
        check_payment,
        filters.chat(PAYMENT_BOTS) & filters.text,
    ))
    return client


async def ensure_userbot_client():
    global app
    if app is None:
        app = create_userbot_client()
    if not app.is_connected:
        is_authorized = await app.connect()
        if is_authorized:
            await app.disconnect()
            await app.start()
    return app


async def check_payment(client, message):
    text = message.text
    match = re.search(r"➕\s*([\d][\d\s.,]*?)\s*UZS\b", text, re.IGNORECASE)
    if match:
        raw_amount = re.sub(r"\s+", "", match.group(1))
        decimal_match = re.fullmatch(r"([\d.,]+)[.,](\d{2})", raw_amount)
        if decimal_match:
            received_amount = int(re.sub(r"\D", "", decimal_match.group(1)))
        else:
            received_amount = int(re.sub(r"\D", "", raw_amount))
        
        tx = None
        expired = False
        async with database() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT id, user_id, amount FROM transactions "
                "WHERE unique_amount=? AND status='pending' "
                "ORDER BY id DESC LIMIT 1",
                (received_amount,),
            )
            tx = await cursor.fetchone()
            if tx:
                tx_id, user_id, base_amount = tx
                cursor = await db.execute(
                    "UPDATE transactions SET status='paid' "
                    "WHERE id=? AND status='pending' "
                    "AND datetime(created_at) > datetime('now', '-5 minutes')",
                    (tx_id,),
                )
                if cursor.rowcount != 1:
                    tx = None
                    expired = True
                    await db.execute(
                        "UPDATE transactions SET status='cancelled' "
                        "WHERE id=? AND status='pending'",
                        (tx_id,),
                    )
                    await db.commit()
                else:
                    await db.execute(
                        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                        (user_id,),
                    )
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (base_amount, user_id),
                    )
                    await db.commit()
            else:
                cursor = await db.execute(
                    "UPDATE transactions SET status='cancelled' "
                    "WHERE unique_amount=? AND status='pending' "
                    "AND datetime(created_at) <= datetime('now', '-5 minutes')",
                    (received_amount,),
                )
                expired = cursor.rowcount > 0
                await db.commit()

        if tx:
            tx_id, user_id, base_amount = tx
            success_text = (
                f"✅ To'lovingiz qabul qilindi!\n➕ {base_amount:,} "
                "so'm balansingizga qo'shildi."
            ).replace(",", " ")
            try:
                await bot.send_message(user_id, success_text)
            except TelegramAPIError:
                logging.exception("Foydalanuvchiga to'lov tasdig'i yuborilmadi (user_id=%s)", user_id)
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🟢 Yangi to'lov!\nUser: {user_id}\nSumma: {base_amount:,} so'm".replace(",", " "),
                )
            except TelegramAPIError:
                logging.exception("Adminga to'lov xabari yuborilmadi (user_id=%s)", user_id)
        else:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "⚠️ <b>Mos kelmagan bank to'lovi</b>\n"
                    f"Summa: <b>{received_amount:,}".replace(",", " ")
                    + " so'm</b>\n"
                    f"Manba: <code>{html.escape(str(message.chat.id))}</code>\n"
                    + (
                        "To'lov muddati o'tgan, tranzaksiyaga mos kelmadi. "
                        if expired
                        else "Mos pending to'lov topilmadi. "
                    )
                    + "Balansga avtomatik qo'shilmadi; tranzaksiyani tekshiring.",
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                logging.exception(
                    "Mismatched bank payment alert could not be sent (amount=%s)",
                    received_amount,
                )

# ==========================================
# 🚀 ISHGA TUSHIRISH
# ==========================================
async def main():
    global app
    await init_db()
    expiry_task = asyncio.create_task(invoice_expiry_worker())
    try:
        try:
            app = create_userbot_client()
            is_authorized = await app.connect()
            if is_authorized:
                await app.disconnect()
                await app.start()
                logging.info("Payment monitor userbot started.")
            else:
                logging.warning(
                    "Payment monitor userbot is not authorized; bot polling will continue."
                )
                await notify_userbot_unavailable()
        except Exception:
            logging.exception(
                "Payment monitor userbot failed to start; bot polling will continue."
            )
            await notify_userbot_unavailable()
            if app and app.is_connected:
                try:
                    await app.disconnect()
                except Exception:
                    logging.exception("Failed to disconnect the incomplete userbot client.")

        await dp.start_polling(bot)
    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        try:
            if app and app.is_initialized:
                await app.stop()
            elif app and app.is_connected:
                await app.disconnect()
        except Exception:
            logging.exception("Failed to stop the payment monitor userbot cleanly.")
        finally:
            await bot.session.close()

async def restart_userbot_after_authorization():
    if app is None:
        raise RuntimeError("Userbot hali ishga tushirilmagan.")
    if app.is_connected:
        if app.is_initialized:
            return
        await app.disconnect()
    await app.start()

if __name__ == "__main__":
    asyncio.run(main())
