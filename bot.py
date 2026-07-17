"""
ORIX GLOBAL — Qo'llanma-bot
Vazifa: Qo'llanma tarqatish orqali odamlarni kanalga (@orix_global_agency) obuna qilish.

Oqim:
  /start → xush kelibsiz
  → "Qaysi qo'llanma?" (tugmalar)
  → "Avval kanalga obuna bo'ling" + [Obuna bo'lish] + [Tekshirish]
  → Obuna bo'lsa: qo'llanma linki
  → Obuna bo'lmasa: eslatma (10 daqiqa → 1 soat → 12 soat)
"""

import logging
import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Sozlamalar (Railway environment variables) ─────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
DB_PATH = "/data/qollanma.db"  # Railway Volume — /data ga mount qilinsin!

# Kanal — bot shu kanalda ADMIN bo'lishi shart (obunani tekshirish uchun)
CHANNEL_USERNAME = "@orix_global_agency"
CHANNEL_LINK = "https://t.me/orix_global_agency"

# ═══════════════════════════════════════════════════════════════════════════════
# QO'LLANMALAR — yangi qo'llanma qo'shish uchun shu ro'yxatga qator qo'shing
# ═══════════════════════════════════════════════════════════════════════════════
#   key    — noyob belgi (tugma uchun)
#   title  — foydalanuvchi ko'radigan nom
#   url    — kanaldagi post linki
#
# Moslashuvchan: qancha qo'shsangiz, shuncha tugma chiqadi.
# Agar 4 tadan ko'p bo'lsa — avtomatik raqamli ro'yxat ko'rinishiga o'tadi.

GUIDES = [
    {
        "key": "g1",
        "title": "TOP universitetlar va kuchli yo'nalishlari",
        "url": "https://t.me/orix_global_agency/622",
    },
    {
        "key": "g2",
        "title": "100% grant yutgan talabalar tajribasi",
        "url": "https://t.me/orix_global_agency/634",
    },
    {
        "key": "g3",
        "title": "100% grant olish bo'yicha to'liq qo'llanma",
        "url": "https://t.me/orix_global_agency/649",
    },
]

# Necha tugmadan keyin raqamli ro'yxatga o'tish
MAX_BUTTONS_INLINE = 4

# Eslatma zanjiri (daqiqalarda): 10 daqiqa, 1 soat, 12 soat
REMINDER_DELAYS = [10, 60, 720]


# ─── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            chosen_guide TEXT,
            subscribed INTEGER DEFAULT 0,
            guide_sent INTEGER DEFAULT 0,
            reminders_sent INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            last_reminder_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_user(telegram_id, username):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (telegram_id, username) VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
    """, (telegram_id, username))
    conn.commit()
    conn.close()


def set_chosen_guide(telegram_id, guide_key):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET chosen_guide = ?, reminders_sent = 0, last_reminder_at = NULL WHERE telegram_id = ?",
        (guide_key, telegram_id)
    )
    conn.commit()
    conn.close()


def mark_subscribed_and_sent(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET subscribed = 1, guide_sent = 1 WHERE telegram_id = ?",
        (telegram_id,)
    )
    conn.commit()
    conn.close()


def get_user(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_guide(guide_key):
    return next((g for g in GUIDES if g["key"] == guide_key), None)


def get_pending_reminders():
    """
    Eslatma yuborish kerak bo'lgan foydalanuvchilar:
    - qo'llanma tanlagan
    - lekin obuna bo'lmagan (guide_sent = 0)
    - eslatma zanjiri tugamagan
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM users
        WHERE chosen_guide IS NOT NULL
          AND guide_sent = 0
          AND reminders_sent < ?
    """, (len(REMINDER_DELAYS),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bump_reminder(telegram_id, count):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET reminders_sent = ?, last_reminder_at = datetime('now','localtime') WHERE telegram_id = ?",
        (count, telegram_id)
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    chose = conn.execute("SELECT COUNT(*) FROM users WHERE chosen_guide IS NOT NULL").fetchone()[0]
    subscribed = conn.execute("SELECT COUNT(*) FROM users WHERE guide_sent = 1").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM users WHERE date(created_at) = date('now','localtime')"
    ).fetchone()[0]
    # Qo'llanma bo'yicha
    by_guide = conn.execute("""
        SELECT chosen_guide, COUNT(*) FROM users
        WHERE guide_sent = 1 GROUP BY chosen_guide
    """).fetchall()
    conn.close()
    return total, chose, subscribed, today, by_guide


# ─── Yordamchilar ───────────────────────────────────────────────────────────────
def is_admin(user_id):
    return user_id == ADMIN_CHAT_ID


def guide_selection_message():
    """
    Qo'llanma tanlash xabari.
    Agar qo'llanmalar 4 tadan kam bo'lsa — har biri alohida tugma.
    Ko'p bo'lsa — matnda raqamlab yoziladi, tugmalar raqamli bo'ladi.
    """
    if len(GUIDES) <= MAX_BUTTONS_INLINE:
        text = "Qaysi qo'llanmani olmoqchisiz?"
        keyboard = [
            [InlineKeyboardButton(g["title"], callback_data=f"choose:{g['key']}")]
            for g in GUIDES
        ]
    else:
        # Raqamli ro'yxat
        lines = ["Qaysi qo'llanmani olmoqchisiz?\n"]
        for i, g in enumerate(GUIDES, 1):
            lines.append(f"{i}. {g['title']}")
        text = "\n".join(lines) + "\n\nKerakli raqamni tanlang:"
        # Tugmalarni 3 tadan qatorlab joylashtiramiz
        buttons = [
            InlineKeyboardButton(str(i), callback_data=f"choose:{g['key']}")
            for i, g in enumerate(GUIDES, 1)
        ]
        keyboard = [buttons[i:i+3] for i in range(0, len(buttons), 3)]

    return text, InlineKeyboardMarkup(keyboard)


def subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Kanalga obuna bo'lish", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Obuna bo'ldim, tekshirish", callback_data="check_sub")],
    ])


async def is_subscribed(context, telegram_id):
    """Foydalanuvchi kanalga obuna bo'lganini tekshiradi."""
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, telegram_id)
        return member.status in ("member", "administrator", "creator")
    except BadRequest as e:
        # Bot kanalda admin emas yoki kanal topilmadi
        logger.error(f"Obuna tekshirishda xatolik (bot kanalda adminmi?): {e}")
        return None  # noma'lum
    except Exception as e:
        logger.warning(f"Obuna tekshirishda xatolik {telegram_id}: {e}")
        return None


# ─── Handlerlar ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username)

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n"
        "\n"
        "Orix Global botiga xush kelibsiz.\n"
        "\n"
        "Bu yerda Koreya universitetlariga grant bilan kirish bo'yicha "
        "foydali qo'llanmalarni bepul olishingiz mumkin."
    )

    text, keyboard = guide_selection_message()
    await update.message.reply_text(text, reply_markup=keyboard)


async def choose_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    guide_key = query.data.split(":", 1)[1]
    guide = get_guide(guide_key)
    if not guide:
        await query.message.reply_text("Qo'llanma topilmadi. /start bosing.")
        return

    set_chosen_guide(query.from_user.id, guide_key)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.reply_text(
        f"Siz tanladingiz: {guide['title']}\n"
        "\n"
        "Qo'llanmani berishimiz biz uchun muammo emas. Ammo avval "
        "kanalimizga obuna bo'lib olishingizni so'raymiz.\n"
        "\n"
        "Kanalimizda Koreya ta'limi, grantlar va universitetlar haqida "
        "doimiy foydali ma'lumotlar chiqib turadi.\n"
        "\n"
        "Obuna bo'lgach, \"Tekshirish\" tugmasini bosing 👇",
        reply_markup=subscribe_keyboard()
    )


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = get_user(user_id)
    if not user or not user.get("chosen_guide"):
        await query.message.reply_text("Avval qo'llanma tanlang. /start bosing.")
        return

    subbed = await is_subscribed(context, user_id)

    if subbed is None:
        # Texnik muammo — bot kanalda admin emas
        await query.message.reply_text(
            "Tekshirishda xatolik yuz berdi. Iltimos, birozdan keyin qayta urinib ko'ring "
            "yoki @Orix_Global_admin ga yozing."
        )
        # Adminni ogohlantiramiz
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                "⚠️ DIQQAT: Obuna tekshiruvi ishlamayapti!\n"
                "Bot kanalda admin ekanini tekshiring."
            )
        except Exception:
            pass
        return

    if subbed:
        guide = get_guide(user["chosen_guide"])
        mark_subscribed_and_sent(user_id)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"Rahmat! Obunangiz tasdiqlandi ✅\n"
            "\n"
            f"Mana sizning qo'llanmangiz:\n"
            f"{guide['title']}\n"
            f"{guide['url']}\n"
            "\n"
            "Savollaringiz bo'lsa, bemalol yozing: @Orix_Global_admin"
        )
    else:
        await query.message.reply_text(
            "Hali obuna bo'lmagansiz 🙈\n"
            "\n"
            "Kanalga obuna bo'lgach, \"Tekshirish\" tugmasini qayta bosing.",
            reply_markup=subscribe_keyboard()
        )


# ─── Eslatma zanjiri ────────────────────────────────────────────────────────────
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Har 2 daqiqada ishlaydi.
    Qo'llanma tanlab, lekin obuna bo'lmaganlarga eslatma yuboradi.
    Zanjir: 10 daqiqa → 1 soat → 12 soat.
    """
    now = datetime.now()
    users = get_pending_reminders()

    for user in users:
        telegram_id = user["telegram_id"]
        reminders_sent = user["reminders_sent"]

        # Keyingi eslatmagacha qancha vaqt kerak?
        if reminders_sent >= len(REMINDER_DELAYS):
            continue

        delay_minutes = REMINDER_DELAYS[reminders_sent]

        # Boshlanish nuqtasi: oxirgi eslatma yoki qo'llanma tanlangan vaqt
        base_time_str = user.get("last_reminder_at") or user.get("created_at")
        try:
            base_time = datetime.strptime(base_time_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        if now < base_time + timedelta(minutes=delay_minutes):
            continue  # hali vaqti kelmagan

        # Obuna bo'lib qo'ymadimi — qayta tekshiramiz
        subbed = await is_subscribed(context, telegram_id)
        if subbed:
            guide = get_guide(user["chosen_guide"])
            mark_subscribed_and_sent(telegram_id)
            try:
                await context.bot.send_message(
                    telegram_id,
                    f"Obunangiz tasdiqlandi ✅\n\n"
                    f"Mana qo'llanmangiz:\n{guide['title']}\n{guide['url']}\n\n"
                    f"Savollaringiz bo'lsa: @Orix_Global_admin"
                )
            except Exception:
                pass
            continue

        # Eslatma matni (bosqichga qarab)
        guide = get_guide(user["chosen_guide"])
        if reminders_sent == 0:
            text = (
                "Eslatma 🔔\n"
                "\n"
                f"Siz \"{guide['title']}\" qo'llanmasini so'ragan edingiz.\n"
                "\n"
                "Uni olish uchun kanalga obuna bo'lishingiz kifoya. "
                "Bir daqiqa ham vaqt olmaydi."
            )
        elif reminders_sent == 1:
            text = (
                "Qo'llanmangiz hali sizni kutyapti 📚\n"
                "\n"
                "Kanalga obuna bo'ling — qo'llanma darhol yuboriladi.\n"
                "\n"
                "Kanalda grant va universitetlar haqida foydali postlar ham bor."
            )
        else:
            text = (
                "Oxirgi eslatma 📌\n"
                "\n"
                f"\"{guide['title']}\" qo'llanmasi tayyor.\n"
                "\n"
                "Obuna bo'lsangiz — hoziroq olasiz."
            )

        try:
            await context.bot.send_message(telegram_id, text, reply_markup=subscribe_keyboard())
            bump_reminder(telegram_id, reminders_sent + 1)
            await asyncio.sleep(0.1)
        except Forbidden:
            # Bloklagan — eslatmani to'xtatamiz
            bump_reminder(telegram_id, len(REMINDER_DELAYS))
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.warning(f"Eslatma yuborilmadi {telegram_id}: {e}")


# ─── Admin buyruqlari ───────────────────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total, chose, subscribed, today, by_guide = get_stats()
    conv = round(subscribed / chose * 100, 1) if chose else 0

    guide_lines = ""
    for gk, cnt in by_guide:
        g = get_guide(gk)
        name = g["title"] if g else gk
        guide_lines += f"  • {name}: {cnt} ta\n"
    if not guide_lines:
        guide_lines = "  Hali yo'q"

    msg = (
        "📊 Statistika\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Jami foydalanuvchi: {total} ta (bugun: {today})\n"
        f"📖 Qo'llanma tanlagan: {chose} ta\n"
        f"✅ Obuna + qo'llanma olgan: {subscribed} ta\n"
        f"📈 Konversiya: {conv}%\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📚 Qo'llanma bo'yicha:\n"
        f"{guide_lines}"
    )
    await update.message.reply_text(msg)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = (
        "🛠 Admin panel\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/stats — Statistika\n"
        "\n"
        f"Kanal: {CHANNEL_USERNAME}\n"
        f"Qo'llanmalar soni: {len(GUIDES)} ta\n"
        "\n"
        "Eslatma: bot kanalda ADMIN bo'lishi shart "
        "(obunani tekshirish uchun)."
    )
    await update.message.reply_text(msg)


# ─── Global xato ushlagich ──────────────────────────────────────────────────────
async def error_handler(update, context):
    err = context.error
    if isinstance(err, (Forbidden, TimedOut, NetworkError, RetryAfter)):
        return
    logger.error(f"Xatolik: {err}", exc_info=err)


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(choose_guide, pattern="^choose:"))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub$"))
    app.add_error_handler(error_handler)

    # Eslatma zanjiri — har 2 daqiqada tekshiradi
    app.job_queue.run_repeating(reminder_job, interval=120, first=30)

    logger.info("Qo'llanma-bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
