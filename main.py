import os
import sqlite3
import logging
import time
import random
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ErrorEvent, BufferedInputFile, FSInputFile, TelegramObject, ChatJoinRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from fastapi import FastAPI
import uvicorn
import io
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# =====================================================================
# CONFIGURATION & LOGGING SETUP (v4.2.0 - Stabilized / Render Web Server)
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logger.warning("BOT_TOKEN environment variable topilmadi! Bot ishga tushirilmaydi.")

# Muhim sozlamalar endi environment variable orqali ham beriladi.
# Agar env var berilmasa, oldingi standart qiymatlar ishlatiladi (mavjud botni buzmaslik uchun).
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8648138832"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1004485992940"))
MAIN_CHANNEL_ID = int(os.environ.get("MAIN_CHANNEL_ID", "-1003877551889"))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "uzmovi_va_asl_mediya")
ADMIN_LINK = os.environ.get("ADMIN_LINK", "https://t.me/ll_e_o_n_001")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Bot holati monitoring uchun global o'zgaruvchilar
BOT_START_TIME = time.time()
ERROR_COUNT = 0

# Botning @username'ini keshlab saqlaymiz - kanal postlaridagi "Tomosha qilish"
# tugmalari uchun chuqur havola (deep-link) qurishda ishlatiladi.
_bot_username_cache = None
async def get_bot_username() -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        try:
            me = await bot.get_me()
            _bot_username_cache = me.username
        except Exception as e:
            logger.error(f"Bot username olishda xatolik: {e}")
            _bot_username_cache = "Premium_kinolari_bot"  # zaxira holat
    return _bot_username_cache

# Janrlar ro'yxati (bitta joyda saqlanadi, botning barcha qismlarida shu ro'yxat ishlatiladi)
GENRES_LIST = ["Jangari", "Komediya", "Drama", "Melodrama", "Detektiv", "Multfilm", "Fantastika"]
# Janr asosida caption shablonlari
GENRE_TEMPLATES = {
    "Jangari": "🎬 Nafas ichida qolasiz — har soniyasi jang va jasoratga to'la!",
    "Komediya": "😂 Kulgidan ich ketadi — kayfiyatingiz kafolatlangan holda ko'tariladi!",
    "Drama": "🎭 Qalbingizga tegadigan, hayotning o'zidek chin va ta'sirchan hikoya.",
    "Melodrama": "💔 Sevgi, sadoqat va sog'inch — yuragingiz to'lqinlanishga tayyor bo'lsin.",
    "Detektiv": "🔍 Har burilishda yangi sir — javobni oxirigacha topa olasizmi?",
    "Multfilm": "🎨 Rang-barang, quvnoq va mehr bilan — butun oila birga zavqlanadi!",
    "Fantastika": "🚀 Boshqa olamlar, kutilmagan texnologiyalar — sarguzasht hozir boshlanadi!"
}


# FastAPI web server for Render port binding
app = FastAPI()

@app.get("/")
async def index():
    return {"status": "Kino Bot v4.1.3.2 is running 24/7 successfully"}

# Universal safe database path for Render/Linux and Windows
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
STATUS_EXPORT_PATH = os.path.join(BASE_DIR, 'status_export.json')

last_request_time = {}

# =====================================================================
# DATABASE HELPER (v4.2.0 - WAL mode + kafolatlangan yopilish)
# =====================================================================
@contextmanager
def db_connect():
    """
    Har bir chaqiriqda xavfsiz DB ulanish beradi.
    WAL rejimi va busy_timeout tufayli 'database is locked' xatosi kamayadi.
    Xatolik yuz bersa ham ulanish albatta yopiladi (resurs sizib chiqishining oldini oladi).
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        cursor = conn.cursor()
        yield conn, cursor
    finally:
        conn.close()

def get_last_main_channel_code() -> int:
    """Asosiy kanalga oxirgi joylangan kino kodini qaytaradi (hali bo'lmasa 0)."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'last_main_channel_code'")
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0

def set_last_main_channel_code(code: int):
    """Asosiy kanalga oxirgi joylangan kino kodini saqlaydi."""
    with db_connect() as (conn, cursor):
        cursor.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('last_main_channel_code', ?)",
            (str(code),)
        )
        conn.commit()

def is_code_already_posted(code: int) -> bool:
    """Kod avval asosiy kanalga joylanganmi tekshiradi (takror joylanishning oldini olish uchun)."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM main_channel_posts WHERE code = ?", (code,))
        return cursor.fetchone() is not None

def get_setting(key: str, default=None):
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
    return row[0] if row and row[0] is not None else default

def set_setting(key: str, value):
    with db_connect() as (conn, cursor):
        cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()

# VIP Bozor narxlari: 1-qator qat'iy (o'zgarmas), 2-qator super admin belgilagan chegirma
VIP_FIXED_PRICES = [(3, "3 kun", 2000), (10, "10 kun", 5000), (30, "1 oy", 10000)]

def get_vip_discount_price(days: int):
    """Berilgan muddat uchun chegirma narxini qaytaradi (agar admin sozlamagan bo'lsa None)."""
    val = get_setting(f"vip_discount_{days}")
    return int(val) if val is not None else None

def get_vip_card_info():
    number = get_setting("vip_card_number", "❗️ Hali sozlanmagan")
    holder = get_setting("vip_card_holder", "")
    return number, holder

def resolve_secret_message_id(code) -> int:
    """Kod uchun maxfiy kanaldagi HAQIQIY xabar raqamini qaytaradi.
    Bot orqali (yangi tizim) yuklangan kinolar uchun alohida xarita saqlanadi
    (chunki bizning 'kod' hisoblagichimiz kanal xabar raqamidan mustaqil).
    Eski (bot mavjud bo'lmasdan oldin qo'lda joylangan) kinolar uchun
    kod = xabar raqami bo'lgani sababli, xarita topilmasa to'g'ridan-to'g'ri kodni ishlatadi."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT secret_message_id FROM main_channel_posts WHERE code = ?", (int(code),))
        row = cursor.fetchone()
    if row and row[0]:
        return int(row[0])
    return int(code)

# =====================================================================
# FSM STATES (v4.1.3.2)
# =====================================================================
class SuperAdminState(StatesGroup):
    waiting_for_assistant_id = State()
    waiting_for_short_video = State()
    waiting_for_full_video = State()
    waiting_for_short_type = State()
    waiting_for_short_genre = State()
    waiting_for_short_vip_duration = State()
    waiting_for_short_code = State()
    waiting_for_restrict_id = State()
    waiting_for_restrict_hours = State()
    waiting_for_promo_name = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_limit = State()
    waiting_for_coin_user_id = State()
    waiting_for_coin_value = State()
    waiting_for_channel_input = State()
    waiting_for_broadcast_schedule = State()
    waiting_for_status_import_file = State()

class UserState(StatesGroup):
    waiting_for_movie_code = State()
    waiting_for_vip_movie_request = State()
    waiting_for_comment_text = State()
    waiting_for_promo_input = State()

class AdminState(StatesGroup):
    waiting_for_vip_code = State()
    waiting_for_broadcast = State()
    waiting_for_genre_code = State()

class VipPurchaseState(StatesGroup):
    waiting_for_receipt_photo = State()

class GrantVipState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_duration = State()

class BozorAdminState(StatesGroup):
    waiting_for_card_number = State()
    waiting_for_card_holder = State()
    waiting_for_discount_price = State()

# =====================================================================
# DATABASE ARCHITECTURE (v4.1.3.2)
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        username TEXT,
        role TEXT DEFAULT 'user',
        restricted_until REAL DEFAULT 0,
        vip_expires_at REAL DEFAULT 0,
        referrals_count INTEGER DEFAULT 0,
        free_vip_views INTEGER DEFAULT 0,
        joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    for col, default in [
        ('role', "'user'"), ('restricted_until', 0), ('renewal_reminder_sent', 0),
        ('main_coin', 0), ('daily_coin', 0), ('diamonds', 0),
        ('last_search_at', 0), ('last_daily_reward_at', 0),
        ('pending_watch_code', 'NULL')
    ]:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vip_gift_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id INTEGER,
        to_id INTEGER,
        days INTEGER,
        created_at REAL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vip_sales_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        days INTEGER,
        granted_at REAL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        error_text TEXT,
        occurred_at REAL,
        sent_in_digest INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS main_channel_posts (
        code INTEGER PRIMARY KEY,
        genre TEXT,
        year INTEGER,
        posted_at REAL,
        video_file_id TEXT,
        is_vip INTEGER DEFAULT 0,
        vip_until REAL DEFAULT 0,
        reposted_as_free INTEGER DEFAULT 0
    )
    """)
    for col, default in [('video_file_id', 'NULL'), ('is_vip', 0), ('vip_until', 0), ('reposted_as_free', 0), ('secret_message_id', 'NULL')]:
        try:
            cursor.execute(f"ALTER TABLE main_channel_posts ADD COLUMN {col} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vip_purchase_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        duration_label TEXT,
        duration_days INTEGER,
        price INTEGER,
        receipt_file_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at REAL,
        decided_at REAL
    )
    """)
    try:
        cursor.execute("ALTER TABLE vip_purchase_requests ADD COLUMN admin_msg_id INTEGER")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS referral_logs (
        referrer_id INTEGER,
        referred_id INTEGER,
        PRIMARY KEY(referrer_id, referred_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS promo_codes (
        code_name TEXT PRIMARY KEY,
        reward_coins INTEGER DEFAULT 1,
        max_uses INTEGER DEFAULT 10,
        used_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promo_type TEXT DEFAULT 'coins',
        vip_hours INTEGER DEFAULT 0
    )
    """)
    for col, default in [('promo_type', "'coins'"), ('vip_hours', 0)]:
        try:
            cursor.execute(f"ALTER TABLE promo_codes ADD COLUMN {col} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS used_promos (
        user_id INTEGER,
        code_name TEXT,
        used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, code_name)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saved_movies (
        user_id INTEGER,
        movie_code TEXT,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, movie_code)
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movie_stats (
        code TEXT PRIMARY KEY,
        likes INTEGER DEFAULT 0,
        dislikes INTEGER DEFAULT 0,
        views_count INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movie_votes (
        user_id INTEGER,
        code TEXT,
        vote_count INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, code)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vip_movies (
        code TEXT PRIMARY KEY,
        added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        vip_until REAL DEFAULT NULL
    )
    """)
    try:
        cursor.execute("ALTER TABLE vip_movies ADD COLUMN vip_until REAL DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movies_meta (
        code TEXT PRIMARY KEY,
        genre TEXT DEFAULT 'Barchasi',
        is_vip BOOLEAN DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        channel_username TEXT,
        channel_title TEXT,
        invite_link TEXT
    )
    """)
    try:
        cursor.execute("ALTER TABLE required_channels ADD COLUMN invite_link TEXT")
    except sqlite3.OperationalError:
        pass

    # Yopiq/maxfiy kanallarda "har bir a'zoni tasdiqlash" sozlamasi yoqilgan bo'lishi mumkin -
    # bunda foydalanuvchi darhol to'liq a'zo bo'lmaydi, faqat qo'shilish so'rovi yuboradi.
    # Shu so'rovlarni saqlab, obuna tekshiruvida "yetarli" deb hisoblash uchun ishlatamiz.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS channel_join_requests (
        user_id INTEGER,
        channel_id TEXT,
        requested_at REAL,
        PRIMARY KEY(user_id, channel_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_log (
        broadcast_id TEXT,
        user_id INTEGER,
        message_id INTEGER
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vip_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        movie_name TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movie_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        user_id INTEGER,
        full_name TEXT,
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name, username, role) VALUES (?, 'Super Admin', 'admin', 'super_admin')", (SUPER_ADMIN_ID,))
    cursor.execute("UPDATE users SET role = 'super_admin' WHERE user_id = ?", (SUPER_ADMIN_ID,))
    
    conn.commit()
    conn.close()

init_db()

# =====================================================================
# CORE FUNCTIONS & ROLE MANAGEMENT
# =====================================================================
def update_last_active(user_id: int):
    with db_connect() as (conn, cursor):
        cursor.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
        conn.commit()

def get_user_role(user_id: int) -> str:
    """
    Foydalanuvchi rolini qaytaradi. Agar VIP muddati allaqachon tugagan bo'lsa,
    HAR CHAQIRIQDA (real vaqt rejimida) darhol 'user' ga tushiriladi - shu tufayli
    foydalanuvchi istalgan tugmani bosgan zahoti aniq holat ko'rsatiladi, orada
    kutish shart emas (fonda ham 10 soniyada bir marta alohida tekshiriladi).
    """
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT role, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()

        if not res:
            return 'user'

        role, expires_at = res
        if role == 'vip' and expires_at and expires_at < time.time():
            cursor.execute("UPDATE users SET role = 'user', vip_expires_at = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            return 'user'
    return role

def is_user_restricted(user_id: int) -> bool:
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT restricted_until FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
    if res and res[0] and res[0] > time.time():
        return True
    return False

def is_vip_movie(code: str) -> bool:
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM vip_movies WHERE code = ?", (code,))
        res = cursor.fetchone()
    return res is not None

def get_movie_stats(code: str):
    """
    FAQAT o'qish uchun - hech qanday statistikani o'zgartirmaydi.
    Like/Dislike/Saqlash tugmalarini qayta chizishda ishlatiladi, shunda
    like bosilganda views_count noto'g'ri oshib ketmaydi.
    """
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT likes, dislikes, views_count FROM movie_stats WHERE code = ?", (code,))
        res = cursor.fetchone()
        if not res:
            cursor.execute("INSERT OR IGNORE INTO movie_stats (code, likes, dislikes, views_count) VALUES (?, 0, 0, 0)", (code,))
            conn.commit()
            return 0, 0, 0
        return res

def register_movie_view(code: str):
    """
    Kino HAQIQATDA foydalanuvchiga yuborilganda (copy_message muvaffaqiyatli
    bo'lgandan KEYIN) chaqiriladi - shu tufayli ko'rishlar soni aniq hisoblanadi.
    """
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM movie_stats WHERE code = ?", (code,))
        if cursor.fetchone():
            cursor.execute("UPDATE movie_stats SET views_count = views_count + 1 WHERE code = ?", (code,))
        else:
            cursor.execute("INSERT INTO movie_stats (code, likes, dislikes, views_count) VALUES (?, 0, 0, 1)", (code,))
        conn.commit()

def use_vip_coin_for_service(user_id: int) -> bool:
    """Oddiy foydalanuvchi uchun premium xususiyat narxi: 1 VIP Coin.
    Avval KUNLIK hisobdan, u tugagach ASOSIY hisobdan yechiladi."""
    role = get_user_role(user_id)
    if role in ['vip', 'assistant', 'super_admin']:
        return True
    return spend_vip_coin(user_id, 1)

def get_balances(user_id: int):
    """(main_coin, daily_coin, diamonds) qaytaradi."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT main_coin, daily_coin, diamonds FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
    if not res:
        return 0, 0, 0
    return res[0] or 0, res[1] or 0, res[2] or 0

def spend_vip_coin(user_id: int, amount: int) -> bool:
    """VIP Coin sarflaydi: avval kunlik hisobdan, tugagach asosiy hisobdan."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT main_coin, daily_coin FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        if not res:
            return False
        main_coin, daily_coin = res[0] or 0, res[1] or 0
        if main_coin + daily_coin < amount:
            return False
        if daily_coin >= amount:
            daily_coin -= amount
        else:
            remainder = amount - daily_coin
            daily_coin = 0
            main_coin -= remainder
        cursor.execute("UPDATE users SET main_coin = ?, daily_coin = ? WHERE user_id = ?", (main_coin, daily_coin, user_id))
        conn.commit()
    return True

def add_main_coin(user_id: int, amount: int):
    with db_connect() as (conn, cursor):
        cursor.execute("UPDATE users SET main_coin = main_coin + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def add_diamonds(user_id: int, amount: int):
    with db_connect() as (conn, cursor):
        cursor.execute("UPDATE users SET diamonds = diamonds + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def spend_diamonds(user_id: int, amount: int) -> bool:
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT diamonds FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        if not res or (res[0] or 0) < amount:
            return False
        cursor.execute("UPDATE users SET diamonds = diamonds - ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
    return True

def mark_movie_search(user_id: int):
    """Foydalanuvchi kino qidirganda chaqiriladi - kunlik mukofot shartini belgilaydi."""
    with db_connect() as (conn, cursor):
        cursor.execute("UPDATE users SET last_search_at = ? WHERE user_id = ?", (time.time(), user_id))
        conn.commit()

async def daily_reward_loop():
    """Har 5 daqiqada tekshiradi: kimning 24 soati to'lgan va o'sha oraliqda
    kino qidirgan bo'lsa - kunlik VIP Coin beriladi (faqat oddiy foydalanuvchilarga;
    Olmos endi faqat referal orqali beriladi, VIP/admin kunlik mukofotga muhtoj emas).
    Kunlik hisobdagi ishlatilmagan qoldiq (max 10) asosiy hisobga o'tkaziladi."""
    while True:
        try:
            now = time.time()
            with db_connect() as (conn, cursor):
                cursor.execute(
                    "SELECT user_id, daily_coin, last_search_at, last_daily_reward_at FROM users "
                    "WHERE role = 'user' AND (? - last_daily_reward_at) >= 86400 AND last_search_at >= last_daily_reward_at",
                    (now,)
                )
                eligible = cursor.fetchall()
                for user_id, daily_coin, last_search_at, last_reward_at in eligible:
                    leftover = daily_coin or 0
                    transfer_to_main = min(leftover, 10)
                    cursor.execute(
                        "UPDATE users SET main_coin = main_coin + ?, daily_coin = 15, last_daily_reward_at = ? WHERE user_id = ?",
                        (transfer_to_main, now, user_id)
                    )
                    # Talabga ko'ra: kunlik mukofot hisobga baribir qo'shiladi,
                    # lekin foydalanuvchiga bu haqda xabar yuborilmaydi.
                conn.commit()
        except Exception as e:
            logger.error(f"daily_reward_loop xatosi: {e}")
        await asyncio.sleep(300)

async def vip_purchase_loop():
    """Har 5 daqiqada tekshiradi: 1 soatdan ortiq javobsiz qolgan VIP xarid so'rovlarini
    avtomatik tasdiqlaydi (admin javob bermasa ham foydalanuvchi zararlanmasligi uchun).
    Foydalanuvchiga bu haqda (avtomatik tasdiqlanganligi haqida) HECH NARSA aytilmaydi -
    u xuddi admin qo'lda tasdiqlagandek bir xil xabarni oladi. Admin esa alohida xabar
    bilan ogohlantiriladi va agar bu xato bo'lsa, bitta tugma orqali bekor qila oladi."""
    while True:
        try:
            now = time.time()
            with db_connect() as (conn, cursor):
                cursor.execute(
                    "SELECT id, user_id, duration_days, duration_label, admin_msg_id FROM vip_purchase_requests "
                    "WHERE status = 'pending' AND (? - created_at) >= 3600",
                    (now,)
                )
                rows = cursor.fetchall()
                for req_id, user_id, duration_days, duration_label, admin_msg_id in rows:
                    grant_vip_to_user(user_id, duration_days)
                    cursor.execute(
                        "UPDATE vip_purchase_requests SET status = 'auto_approved', decided_at = ? WHERE id = ?",
                        (now, req_id)
                    )
                conn.commit()
            for req_id, user_id, duration_days, duration_label, admin_msg_id in rows:
                # Foydalanuvchiga — xuddi admin qo'lda tasdiqlagandagi bilan bir xil xabar
                # (avtomatik tasdiqlash haqida umuman aytilmaydi).
                try:
                    await bot.send_message(
                        user_id,
                        f"🎉 To'lovingiz tasdiqlandi! Sizga <b>{duration_label}</b> VIP obuna berildi.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                # Eski (screenshot bilan kelgan) xabardagi Tasdiqlash/Bekor qilish
                # tugmalarini olib tashlaymiz - endi ular eskirgan.
                if admin_msg_id:
                    try:
                        await bot.edit_message_reply_markup(chat_id=SUPER_ADMIN_ID, message_id=admin_msg_id, reply_markup=None)
                    except Exception:
                        pass
                # Adminga — alohida, aniq xabar va "bekor qilish" imkoniyati bilan.
                try:
                    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🚫 VIPni bekor qilish", callback_data=f"cancel_autovip_{req_id}")
                    ]])
                    await bot.send_message(
                        SUPER_ADMIN_ID,
                        f"⏱ #{req_id} VIP so'rovi <b>1 soat</b> ichida javobsiz qoldi — avtomatik tasdiqlandi "
                        f"(foydalanuvchi: <code>{user_id}</code>, muddat: {duration_label}).\n\n"
                        f"Agar bu xato bo'lsa, quyidagi tugma orqali bekor qilishingiz mumkin:",
                        reply_markup=cancel_kb, parse_mode="HTML"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"vip_purchase_loop xatosi: {e}")
        await asyncio.sleep(300)

async def vip_movie_expiry_loop():
    """Har 2 daqiqada tekshiradi: muddatli VIP-kino postlarining VIP muddati tugaganmi.
    Tugagan bo'lsa — kinoni VIP ro'yxatidan chiqaradi va xuddi shu kod bilan
    asosiy kanalga oddiy (ochiq) post sifatida qayta joylaydi."""
    while True:
        try:
            now = time.time()
            with db_connect() as (conn, cursor):
                cursor.execute(
                    "SELECT code, genre, year, video_file_id FROM main_channel_posts "
                    "WHERE is_vip = 1 AND vip_until > 0 AND vip_until <= ? AND reposted_as_free = 0",
                    (now,)
                )
                expired = cursor.fetchall()
            for code, genre, year, video_file_id in expired:
                try:
                    genre_desc = GENRE_TEMPLATES.get(genre, "Bugun ko'rish uchun qiziqarli film!")
                    caption = (
                        f"🎬 Kino janri: <b>{genre}</b> {year}\n"
                        f"🔛 Kino KODI : {code} ✅\n\n"
                        f"🎉 Endi barchaga ochiq!\n"
                        f"Kinoni to'ligini pastagi tugma orqali yuklab oling\n\n"
                        f"🔐 {genre_desc}\n\n"
                        f"🖇 Bot manzili: @Premium_kinolari_bot"
                    )
                    if video_file_id:
                        bot_username = await get_bot_username()
                        watch_url = f"https://t.me/{bot_username}?start=watch_{code}"
                        await bot.send_video(
                            chat_id=MAIN_CHANNEL_ID,
                            video=video_file_id,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[[InlineKeyboardButton(text="🎬 Tomosha qilish", url=watch_url)]]
                            )
                        )
                    with db_connect() as (conn, cursor):
                        cursor.execute("DELETE FROM vip_movies WHERE code = ?", (str(code),))
                        cursor.execute("UPDATE main_channel_posts SET reposted_as_free = 1, is_vip = 0 WHERE code = ?", (code,))
                        conn.commit()
                except Exception as e:
                    logger.error(f"vip_movie_expiry_loop (code={code}) xatosi: {e}")
        except Exception as e:
            logger.error(f"vip_movie_expiry_loop xatosi: {e}")
        await asyncio.sleep(120)

# =====================================================================
# BACKGROUND TASK: REAL-TIME VIP EXPIRATION MONITOR
# =====================================================================
# =====================================================================
# STATUS HISOBOTI (Export/Import) - super_admin, assistant, vip statuslarini
# saqlaydi va tiklaydi. Har 24 soatda avtomatik yangilanadi (eskisi o'chib,
# yangisi saqlanadi).
# =====================================================================
def generate_status_export() -> dict:
    now = time.time()
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT user_id, role, vip_expires_at FROM users WHERE role IN ('super_admin', 'assistant', 'vip')")
        rows = cursor.fetchall()

    statuses = []
    for u_id, role, vip_exp in rows:
        vip_remaining_hours = None
        if role == 'vip' and vip_exp:
            remaining = vip_exp - now
            vip_remaining_hours = round(remaining / 3600, 2) if remaining > 0 else 0
        statuses.append({"user_id": u_id, "role": role, "vip_remaining_hours": vip_remaining_hours})

    data = {
        "generated_at": datetime.now().isoformat(),
        "super_admin_id": SUPER_ADMIN_ID,
        "statuses": statuses
    }
    try:
        with open(STATUS_EXPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Status export faylini yozishda xatolik: {e}")
    return data

async def status_export_loop():
    while True:
        try:
            generate_status_export()
            logger.info("Status hisoboti avtomatik yangilandi (24 soatlik tsikl).")
        except Exception as e:
            logger.error(f"status_export_loop xatosi: {e}")
        await asyncio.sleep(86400)

async def db_backup_loop():
    """Har 24 soatda database.db faylini Super Adminga yuboradi (zaxira nusxa).
    Bot qayta deploy qilinganda yoki fayl yo'qolib qolganda, Super Admin
    menyusidagi 'Ma'lumotlarni tiklash' orqali shu faylni qayta yuklab qo'yishi mumkin."""
    while True:
        await asyncio.sleep(86400)
        try:
            with db_connect() as (conn, cursor):
                cursor.execute("PRAGMA wal_checkpoint(FULL);")
            await bot.send_document(
                SUPER_ADMIN_ID,
                FSInputFile(DB_PATH, filename="database.db"),
                caption=f"🗄 Avtomatik zaxira nusxa ({datetime.now().strftime('%Y-%m-%d %H:%M')}).\n"
                        f"Kerak bo'lsa, Super Admin menyusi → 'Ma'lumotlarni tiklash' orqali qayta yuklang."
            )
        except Exception as e:
            logger.error(f"db_backup_loop xatosi: {e}")

async def check_vip_expirations():
    while True:
        try:
            now = time.time()
            with db_connect() as (conn, cursor):
                cursor.execute("SELECT user_id FROM users WHERE role = 'vip' AND vip_expires_at > 0 AND vip_expires_at <= ?", (now,))
                expired_users = cursor.fetchall()

                if expired_users:
                    cursor.execute("UPDATE users SET role = 'user', vip_expires_at = 0 WHERE role = 'vip' AND vip_expires_at > 0 AND vip_expires_at <= ?", (now,))
                    conn.commit()

                # Muddati 24 soatdan kam qolgan VIP'lar uchun 1 martalik eslatma
                reminder_window_end = now + 86400
                cursor.execute(
                    "SELECT user_id FROM users WHERE role = 'vip' AND vip_expires_at > ? AND vip_expires_at <= ? AND (renewal_reminder_sent IS NULL OR renewal_reminder_sent = 0)",
                    (now, reminder_window_end)
                )
                soon_expiring = cursor.fetchall()
                if soon_expiring:
                    ids = [u[0] for u in soon_expiring]
                    cursor.executemany("UPDATE users SET renewal_reminder_sent = 1 WHERE user_id = ?", [(i,) for i in ids])
                    conn.commit()
            
            for (u_id,) in expired_users:
                try:
                    await bot.send_message(
                        u_id, 
                        "⚠️ Sizning VIP obunangiz vaqti tugadi. Avtomatik ravishda oddiy foydalanuvchi maqomiga o'tkazildingiz.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            for u_id in ids if soon_expiring else []:
                try:
                    renew_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Uzaytirish (Bozor)", callback_data="vip_shop")]
                    ])
                    await bot.send_message(
                        u_id,
                        "⏰ <b>Eslatma!</b>\n\nSizning VIP obunangiz muddati <b>24 soatdan kamroq</b> vaqt ichida tugaydi.\n"
                        "Imtiyozlaringizni yo'qotmaslik uchun hoziroq uzaytiring:",
                        reply_markup=renew_kb,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error in check_vip_expirations: {e}")
        
        await asyncio.sleep(10)

# =====================================================================
# UI: DYNAMIC KEYBOARDS
# =====================================================================
def get_main_keyboard(role: str):
    row2_middle = KeyboardButton(text="📤 Yuklash") if role in ['super_admin', 'assistant'] else KeyboardButton(text="🛒 Bozor")
    keyboard = [
        [KeyboardButton(text="🎬 Kino Qidirish"), KeyboardButton(text="💾 Saqlanganlar")],
        [KeyboardButton(text="📊 Profilim"), row2_middle, KeyboardButton(text="🎭 Janrlar")],
        [KeyboardButton(text="🎲 Mix Kino Xizmati"), KeyboardButton(text="💎 VIP Kinolar")],
    ]

    if role == 'user':
        keyboard.append([KeyboardButton(text="🔗 Referal va Promo kod")])
    elif role == 'vip':
        keyboard.append([KeyboardButton(text="🎁 VIP Sovg'a")])
    elif role in ['super_admin', 'assistant']:
        keyboard.append([KeyboardButton(text="👑 Super Admin & Yordamchi Menyusi")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def generate_movie_keyboard(code: str, likes: int, dislikes: int, is_saved_by_user: bool = False, is_vip_viewer: bool = False):
    save_text = "✅ Saqlangan" if is_saved_by_user else "💾 Saqlash"
    rows = [
        [
            InlineKeyboardButton(text="💬 Izohlar", callback_data=f"comments_{code}"),
            InlineKeyboardButton(text=save_text, callback_data=f"save_{code}")
        ]
    ]
    if not is_vip_viewer:
        rows.append([InlineKeyboardButton(text="⚡ Reklamasiz va kutishsiz tomosha qilish", callback_data="vip_shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_vip_upgrade_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ VIP bo'lish (Bozor)", callback_data="vip_shop")]
    ])

# =====================================================================
# MAJBURIY KANALGA OBUNA (faqat oddiy foydalanuvchilar uchun, REAL VAQTDA)
# =====================================================================
async def get_unsubscribed_channels(user_id: int):
    """
    Foydalanuvchi hali obuna bo'lmagan majburiy kanallar ro'yxatini qaytaradi.
    Agar kanal sozlamasida xatolik bo'lsa (bot admin emas va h.k.), o'sha kanal
    tekshiruvdan chetlab o'tiladi - shu tufayli noto'g'ri sozlama butun botni
    barcha foydalanuvchilar uchun qulflab qo'ymaydi.
    """
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT channel_id, channel_username, channel_title, invite_link FROM required_channels")
        channels = cursor.fetchall()

    missing = []
    for ch_id, ch_username, ch_title, invite_link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ('member', 'administrator', 'creator'):
                continue
            # Yopiq/maxfiy kanalda "har bir a'zoni tasdiqlash" sozlamasi yoqilgan bo'lishi
            # mumkin - bunda foydalanuvchi darhol to'liq a'zo bo'lib qolmaydi, faqat
            # qo'shilish so'rovi yuboradi. Shu holatni ham YETARLI deb hisoblaymiz -
            # to'liq a'zo bo'lishini talab qilmaymiz, faqat so'rov yuborilganini tekshiramiz.
            if has_join_request(user_id, ch_id):
                continue
            missing.append((ch_id, ch_username, ch_title, invite_link))
        except Exception as e:
            logger.warning(f"Kanal tekshiruvida xatolik ({ch_id}): {e}")
            continue
    return missing

def has_join_request(user_id: int, channel_id) -> bool:
    """Foydalanuvchi shu kanalga qo'shilish so'rovi yuborganmi (chat_join_request
    orqali qayd etilgan) - yopiq/maxfiy kanallar uchun a'zolik o'rniga ishlatiladi."""
    with db_connect() as (conn, cursor):
        cursor.execute(
            "SELECT 1 FROM channel_join_requests WHERE user_id = ? AND channel_id = ?",
            (user_id, str(channel_id))
        )
        return cursor.fetchone() is not None

def set_pending_watch_code(user_id: int, code: str):
    """Foydalanuvchi chuqur havola (watch_{code}) orqali kirgan, lekin hali kanallarga
    obuna bo'lmagan bo'lsa, so'ragan kino kodini eslab qolamiz - obuna tasdiqlangach
    aynan shu kino "kelgan joyidan" avtomatik chiqariladi."""
    with db_connect() as (conn, cursor):
        cursor.execute("UPDATE users SET pending_watch_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()

def pop_pending_watch_code(user_id: int):
    """Eslab qolingan kino kodini qaytaradi va bazadan tozalaydi (bir martalik)."""
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT pending_watch_code FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        code = row[0] if row else None
        if code:
            cursor.execute("UPDATE users SET pending_watch_code = NULL WHERE user_id = ?", (user_id,))
            conn.commit()
    return code

def build_subscription_prompt_keyboard(missing_channels):
    rows = []
    for ch_id, ch_username, ch_title, invite_link in missing_channels:
        if invite_link:
            url = invite_link
        else:
            uname = ch_username.lstrip("@") if ch_username else None
            url = f"https://t.me/{uname}" if uname else "https://t.me/telegram"
        rows.append([InlineKeyboardButton(text=f"📢 {ch_title or ch_username or ch_id}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def send_subscription_prompt(chat_id: int, missing_channels):
    text = (
        "🔐 <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling!</b>\n\n"
        "Obuna bo'lgach, pastdagi <b>✅ Tasdiqlash</b> tugmasini bosing."
    )
    try:
        await bot.send_message(chat_id, text, reply_markup=build_subscription_prompt_keyboard(missing_channels), parse_mode="HTML")
    except Exception:
        pass

@dp.chat_join_request()
async def handle_chat_join_request(update: ChatJoinRequest):
    """Foydalanuvchi yopiq/maxfiy kanalga qo'shilish so'rovi yuborganda ishga tushadi
    (bot o'sha kanalda admin bo'lgani uchun bu event avtomatik keladi, qo'shimcha
    sozlash shart emas). So'rov yuborilgani bazaga yoziladi - shu orqali kanalda
    "har bir a'zoni tasdiqlash" yoqilgan bo'lsa ham, foydalanuvchi botdan
    foydalanish uchun to'liq a'zo bo'lishini kutish shart bo'lmaydi."""
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT OR REPLACE INTO channel_join_requests (user_id, channel_id, requested_at) VALUES (?, ?, ?)",
                (update.from_user.id, str(update.chat.id), time.time())
            )
            conn.commit()
    except Exception as e:
        logger.error(f"chat_join_request handler xatosi: {e}")

class SubscriptionMiddleware(BaseMiddleware):
    """
    Oddiy foydalanuvchilar (role == 'user') uchun barcha majburiy kanallarga
    obuna bo'lmaguncha, hech qanday boshqa menyu/funksiyaga o'ta olmasligini
    ta'minlaydigan global tekshiruv. VIP, Yordamchi va Super Admin uchun
    ushbu tekshiruv qo'llanilmaydi.
    """
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user_obj = data.get("event_from_user")
        if user_obj:
            role = get_user_role(user_obj.id)
            if role == 'user':
                is_start_cmd = isinstance(event, Message) and event.text and event.text.startswith('/start')
                is_check_cb = isinstance(event, CallbackQuery) and event.data == "check_subscription"
                if not is_start_cmd and not is_check_cb:
                    missing = await get_unsubscribed_channels(user_obj.id)
                    if missing:
                        target_chat_id = event.chat.id if isinstance(event, Message) else event.from_user.id
                        await send_subscription_prompt(target_chat_id, missing)
                        if isinstance(event, CallbackQuery):
                            try:
                                await event.answer()
                            except Exception:
                                pass
                        return
        return await handler(event, data)

@dp.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery):
    missing = await get_unsubscribed_channels(callback.from_user.id)
    if missing:
        await callback.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=build_subscription_prompt_keyboard(missing))
        except Exception:
            pass
        return
    role = get_user_role(callback.from_user.id)
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Agar foydalanuvchi kanaldagi "Tomosha qilish" tugmasi orqali (chuqur havola bilan)
    # kelgan bo'lsa - so'ragan kinosi aynan shu yerda, kelgan joyidan avtomatik chiqariladi.
    pending_code = pop_pending_watch_code(callback.from_user.id)
    if pending_code:
        await callback.answer()
        await bot.send_message(
            callback.from_user.id,
            "✅ Obuna tasdiqlandi!",
            reply_markup=get_main_keyboard(role)
        )
        await deliver_movie_by_code(callback.from_user.id, pending_code)
        return

    await bot.send_message(
        callback.from_user.id,
        "✅ Obuna tasdiqlandi! Botdan bemalol foydalanishingiz mumkin.",
        reply_markup=get_main_keyboard(role)
    )
    await callback.answer()

# Middleware'ni ro'yxatdan o'tkazish - endi barcha message va callback'lar shu tekshiruvdan o'tadi
dp.message.outer_middleware(SubscriptionMiddleware())
dp.callback_query.outer_middleware(SubscriptionMiddleware())

async def deliver_movie(chat_id: int, code: str, role: str, is_saved: bool, likes: int, dislikes: int, extra_rows: list = None, skip_wait: bool = False) -> bool:
    """
    Kinoni foydalanuvchiga yetkazadi.
    - VIP/Admin/Yordamchi uchun: DARHOL, kutishsiz yuboriladi.
    - Oddiy foydalanuvchi uchun: ~3 soniyalik qidiruv animatsiyasi ko'rsatiladi
      (sun'iy kechiktirish emas, "kino qidirilmoqda" tuyg'usini beradi) va
      "Cheklovni olib tashlash" (VIP) tugmasi bilan birga chiqadi - bu VIP
      olishga bo'lgan hissiyotni kuchaytiradi.
    Muvaffaqiyatli yuborilsa True, kino topilmasa False qaytaradi.
    """
    is_vip_viewer = role in ['vip', 'assistant', 'super_admin']
    keyboard = generate_movie_keyboard(code, likes, dislikes, is_saved, is_vip_viewer=is_vip_viewer)
    if extra_rows:
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard.inline_keyboard + extra_rows)
    wait_msg = None

    if not is_vip_viewer and not skip_wait:
        remove_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔓 Cheklovni olib tashlash (Bozor)", callback_data="vip_shop")]
        ])
        search_frames = [
            "🔍 Kino qidirilmoqda",
            "🔍 Kino qidirilmoqda.",
            "🔍 Kino qidirilmoqda..",
            "🔍 Kino qidirilmoqda...",
        ]
        try:
            wait_msg = await bot.send_message(
                chat_id,
                f"{search_frames[0]}\n\n"
                "👑 <b>VIP a'zolar</b> kinoni <b>bir zumda</b>, kutishsiz oladi!",
                reply_markup=remove_kb, parse_mode="HTML"
            )
        except Exception:
            wait_msg = None

        for frame in search_frames[1:]:
            await asyncio.sleep(1)
            if wait_msg:
                try:
                    await wait_msg.edit_text(
                        f"{frame}\n\n"
                        "👑 <b>VIP a'zolar</b> kinoni <b>bir zumda</b>, kutishsiz oladi!",
                        reply_markup=remove_kb, parse_mode="HTML"
                    )
                except Exception:
                    pass

    try:
        protect = role not in ["vip", "super_admin", "assistant"]
        await bot.copy_message(chat_id=chat_id, from_chat_id=CHANNEL_ID, message_id=resolve_secret_message_id(code), reply_markup=keyboard, protect_content=protect)
        register_movie_view(code)
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
        return True
    except TelegramBadRequest:
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
        return False

async def deliver_movie_by_code(user_id: int, code_text: str) -> bool:
    """Kanaldagi "Tomosha qilish" tugmasi (chuqur havola: /start watch_{code}) orqali
    kelgan foydalanuvchiga kodi bo'yicha kinoni to'g'ridan-to'g'ri botning shaxsiy
    chatida yetkazadi. VIP-kino bo'lsa, oddiy foydalanuvchidan 1 Olmos yechiladi
    (xuddi qo'lda kod kiritilgandagi kabi). Muvaffaqiyatli bo'lsa True qaytaradi."""
    if not code_text.isdigit():
        return False
    code_int = int(code_text)
    role = get_user_role(user_id)

    if is_vip_movie(str(code_int)) and role not in ['vip', 'assistant', 'super_admin']:
        if not spend_diamonds(user_id, 1):
            try:
                await bot.send_message(
                    user_id,
                    "🔒 Bu VIP kino turiga kiradi. Ko'rish uchun 1 ta Olmos kerak, lekin hisobingizda olmos yo'q.\n"
                    "Olmosni do'stlaringizni taklif qilish yoki promo kod orqali qo'lga kiritishingiz mumkin:",
                    reply_markup=get_vip_upgrade_keyboard()
                )
            except Exception:
                pass
            return False

    likes, dislikes, views = get_movie_stats(str(code_int))
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (user_id, str(code_int)))
        is_saved = cursor.fetchone() is not None

    ok = await deliver_movie(user_id, str(code_int), role, is_saved, likes, dislikes)
    if not ok:
        try:
            await bot.send_message(user_id, f"❌ {code_int}-kodli kino topilmadi.")
        except Exception:
            pass
    return ok

# =====================================================================
# START & REFERRAL LOGIC
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id):
        await message.answer("⚠️ Sizga vaqtinchalik cheklov qo'yilgan. Bot funksiyalaridan foydalana olmaysiz.")
        return
        
    update_last_active(user_id)
    args = message.text.split()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = cursor.fetchone()
    
    if not exists:
        cursor.execute("INSERT INTO users (user_id, full_name, username, role, main_coin) VALUES (?, ?, ?, 'user', 25)", 
                       (user_id, message.from_user.full_name, message.from_user.username))
        conn.commit()
        
        try:
            uname_text = f"@{message.from_user.username}" if message.from_user.username else "mavjud emas"
            await bot.send_message(
                SUPER_ADMIN_ID,
                f"👤 <b>Yangi foydalanuvchi botga qo'shildi!</b>\n\n"
                f"Ismi: <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a>\n"
                f"ID: <code>{user_id}</code>\n"
                f"Username: {uname_text}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                if referrer_id != user_id:
                    cursor.execute("SELECT 1 FROM referral_logs WHERE referrer_id = ? AND referred_id = ?", (referrer_id, user_id))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO referral_logs (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user_id))
                        # Har 1 ta referalga: 1 olmos (1 olmos = 1 referal = 1 VIP kino ko'rish huquqi)
                        cursor.execute(
                            "UPDATE users SET referrals_count = referrals_count + 1, diamonds = diamonds + 1 WHERE user_id = ?",
                            (referrer_id,)
                        )
                        conn.commit()
                        try:
                            await bot.send_message(
                                referrer_id,
                                "🎉 Tabriklaymiz! Referal havolangiz orqali yangi do'stingiz qo'shildi.\n"
                                "Sizga <b>1 ta Olmos</b> berildi (1 ta VIP-kinoni bepul ko'rish huquqi)!",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
            except ValueError:
                pass
    conn.close()

    role = get_user_role(user_id)

    # Kanaldagi "Tomosha qilish" / "VIP Tomosha" tugmasi chuqur havola
    # (https://t.me/BOT?start=watch_{code}) orqali bosilganda shu yerga tushadi.
    # Foydalanuvchi asosiy kanalda qolmaydi - to'g'ridan-to'g'ri botga yo'naltiriladi
    # va so'ragan kinosi shu yerdan chiqariladi.
    payload = args[1] if len(args) > 1 else None
    if payload and payload.startswith("watch_"):
        watch_code = payload[len("watch_"):]
        if watch_code.isdigit():
            # Obuna talabi faqat oddiy foydalanuvchilar uchun (VIP/admin uchun shart emas).
            if role == 'user':
                missing = await get_unsubscribed_channels(user_id)
                if missing:
                    set_pending_watch_code(user_id, watch_code)
                    await send_subscription_prompt(message.chat.id, missing)
                    return
            # Butunlay yangi foydalanuvchiga asosiy menyu klaviaturasi ham chiqsin
            # (qayta-qayta bosadigan eski foydalanuvchilarga ortiqcha xabar yuborilmaydi).
            if not exists:
                await message.answer(
                    f"Assalomu alaykum, <b>{message.from_user.first_name}</b>! 🎬\n"
                    f"Kinolar botiga xush kelibsiz.",
                    reply_markup=get_main_keyboard(role), parse_mode="HTML"
                )
            await deliver_movie_by_code(user_id, watch_code)
            return

    text = (
        f"Assalomu alaykum, <b>{message.from_user.first_name}</b>!\n"
        f"Kinolar botiga xush kelibsiz. Quyidagi menyudan foydalaning:"
    )

    await message.answer(text, reply_markup=get_main_keyboard(role), parse_mode="HTML")

# =====================================================================
# MENU HANDLERS
# =====================================================================
@dp.message(F.text == "🎬 Kino Qidirish")
async def msg_search_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    role = get_user_role(user_id)
    await message.answer("🎬 Kino kodini yuboring (Masalan: 15):", reply_markup=get_main_keyboard(role))
    await state.set_state(UserState.waiting_for_movie_code)

@dp.message(F.text == "📊 Profilim")
async def msg_status_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    role = get_user_role(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT referrals_count, main_coin, daily_coin, diamonds, joined_date, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    
    ref_count, main_coin, daily_coin, diamonds, joined_date, vip_expires = res
    
    role_names = {'user': '👤 Oddiy foydalanuvchi', 'vip': '👑 VIP a\'zo', 'assistant': '🛠 Yordamchi Admin', 'super_admin': '👑 Super Admin'}
    daraja_line = role_names.get(role, 'Noma`lum')
    vip_time = ""
    if role == 'vip' and vip_expires:
        left = int(vip_expires - time.time())
        if left > 0:
            days_left = left // 86400
            daraja_line = f"👑 VIP a'zo | Amal qiladi: {days_left} kun"
            vip_time = f"\n⏳ Aniq qolgan vaqt: {days_left} kun, {(left % 86400) // 3600} soat, {(left % 3600) // 60} daqiqa"

    trust_line = ""

    profile_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Bozor (VIP sotib olish)", callback_data="vip_shop")]
    ])

    await message.answer(
        f"📊 <b>Profil Ma'lumotlari:</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👑 Daraja: {daraja_line}{vip_time}{trust_line}\n"
        f"👥 Taklif qilingan do'stlar: {ref_count} ta\n"
        f"🪙 Asosiy VIP Coin: {main_coin} ta\n"
        f"🎟 Kunlik VIP Coin: {daily_coin} ta\n"
        f"💎 Olmos: {diamonds} ta\n"
        f"📅 Ro'yxatdan o'tgan: {joined_date}", 
        reply_markup=profile_kb, 
        parse_mode="HTML"
    )

@dp.message(F.text == "🔗 Referal va Promo kod")
async def msg_referral_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    role = get_user_role(user_id)
    if role != 'user':
        await message.answer("Bu menyu faqat oddiy foydalanuvchilar uchun mo'ljallangan.")
        return

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Promo koddan foydalanish", callback_data="use_promo_code")]
    ])
    
    await message.answer(
        f"🔗 <b>Sizning shaxsiy referal havolangiz:</b>\n<code>{ref_link}</code>\n\n"
        f"👤 1 ta do'stni taklif qiling = <b>1 ta VIP kino tomosha qilish huquqi</b> (1 Olmos) qo'lga kiriting!",
        reply_markup=inline_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "use_promo_code")
async def cb_use_promo_code(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🎁 Promo kodni kiriting:")
    await state.set_state(UserState.waiting_for_promo_input)
    await callback.answer()

@dp.message(UserState.waiting_for_promo_input)
async def process_promo_input(message: Message, state: FSMContext):
    promo = message.text.strip().upper()
    user_id = message.from_user.id

    if get_user_role(user_id) in ('vip', 'assistant', 'super_admin'):
        await message.answer("⚠️ Promo kod faqat oddiy foydalanuvchilar uchun mo'ljallangan.")
        await state.clear()
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT reward_coins, max_uses, used_count, promo_type, vip_hours FROM promo_codes WHERE code_name = ?", (promo,))
    promo_data = cursor.fetchone()
    
    if not promo_data:
        await message.answer("❌ Bunday promo kod mavjud emas yoki xato kiritildi.")
        conn.close()
        return
        
    reward, max_uses, used_count, promo_type, vip_hours = promo_data
    
    if used_count >= max_uses:
        await message.answer("❌ Kechirasiz, bu promo kodning ishlatish limiti tugagan va yaroqsiz holatda!")
        conn.close()
        return

    cursor.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND code_name = ?", (user_id, promo))
    if cursor.fetchone():
        await message.answer("⚠️ Siz bu promo koddan avval foydalangansiz! Har bir akkaunt faqat 1 marta ishlatishi mumkin.")
        conn.close()
        return
        
    cursor.execute("INSERT INTO used_promos (user_id, code_name) VALUES (?, ?)", (user_id, promo))
    cursor.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code_name = ?", (promo,))

    if promo_type == 'vip_duration' and vip_hours:
        cursor.execute("SELECT role, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        cur_role, cur_exp = cursor.fetchone()
        base_time = cur_exp if (cur_role == 'vip' and cur_exp and cur_exp > time.time()) else time.time()
        new_expiry = base_time + (vip_hours * 3600)
        cursor.execute("UPDATE users SET role = 'vip', vip_expires_at = ?, renewal_reminder_sent = 0 WHERE user_id = ?", (new_expiry, user_id))
        conn.commit()
        conn.close()
        await state.clear()
        role = get_user_role(user_id)
        await message.answer(f"✅ Tabriklaymiz! Promo kod muvaffaqiyatli ishlatildi.\n👑 Sizga <b>{vip_hours} soatlik VIP</b> maqomi berildi!", reply_markup=get_main_keyboard(role), parse_mode="HTML")
        return

    cursor.execute("UPDATE users SET diamonds = diamonds + ? WHERE user_id = ?", (reward, user_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    role = get_user_role(user_id)
    await message.answer(f"✅ Tabriklaymiz! Promo kod muvaffaqiyatli ishlatildi.\n💎 Sizga {reward} ta Olmos berildi!", reply_markup=get_main_keyboard(role))

@dp.message(F.text == "💾 Saqlanganlar")
async def msg_saved_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    if not use_vip_coin_for_service(user_id):
        await message.answer(
            "⚠️ Bu xizmatdan foydalanish uchun hisobingizda VIP coin mavjud emas.\n"
            "VIP olish yoki coin yig'ish uchun quyidagi tugmani bosing:",
            reply_markup=get_vip_upgrade_keyboard()
        )
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT movie_code FROM saved_movies WHERE user_id = ? ORDER BY saved_at DESC LIMIT 50", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("Sizda saqlangan kinolar yo'q.")
        return
        
    buttons = [[InlineKeyboardButton(text=f"🎬 {code}-kino", callback_data=f"watch_{code}")] for code, in rows]
    await message.answer("💾 Saqlangan kinolar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# =====================================================================
# TOP-10 KINOLAR (eng ko'p ko'rilgan kinolar reytingi)
# =====================================================================
# (🏆 Top-10 Kinolar bo'limi va tugmasi so'rovga ko'ra butunlay olib tashlandi)

# =====================================================================
# VIP KINOLAR MENYUSI (faqat "VIP" deb belgilangan kinolar ro'yxati)
# =====================================================================
@dp.message(F.text == "💎 VIP Kinolar")
async def msg_vip_movies_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    role = get_user_role(user_id)

    if role not in ['vip', 'assistant', 'super_admin']:
        await message.answer(
            "💎 <b>VIP Kinolar</b> bo'limi faqat VIP a'zolar uchun mavjud!\n\n"
            "Bu yerda eksklyuziv, faqat VIP'lar uchun maxsus belgilangan kinolar to'plangan.\n"
            "👑 VIP bo'lib, ushbu maxsus kinolarga kirish huquqiga ega bo'ling:",
            reply_markup=get_vip_upgrade_keyboard(),
            parse_mode="HTML"
        )
        return

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT code FROM vip_movies ORDER BY code DESC LIMIT 30")
        rows = [r[0] for r in cursor.fetchall()]

    if not rows:
        await message.answer("💎 Hozircha VIP kinolar ro'yxati bo'sh. Tez orada yangilanadi!")
        return

    buttons = [[InlineKeyboardButton(text=f"💎 {code}-kino", callback_data=f"watch_{code}")] for code in rows]
    await message.answer(
        f"💎 <b>VIP Kinolar to'plami</b> (jami {len(rows)} ta):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


# =====================================================================
# GENRES REAL LOGIC
# =====================================================================
@dp.message(F.text == "🎭 Janrlar")
async def msg_genres_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    if not use_vip_coin_for_service(user_id):
        await message.answer(
            "⚠️ Bu xizmatdan foydalanish uchun hisobingizda VIP coin mavjud emas.\n"
            "VIP olish uchun quyidagi tugmani bosing:",
            reply_markup=get_vip_upgrade_keyboard()
        )
        return
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=g, callback_data=f"usergenre_{g}")] for g in GENRES_LIST])
    await message.answer("🎭 Kino janrini tanlang:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("usergenre_"))
async def cb_user_genre(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    genre = callback.data.split("_", 1)[1]
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT code FROM movies_meta WHERE genre = ?", (genre,))
        rows = [r[0] for r in cursor.fetchall()]
    
    if not rows:
        await callback.answer(f"Hozircha '{genre}' janrida kinolar topilmadi.", show_alert=True)
        return
        
    random.shuffle(rows)
    target_code = rows[0]
    
    likes, dislikes, views = get_movie_stats(target_code)
    role = get_user_role(callback.from_user.id)
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (callback.from_user.id, target_code))
        is_saved = cursor.fetchone() is not None
    
    extra = [[InlineKeyboardButton(text="🔄 Boshqa kinoga almashtirish", callback_data=f"genrenext_{genre}")]]
    
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    ok = await deliver_movie(callback.from_user.id, target_code, role, is_saved, likes, dislikes, extra_rows=extra)
    if not ok:
        await callback.answer("Kinoni yuborishda xatolik yuz berdi.", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("genrenext_"))
async def cb_genre_next(callback: CallbackQuery):
    genre = callback.data.split("_", 1)[1]
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT code FROM movies_meta WHERE genre = ?", (genre,))
        rows = [r[0] for r in cursor.fetchall()]
    
    if not rows:
        await callback.answer("Kinolar tugadi.", show_alert=True)
        return
        
    random.shuffle(rows)
    target_code = rows[0]
    likes, dislikes, views = get_movie_stats(target_code)
    role = get_user_role(callback.from_user.id)
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (callback.from_user.id, target_code))
        is_saved = cursor.fetchone() is not None
    
    extra = [[InlineKeyboardButton(text="🔄 Boshqa kinoga almashtirish", callback_data=f"genrenext_{genre}")]]
    
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    ok = await deliver_movie(callback.from_user.id, target_code, role, is_saved, likes, dislikes, extra_rows=extra)
    if not ok:
        await callback.answer("Kino topilmadi.", show_alert=True)
    await callback.answer()

# =====================================================================
# MIX MOVIE SERVICE & VIP REQUEST
# =====================================================================
async def send_mix_movie(chat_id: int, callback: CallbackQuery = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM movie_stats")
    all_movies = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    if not all_movies:
        text = "Bazada yetarli kinolar mavjud emas."
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await bot.send_message(chat_id, text)
        return
        
    random.shuffle(all_movies)
    success = False
    role = get_user_role(chat_id)
    
    for random_code in all_movies[:10]:
        likes, dislikes, views = get_movie_stats(str(random_code))
        with db_connect() as (conn, cursor):
            cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (chat_id, str(random_code)))
            is_saved = cursor.fetchone() is not None
        
        if callback:
            try:
                await callback.message.delete()
            except Exception:
                pass
        success = await deliver_movie(chat_id, str(random_code), role, is_saved, likes, dislikes, skip_wait=True)
        if success:
            break
            
    if not success:
        err_msg = "Tasodifiy kino qidirilmoqda, ammo kanalda mavjud aktiv post topilmadi."
        if callback:
            await callback.answer(err_msg, show_alert=True)
        else:
            await bot.send_message(chat_id, err_msg)
    elif callback:
        await callback.answer()

@dp.message(F.text == "🎲 Mix Kino Xizmati")
async def msg_mix_movie(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    
    if not use_vip_coin_for_service(user_id):
        await message.answer(
            "⚠️ Bu xizmatdan foydalanish uchun hisobingizda VIP coin mavjud emas.\n"
            "VIP olish uchun quyidagi tugmani bosing:",
            reply_markup=get_vip_upgrade_keyboard()
        )
        return
        
    await send_mix_movie(message.chat.id)

class VipGiftState(StatesGroup):
    waiting_for_gift_id = State()
    waiting_for_gift_days = State()

@dp.message(F.text == "🎁 VIP Sovg'a")
async def msg_vip_gift_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)

    role = get_user_role(user_id)
    if role != 'vip':
        await message.answer("Bu funksiya faqat VIP foydalanuvchilar uchun.")
        return

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
    remaining_days = 0
    if res and res[0]:
        remaining_days = int((res[0] - time.time()) // 86400)

    if remaining_days < 2:
        await message.answer(
            f"⚠️ Sovg'a qilish uchun sizda kamida 2 kunlik VIP bo'lishi kerak "
            f"(o'zingizda kamida 1 kun qolishi shart). Sizda: {remaining_days} kun qoldi."
        )
        return

    await state.update_data(max_gift_days=remaining_days - 1)
    await message.answer(
        f"🎁 <b>VIP Sovg'a</b>\n\nSizda <b>{remaining_days} kun</b> VIP qoldi, shundan "
        f"<b>{remaining_days - 1} kunigacha</b> boshqa foydalanuvchiga sovg'a qilishingiz mumkin "
        f"(o'zingizda kamida 1 kun qolishi shart).\n\nSovg'a qilmoqchi bo'lgan foydalanuvchining ID raqamini yuboring:",
        parse_mode="HTML"
    )
    await state.set_state(VipGiftState.waiting_for_gift_id)

@dp.message(VipGiftState.waiting_for_gift_id)
async def process_vip_gift_id(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Faqat raqamli ID kiriting.")
        return
    target_id = int(message.text.strip())
    if target_id == message.from_user.id:
        await message.answer("❌ O'zingizga sovg'a qila olmaysiz.")
        return
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,))
        exists = cursor.fetchone()
    if not exists:
        await message.answer("❌ Bunday ID li foydalanuvchi topilmadi.")
        return
    data = await state.get_data()
    await state.update_data(gift_target_id=target_id)
    await message.answer(f"Nechta kun sovg'a qilmoqchisiz? (Maksimal: {data.get('max_gift_days')} kun)")
    await state.set_state(VipGiftState.waiting_for_gift_days)

@dp.message(VipGiftState.waiting_for_gift_days)
async def process_vip_gift_days(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Faqat raqam kiriting.")
        return
    days = int(message.text.strip())
    data = await state.get_data()
    max_days = data.get('max_gift_days', 0)
    target_id = data.get('gift_target_id')
    user_id = message.from_user.id

    if days < 1 or days > max_days:
        await message.answer(f"❌ 1 dan {max_days} gachagina kun sovg'a qilishingiz mumkin.")
        return

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        my_exp = cursor.fetchone()[0] or time.time()
        cursor.execute("UPDATE users SET vip_expires_at = vip_expires_at - ? WHERE user_id = ?", (days * 86400, user_id))

        cursor.execute("SELECT role, vip_expires_at FROM users WHERE user_id = ?", (target_id,))
        t_role, t_exp = cursor.fetchone()
        base_time = t_exp if (t_role == 'vip' and t_exp and t_exp > time.time()) else time.time()
        new_expiry = base_time + (days * 86400)
        cursor.execute("UPDATE users SET role = 'vip', vip_expires_at = ?, renewal_reminder_sent = 0 WHERE user_id = ?", (new_expiry, target_id))
        cursor.execute("INSERT INTO vip_gift_logs (from_id, to_id, days, created_at) VALUES (?, ?, ?, ?)", (user_id, target_id, days, time.time()))
        conn.commit()

    role = get_user_role(user_id)
    await message.answer(f"✅ {days} kunlik VIP muvaffaqiyatli sovg'a qilindi!", reply_markup=get_main_keyboard(role))
    try:
        await bot.send_message(
            target_id,
            f"🎁 Sizga <a href='tg://user?id={user_id}'>bir foydalanuvchi</a> tomonidan <b>{days} kunlik VIP</b> sovg'a qilindi!",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await state.clear()

# =====================================================================
# SUPER ADMIN PANEL — asosiy menyu
# =====================================================================
def build_super_admin_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="👥 Yordamchilarni boshqarish (Max 3 ta)", callback_data="sa_manage_assistants")],
        [InlineKeyboardButton(text="💎 Olmos boshqarish (ID orqali)", callback_data="sa_manage_coins")],
        [InlineKeyboardButton(text="🔒 VIP kodini belgilash", callback_data="admin_set_vip_code")],
        [InlineKeyboardButton(text="🎭 Kinoga janr belgilash", callback_data="admin_set_genre")],
        [InlineKeyboardButton(text="📢 Ommaviy xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎁 Promo Kod Yaratish (Olmos)", callback_data="sa_promo_codes")],
        [InlineKeyboardButton(text="⏱ Vaqtinchalik cheklov berish (Mute)", callback_data="sa_restrict_user")],
        [InlineKeyboardButton(text="📈 Bot statistikasi (Akauntlar va Linklar)", callback_data="sa_bot_stats")],
        [InlineKeyboardButton(text="📋 Bot holati (Server)", callback_data="sa_bot_health")],
        [InlineKeyboardButton(text="🔐 Majburiy Obuna Kanallari", callback_data="sa_channels_menu")],
        [InlineKeyboardButton(text="📊 Status hisoboti (Export/Import)", callback_data="sa_status_report")],
        [InlineKeyboardButton(text="🐞 Xatoliklar hisoboti", callback_data="sa_error_report")]
    ]
    if role == 'super_admin':
        buttons.append([InlineKeyboardButton(text="👤 ID orqali VIP berish", callback_data="sa_grant_vip")])
        buttons.append([InlineKeyboardButton(text="🛒 Bozorni boshqarish", callback_data="sa_market_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_super_admin_panel(target, role: str, as_callback: bool):
    text = f"👑 <b>Boshqaruv Paneli</b>\nDarajangiz: <b>{role.upper()}</b>"
    kb = build_super_admin_keyboard(role)
    if as_callback:
        await target.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "sa_menu")
async def cb_sa_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = get_user_role(callback.from_user.id)
    if role not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.answer()
    await send_super_admin_panel(callback, role, as_callback=True)

@dp.message(F.text == "👑 Super Admin & Yordamchi Menyusi")
async def msg_super_admin_menu(message: Message, state: FSMContext):
    await state.clear()
    role = get_user_role(message.from_user.id)
    if role not in ['super_admin', 'assistant']:
        return
    await send_super_admin_panel(message, role, as_callback=False)

@dp.callback_query(F.data == "sa_bot_stats")
async def cb_bot_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    if get_user_role(user_id) not in ['super_admin', 'assistant']: return
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT user_id, full_name FROM users ORDER BY joined_date DESC")
        users = cursor.fetchall()
    
    total_users = len(users)
    
    header = f"📊 <b>Akauntlar statistikasi (Jami: {total_users} ta):</b>\n\n"
    user_lines = []
    for u_id, full_name in users:
        name_clean = full_name.replace("<", "&lt;").replace(">", "&gt;") if full_name else "Foydalanuvchi"
        user_lines.append(f"• <a href='tg://user?id={u_id}'>{name_clean}</a> (<code>{u_id}</code>)")
        
    # Telegramning 4096 belgi limitidan oshib ketmasligi uchun xabarni bo'laklarga bo'lib yuboramiz
    chunk = header
    for line in user_lines:
        if len(chunk) + len(line) + 1 > 3800:
            await callback.message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await callback.message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
    if not user_lines:
        await callback.message.answer(header + "Hozircha foydalanuvchilar yo'q.", parse_mode="HTML")
        
    await callback.answer()

# =====================================================================
# BOT HOLATI (server ishlab turgani, xatolar soni, ishga tushgan vaqti)
# =====================================================================
@dp.callback_query(F.data == "sa_bot_health")
async def cb_bot_health(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return

    uptime_sec = int(time.time() - BOT_START_TIME)
    days = uptime_sec // 86400
    hours = (uptime_sec % 86400) // 3600
    minutes = (uptime_sec % 3600) // 60

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'vip'")
        total_vip = cursor.fetchone()[0]

    await callback.message.answer(
        f"📋 <b>Bot Holati</b>\n\n"
        f"🟢 Holat: <b>Ishlamoqda</b>\n"
        f"⏱ Ishga tushganiga: <b>{days} kun {hours} soat {minutes} daqiqa</b>\n"
        f"⚠️ Ushbu ishga tushishdan beri xatoliklar soni: <b>{ERROR_COUNT}</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b> ta\n"
        f"👑 Faol VIP'lar: <b>{total_vip}</b> ta",
        parse_mode="HTML"
    )
    await callback.answer()

# =====================================================================
# MAJBURIY OBUNA KANALLARINI BOSHQARISH (Super Admin Menyusi)
# =====================================================================
@dp.callback_query(F.data == "sa_channels_menu")
async def cb_channels_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT id, channel_username, channel_title FROM required_channels")
        channels = cursor.fetchall()

    text = "🔐 <b>Majburiy Obuna Kanallari</b>\n\n"
    if channels:
        for cid, uname, title in channels:
            text += f"• {title or uname} ({uname})\n"
    else:
        text += "Hozircha kanal qo'shilmagan."

    kb = [
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="ch_add")],
    ]
    if channels:
        kb.append([InlineKeyboardButton(text="➖ Kanal olib tashlash", callback_data="ch_remove_list")])

    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "ch_add")
async def cb_channel_add(callback: CallbackQuery, state: FSMContext):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.message.answer(
        "📢 Kanalni yuboring:\n"
        "• Ochiq kanal uchun — username (masalan: <code>@mening_kanalim</code>)\n"
        "• Yopiq/maxfiy kanal uchun — ID (masalan: <code>-1001234567890</code>)\n\n"
        "⚠️ <b>Muhim:</b> Bot ushbu kanalda <b>admin</b> bo'lishi shart, aks holda obunani tekshira olmaydi!",
        parse_mode="HTML"
    )
    await state.set_state(SuperAdminState.waiting_for_channel_input)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_channel_input)
async def process_channel_add(message: Message, state: FSMContext):
    raw = message.text.strip()
    is_id_format = raw.lstrip("-").isdigit()

    if not raw.startswith("@") and not is_id_format:
        await message.answer("❌ Noto'g'ri format! @username yoki -100 bilan boshlanuvchi ID kiriting.")
        return

    target = int(raw) if is_id_format else raw
    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await message.answer(f"❌ Kanal topilmadi yoki bot u yerda admin emas.\nXatolik: {e}")
        return

    invite_link = None
    if is_id_format:
        # Yopiq kanal — foydalanuvchilar qo'shilishi uchun taklif havolasi yaratamiz
        try:
            link_obj = await bot.create_chat_invite_link(chat.id)
            invite_link = link_obj.invite_link
        except Exception as e:
            await message.answer(
                f"⚠️ Kanal topildi, lekin taklif havolasi yaratib bo'lmadi (bot admin emasmi?): {e}\n"
                "Kanal baribir qo'shildi, lekin foydalanuvchilar unga qo'shila olmasligi mumkin."
            )

    with db_connect() as (conn, cursor):
        cursor.execute(
            "INSERT INTO required_channels (channel_id, channel_username, channel_title, invite_link) VALUES (?, ?, ?, ?)",
            (str(chat.id), raw if not is_id_format else None, chat.title, invite_link)
        )
        conn.commit()

    role = get_user_role(message.from_user.id)
    await message.answer(f"✅ <b>{chat.title}</b> majburiy obuna ro'yxatiga qo'shildi!", reply_markup=get_main_keyboard(role), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "ch_remove_list")
async def cb_channel_remove_list(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT id, channel_username, channel_title FROM required_channels")
        channels = cursor.fetchall()

    if not channels:
        await callback.answer("Ro'yxat bo'sh.", show_alert=True)
        return

    kb = [[InlineKeyboardButton(text=f"❌ {title or uname}", callback_data=f"ch_del_{cid}")] for cid, uname, title in channels]
    await callback.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("ch_del_"))
async def cb_channel_delete(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    ch_id = callback.data.split("_", 2)[2]
    with db_connect() as (conn, cursor):
        cursor.execute("DELETE FROM required_channels WHERE id = ?", (ch_id,))
        conn.commit()
    await callback.message.answer("✅ Kanal ro'yxatdan olib tashlandi.")
    await callback.answer()


# =====================================================================
# STATUS HISOBOTI MENYUSI (Export / Import)
# =====================================================================
@dp.callback_query(F.data == "sa_status_report")
async def cb_status_report_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Yuklab olish", callback_data="status_download")],
        [InlineKeyboardButton(text="⬆️ Yuklab berish (Import)", callback_data="status_import")]
    ])
    await callback.message.answer(
        "📊 <b>Status hisoboti</b>\n\n"
        "Bu yerda barcha Super Admin, Yordamchi va VIP foydalanuvchilarning "
        "maqomi va qolgan VIP vaqti saqlanadi. Hisobot har 24 soatda avtomatik yangilanadi.",
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "status_download")
async def cb_status_download(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    generate_status_export()
    try:
        await callback.message.answer_document(
            FSInputFile(STATUS_EXPORT_PATH, filename="status_export.json"),
            caption="📊 Joriy status hisoboti (Super Admin/Yordamchi/VIP ro'yxati)."
        )
    except Exception as e:
        await callback.message.answer(f"❌ Faylni yuborishda xatolik: {e}")
    await callback.answer()

@dp.callback_query(F.data == "status_import")
async def cb_status_import_mode(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Qo'shish (mavjudga qo'shiladi)", callback_data="status_mode_merge")],
        [InlineKeyboardButton(text="🔁 Almashtirish (mavjudni o'chirib, yangisi yoziladi)", callback_data="status_mode_replace")],
    ])
    await callback.message.answer("Faylni qanday rejimda yuklaymiz?", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("status_mode_"))
async def cb_status_import_mode_selected(callback: CallbackQuery, state: FSMContext):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    mode = callback.data.split("_", 2)[2]
    await state.update_data(status_import_mode=mode)
    await callback.message.answer("📎 Endi status_export.json faylini (hujjat sifatida) yuboring:")
    await state.set_state(SuperAdminState.waiting_for_status_import_file)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_status_import_file, F.document)
async def process_status_import_file(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get('status_import_mode', 'merge')

    try:
        tg_file = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(tg_file.file_path)
        parsed = json.loads(file_bytes.read().decode("utf-8"))
        statuses = parsed.get("statuses", [])
    except Exception as e:
        await message.answer(f"❌ Faylni o'qishda xatolik: {e}")
        return

    now = time.time()
    applied = 0
    with db_connect() as (conn, cursor):
        if mode == 'replace':
            # Mavjud barcha status egalarini tozalaymiz (SUPER_ADMIN_ID hech qachon tegilmaydi - xavfsizlik uchun)
            cursor.execute(
                "UPDATE users SET role = 'user', vip_expires_at = 0 WHERE role IN ('vip', 'assistant') AND user_id != ?",
                (SUPER_ADMIN_ID,)
            )
            conn.commit()

        for entry in statuses:
            u_id = entry.get("user_id")
            role = entry.get("role")
            vip_hours = entry.get("vip_remaining_hours")
            if not u_id or not role or u_id == SUPER_ADMIN_ID:
                continue

            vip_expires_at = now + (vip_hours * 3600) if (role == 'vip' and vip_hours) else 0

            cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (u_id,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO users (user_id, full_name, username, role, vip_expires_at) VALUES (?, ?, ?, ?, ?)",
                    (u_id, "Import orqali qo'shilgan", "", role, vip_expires_at)
                )
            else:
                cursor.execute(
                    "UPDATE users SET role = ?, vip_expires_at = ?, renewal_reminder_sent = 0 WHERE user_id = ?",
                    (role, vip_expires_at, u_id)
                )
            applied += 1
        conn.commit()

    role = get_user_role(message.from_user.id)
    mode_text = "Almashtirish" if mode == 'replace' else "Qo'shish"
    await message.answer(
        f"✅ Status hisoboti import qilindi!\n\n"
        f"🔧 Rejim: <b>{mode_text}</b>\n"
        f"👥 Yangilangan/qo'shilgan yozuvlar: <b>{applied}</b> ta",
        reply_markup=get_main_keyboard(role), parse_mode="HTML"
    )
    await state.clear()


# =====================================================================
# OLMOS BOSHQARUVI (ID orqali istalgan foydalanuvchining Olmos sonini o'zgartirish)
# =====================================================================
@dp.callback_query(F.data == "sa_manage_coins")
async def cb_manage_coins(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.message.answer("💎 Olmos sonini o'zgartirmoqchi bo'lgan foydalanuvchining ID raqamini kiriting:")
    await state.set_state(SuperAdminState.waiting_for_coin_user_id)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_coin_user_id)
async def process_coin_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqamli ID kiriting!")
        return
    target_id = int(message.text)
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT full_name, diamonds FROM users WHERE user_id = ?", (target_id,))
        res = cursor.fetchone()
        
    if not res:
        await message.answer("❌ Bunday foydalanuvchi bazada topilmadi.")
        await state.clear()
        return
        
    full_name, current_diamonds = res
    current_diamonds = current_diamonds or 0
    await state.update_data(coin_user_id=target_id)
    await message.answer(
        f"👤 Foydalanuvchi: <b>{full_name}</b> (<code>{target_id}</code>)\n"
        f"💎 Hozirgi Olmos miqdori: <b>{current_diamonds} ta</b>\n\n"
        f"Yangi Olmos sonini kiriting (masalan: 5).\n"
        f"Qo'shish/ayirish uchun <code>+3</code> yoki <code>-2</code> ko'rinishida ham yuborishingiz mumkin:",
        parse_mode="HTML"
    )
    await state.set_state(SuperAdminState.waiting_for_coin_value)

@dp.message(SuperAdminState.waiting_for_coin_value)
async def process_coin_value(message: Message, state: FSMContext):
    raw = message.text.strip()
    data = await state.get_data()
    target_id = data.get('coin_user_id')
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT diamonds FROM users WHERE user_id = ?", (target_id,))
        res = cursor.fetchone()
        if not res:
            await message.answer("❌ Foydalanuvchi topilmadi.")
            await state.clear()
            return
        current_diamonds = res[0] or 0
        
        try:
            if raw.startswith('+') or raw.startswith('-'):
                delta = int(raw)
                new_value = max(0, current_diamonds + delta)
            else:
                new_value = int(raw)
                if new_value < 0:
                    raise ValueError
        except ValueError:
            await message.answer("❌ Noto'g'ri format! Masalan: <code>5</code>, <code>+3</code> yoki <code>-2</code> kiriting.", parse_mode="HTML")
            return
            
        cursor.execute("UPDATE users SET diamonds = ? WHERE user_id = ?", (new_value, target_id))
        conn.commit()
        
    role = get_user_role(message.from_user.id)
    await message.answer(
        f"✅ <a href='tg://user?id={target_id}'>{target_id}</a> foydalanuvchining Olmos miqdori "
        f"<b>{new_value} ta</b> qilib belgilandi!",
        reply_markup=get_main_keyboard(role), parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"💎 Sizning Olmos hisobingiz yangilandi. Hozirgi miqdor: <b>{new_value} ta</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await state.clear()

# =====================================================================
# JANR BOSHQARUVI (kino kodiga janr biriktirish - Janrlar menyusi shu orqali ishlaydi)
# =====================================================================
@dp.callback_query(F.data == "admin_set_genre")
async def cb_admin_set_genre(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.message.answer("🎭 Janr belgilamoqchi bo'lgan kino kodini kiriting (masalan: 15):")
    await state.set_state(AdminState.waiting_for_genre_code)
    await callback.answer()

@dp.message(AdminState.waiting_for_genre_code)
async def process_admin_genre_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.isdigit():
        await message.answer("Faqat raqamli kino kodini kiriting!")
        return
    await state.update_data(genre_movie_code=code)
    
    kb_rows = [[InlineKeyboardButton(text=g, callback_data=f"setgenre_{g}")] for g in GENRES_LIST]
    kb_rows.append([InlineKeyboardButton(text="🚫 Janrni olib tashlash", callback_data="setgenre_remove")])
    await message.answer(
        f"🎬 <b>{code}</b>-kodli kino uchun janrni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("setgenre_"), AdminState.waiting_for_genre_code)
async def cb_set_genre_confirm(callback: CallbackQuery, state: FSMContext):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
        
    data = await state.get_data()
    code = data.get('genre_movie_code')
    if not code:
        await callback.answer("Sessiya eskirgan, qaytadan urinib ko'ring.", show_alert=True)
        await state.clear()
        return
        
    choice = callback.data.split("_", 1)[1]
    
    with db_connect() as (conn, cursor):
        if choice == "remove":
            cursor.execute("DELETE FROM movies_meta WHERE code = ?", (code,))
            conn.commit()
            msg = f"🚫 {code}-kodli kinodan janr belgisi olib tashlandi."
        else:
            cursor.execute("INSERT INTO movies_meta (code, genre) VALUES (?, ?) ON CONFLICT(code) DO UPDATE SET genre = excluded.genre", (code, choice))
            conn.commit()
            msg = f"✅ {code}-kodli kino <b>{choice}</b> janriga muvaffaqiyatli biriktirildi!"
            
    role = get_user_role(callback.from_user.id)
    await callback.message.answer(msg, reply_markup=get_main_keyboard(role), parse_mode="HTML")
    await callback.answer()
    await state.clear()

@dp.callback_query(F.data == "sa_manage_assistants")
async def cb_manage_assistants(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Bu funksiya faqat Super Admin uchun!", show_alert=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, full_name, username FROM users WHERE role = 'assistant'")
    assistants = cursor.fetchall()
    conn.close()
    
    text = "👥 <b>Hozirgi Yordamchilar (Maksimal 3 ta):</b>\n\n"
    for u_id, name, uname in assistants:
        clean_name = name.replace("<", "&lt;").replace(">", "&gt;") if name else "Yordamchi"
        text += f"• <a href='tg://user?id={u_id}'>{clean_name}</a> (@{uname}, <code>{u_id}</code>)\n"
    if not assistants:
        text += "Hozircha yordamchilar tayinlanmagan.\n"
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yordamchi qo'shish", callback_data="sa_add_assistant")],
        [InlineKeyboardButton(text="➖ Yordamchini o'chirish", callback_data="sa_remove_assistant")]
    ])
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "sa_add_assistant")
async def cb_add_assistant(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) != 'super_admin': return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'assistant'")
    count = cursor.fetchone()[0]
    conn.close()
    
    if count >= 3:
        await callback.answer("Maksimal 3 ta yordamchi tayinlash mumkin!", show_alert=True)
        return
        
    await callback.message.answer("Yordamchi qilmoqchi bo'lgan foydalanuvchining ID raqamini kiriting:")
    await state.set_state(SuperAdminState.waiting_for_assistant_id)
    await state.update_data(action="add")
    await callback.answer()

@dp.callback_query(F.data == "sa_remove_assistant")
async def cb_remove_assistant(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) != 'super_admin': return
    await callback.message.answer("Yordamchilik huquqini olib tashlamoqchi bo'lgan foydalanuvchining ID raqamini kiriting:")
    await state.set_state(SuperAdminState.waiting_for_assistant_id)
    await state.update_data(action="remove")
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_assistant_id)
async def process_assistant_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqamli ID kiriting!")
        return
    target_id = int(message.text)
    data = await state.get_data()
    action = data.get('action')
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,))
    if not cursor.fetchone():
        await message.answer("Bunday foydalanuvchi topilmadi.")
        conn.close()
        return
        
    if action == "add":
        cursor.execute("UPDATE users SET role = 'assistant' WHERE user_id = ?", (target_id,))
        conn.commit()
        await message.answer(f"✅ <a href='tg://user?id={target_id}'>{target_id}</a> ID raqamli foydalanuvchi yordamchi qilib tayinlandi!", parse_mode="HTML")
        try:
            await bot.send_message(target_id, "🎉 Tabriklaymiz! Sizga Super Admin tomonidan yordamchi huquqi berildi.", parse_mode="HTML")
        except Exception:
            pass
    else:
        cursor.execute("UPDATE users SET role = 'user' WHERE user_id = ?", (target_id,))
        conn.commit()
        await message.answer(f"✅ <a href='tg://user?id={target_id}'>{target_id}</a> ID raqamli foydalanuvchidan yordamchi huquqi olib tashlandi.", parse_mode="HTML")
    conn.close()
    
    role = get_user_role(message.from_user.id)
    await message.answer("Bosh menyu:", reply_markup=get_main_keyboard(role))
    await state.clear()

@dp.callback_query(F.data == "sa_restrict_user")
async def cb_restrict_user(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if get_user_role(user_id) not in ['super_admin', 'assistant']: return
    await callback.message.answer("Cheklamoqchi (mute qilmoqchi) bo'lgan foydalanuvchining ID raqamini kiriting:")
    await state.set_state(SuperAdminState.waiting_for_restrict_id)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_restrict_id)
async def process_restrict_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqamli ID kiriting!")
        return
    await state.update_data(restrict_id=int(message.text))
    await message.answer("Foydalanuvchi necha soatga cheklansin? (Raqam kiriting, masalan: 24):")
    await state.set_state(SuperAdminState.waiting_for_restrict_hours)

@dp.message(SuperAdminState.waiting_for_restrict_hours)
async def process_restrict_hours(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqam kiriting!")
        return
    hours = int(message.text)
    data = await state.get_data()
    target_id = data.get('restrict_id')
    
    restricted_until = time.time() + (hours * 3600)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET restricted_until = ? WHERE user_id = ?", (restricted_until, target_id))
    conn.commit()
    conn.close()
    
    role = get_user_role(message.from_user.id)
    await message.answer(f"✅ <a href='tg://user?id={target_id}'>{target_id}</a> ID raqamli foydalanuvchi {hours} soatga vaqtinchalik cheklandi.", reply_markup=get_main_keyboard(role), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "admin_set_vip_code")
async def cb_admin_set_vip_code(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if get_user_role(user_id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.message.answer("VIP qilmoqchi bo'lgan kino kodini kiriting (masalan, 15):")
    await state.set_state(AdminState.waiting_for_vip_code)
    await callback.answer()

@dp.message(AdminState.waiting_for_vip_code)
async def process_admin_vip_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code.isdigit():
        await message.answer("Faqat raqamli kino kodini kiriting!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO vip_movies (code) VALUES (?)", (code,))
    conn.commit()
    conn.close()

    role = get_user_role(message.from_user.id)
    await message.answer(f"✅ {code}-kodli kino muvaffaqiyatli VIP turiga o'tkazildi!", reply_markup=get_main_keyboard(role))
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if get_user_role(user_id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Hammaga", callback_data="bcast_target_all")],
        [InlineKeyboardButton(text="👑 Faqat VIP'larga", callback_data="bcast_target_vip")],
        [InlineKeyboardButton(text="👤 Faqat Oddiy foydalanuvchilarga", callback_data="bcast_target_user")],
        [InlineKeyboardButton(text="🟢 Faqat Faollarga (so'nggi 7 kun)", callback_data="bcast_target_active")],
    ])
    await callback.message.answer("📢 Xabar kimlarga yuborilsin?", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("bcast_target_"))
async def cb_bcast_target(callback: CallbackQuery, state: FSMContext):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    target = callback.data.split("_", 2)[2]
    await state.update_data(bcast_target=target)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Hozir yuborish", callback_data="bcast_when_now")],
        [InlineKeyboardButton(text="⏱ Rejalashtirish", callback_data="bcast_when_later")],
    ])
    await callback.message.answer("Xabar qachon yuborilsin?", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "bcast_when_now")
async def cb_bcast_when_now(callback: CallbackQuery, state: FSMContext):
    await state.update_data(bcast_delay_minutes=0)
    await callback.message.answer("📢 Yubormoqchi bo'lgan xabaringiz matnini kiriting:")
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.answer()

@dp.callback_query(F.data == "bcast_when_later")
async def cb_bcast_when_later(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("⏱ Necha daqiqadan keyin yuborilsin? Raqam kiriting (masalan: 60):")
    await state.set_state(SuperAdminState.waiting_for_broadcast_schedule)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_broadcast_schedule)
async def process_bcast_schedule(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqam kiriting!")
        return
    await state.update_data(bcast_delay_minutes=int(message.text))
    await message.answer("📢 Yubormoqchi bo'lgan xabaringiz matnini kiriting:")
    await state.set_state(AdminState.waiting_for_broadcast)

def get_broadcast_target_users(target: str):
    with db_connect() as (conn, cursor):
        if target == 'vip':
            cursor.execute("SELECT user_id FROM users WHERE role = 'vip'")
        elif target == 'user':
            cursor.execute("SELECT user_id FROM users WHERE role = 'user'")
        elif target == 'active':
            cursor.execute("SELECT user_id FROM users WHERE last_active >= datetime('now', '-7 days')")
        else:
            cursor.execute("SELECT user_id FROM users")
        return [r[0] for r in cursor.fetchall()]

async def execute_broadcast(admin_chat_id: int, target: str, broadcast_msg: str):
    users = get_broadcast_target_users(target)
    broadcast_id = f"bc_{int(time.time())}_{random.randint(1000, 9999)}"
    success, blocked = 0, 0
    sent_log = []

    for u_id in users:
        try:
            sent = await bot.send_message(u_id, broadcast_msg, parse_mode="HTML")
            sent_log.append((broadcast_id, u_id, sent.message_id))
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            pass

    if sent_log:
        with db_connect() as (conn, cursor):
            cursor.executemany("INSERT INTO broadcast_log (broadcast_id, user_id, message_id) VALUES (?, ?, ?)", sent_log)
            conn.commit()

    target_names = {'all': 'Hammaga', 'vip': 'VIP\'larga', 'user': 'Oddiy foydalanuvchilarga', 'active': 'Faollarga'}
    report_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Ushbu xabarni hammadan o'chirish", callback_data=f"delbcast_{broadcast_id}")]
    ])
    try:
        await bot.send_message(
            admin_chat_id,
            f"✅ <b>Ommaviy xabar yuborish yakunlandi!</b>\n\n"
            f"🎯 Auditoriya: {target_names.get(target, target)}\n"
            f"✔️ Muvaffaqiyatli yetib bordi: {success} ta\n"
            f"❌ Botni bloklaganlar: {blocked} ta",
            reply_markup=report_kb,
            parse_mode="HTML"
        )
    except Exception:
        pass

@dp.message(AdminState.waiting_for_broadcast)
async def process_admin_broadcast(message: Message, state: FSMContext):
    broadcast_msg = message.text
    data = await state.get_data()
    target = data.get('bcast_target', 'all')
    delay_minutes = data.get('bcast_delay_minutes', 0)
    admin_chat_id = message.chat.id
    role = get_user_role(message.from_user.id)

    if delay_minutes and delay_minutes > 0:
        await message.answer(
            f"⏱ Xabar <b>{delay_minutes} daqiqadan</b> so'ng yuborilishi rejalashtirildi.",
            reply_markup=get_main_keyboard(role), parse_mode="HTML"
        )

        async def scheduled_task():
            await asyncio.sleep(delay_minutes * 60)
            await execute_broadcast(admin_chat_id, target, broadcast_msg)

        asyncio.create_task(scheduled_task())
    else:
        status_msg = await message.answer("📢 Ommaviy xabar yuborish boshlandi...")
        await execute_broadcast(admin_chat_id, target, broadcast_msg)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("Bosh menyu:", reply_markup=get_main_keyboard(role))

    await state.clear()

@dp.callback_query(F.data.startswith("delbcast_"))
async def cb_delete_broadcast(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']:
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    broadcast_id = callback.data.split("delbcast_", 1)[1]

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT user_id, message_id FROM broadcast_log WHERE broadcast_id = ?", (broadcast_id,))
        entries = cursor.fetchall()

    deleted = 0
    for u_id, msg_id in entries:
        try:
            await bot.delete_message(chat_id=u_id, message_id=msg_id)
            deleted += 1
        except Exception:
            pass

    with db_connect() as (conn, cursor):
        cursor.execute("DELETE FROM broadcast_log WHERE broadcast_id = ?", (broadcast_id,))
        conn.commit()

    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n🗑 <b>O'chirildi:</b> {deleted}/{len(entries)} ta foydalanuvchidan",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.answer("Xabar o'chirildi.")

@dp.callback_query(F.data == "sa_promo_codes")
async def cb_sa_promo(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) not in ['super_admin', 'assistant']: return
    await callback.message.answer("Promo kod nomini kiriting (masalan, KINO2026):")
    await state.set_state(SuperAdminState.waiting_for_promo_name)
    await callback.answer()

@dp.message(SuperAdminState.waiting_for_promo_name)
async def process_promo_name(message: Message, state: FSMContext):
    promo_name = message.text.strip().upper()
    await state.update_data(promo_name=promo_name)
    await message.answer(f"Promo kod: <b>{promo_name}</b>\nNechta Olmos bersin? Raqam kiriting:", parse_mode="HTML")
    await state.set_state(SuperAdminState.waiting_for_promo_reward)

@dp.message(SuperAdminState.waiting_for_promo_reward)
async def process_promo_reward(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqam kiriting!")
        return
    reward = int(message.text)
    await state.update_data(promo_reward=reward)
    await message.answer("Bu promo koddan ko'pi bilan nechta odam foydalangandan keyin yaroqsiz holatga kelsin? Limit raqamini kiriting (masalan: 50):")
    await state.set_state(SuperAdminState.waiting_for_promo_limit)

@dp.message(SuperAdminState.waiting_for_promo_limit)
async def process_promo_limit(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Faqat raqam kiriting!")
        return
    max_uses = int(message.text)
    data = await state.get_data()
    promo_name = data.get('promo_name')
    reward = data.get('promo_reward')
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO promo_codes (code_name, reward_coins, max_uses, used_count) VALUES (?, ?, ?, 0)", (promo_name, reward, max_uses))
    conn.commit()
    conn.close()
    
    role = get_user_role(message.from_user.id)
    await message.answer(f"✅ Promo kod muvaffaqiyatli yaratildi!\n\n🏷 Nomi: <b>{promo_name}</b>\n💎 Olmos: <b>{reward} ta</b>\n👥 Max limit: <b>{max_uses} ta odam</b>", reply_markup=get_main_keyboard(role), parse_mode="HTML")
    await state.clear()

# =====================================================================
# ID ORQALI VIP BERISH (Promo VIP o'rniga)
# =====================================================================
def grant_vip_to_user(user_id: int, days: int):
    """VIP statusni beradi — agar allaqachon VIP bo'lsa muddatga qo'shadi (real vaqtda hisoblanadi),
    aks holda hozirgi vaqtdan boshlab beradi. Muddat tugagach avtomatik uziladi (vip expiry loop orqali)."""
    now = time.time()
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT role, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        cur_role, cur_expiry = row if row else ('user', 0)
        base_time = cur_expiry if (cur_role == 'vip' and cur_expiry and cur_expiry > now) else now
        new_expiry = base_time + (days * 86400)
        cursor.execute(
            "UPDATE users SET role = 'vip', vip_expires_at = ?, renewal_reminder_sent = 0 WHERE user_id = ?",
            (new_expiry, user_id)
        )
        conn.commit()
    return new_expiry

def revoke_vip_grant(user_id: int, days: int):
    """Avtomatik tasdiqlangan VIP xaridni admin bekor qilganda ishlatiladi.
    Agar foydalanuvchida boshqa manbadan qo'shilgan qo'shimcha VIP vaqti ham
    bo'lsa, faqat shu xariddagi kunlar miqdorini ayiradi (butunlay olib
    tashlamaydi) - shu tufayli boshqa manbadagi VIP vaqti buzilmaydi."""
    now = time.time()
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT role, vip_expires_at FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return
        cur_role, cur_expiry = row
        if cur_role != 'vip' or not cur_expiry:
            return
        new_expiry = cur_expiry - (days * 86400)
        if new_expiry <= now:
            cursor.execute("UPDATE users SET role = 'user', vip_expires_at = 0 WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("UPDATE users SET vip_expires_at = ? WHERE user_id = ?", (new_expiry, user_id))
        conn.commit()

@dp.callback_query(F.data == "sa_grant_vip")
async def cb_sa_grant_vip(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.message.answer("👤 VIP bermoqchi bo'lgan foydalanuvchining ID raqamini kiriting:")
    await state.set_state(GrantVipState.waiting_for_user_id)
    await callback.answer()

@dp.message(GrantVipState.waiting_for_user_id)
async def process_grant_vip_user_id(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Faqat raqamli ID kiriting!")
        return
    await state.update_data(grant_vip_user_id=int(message.text.strip()))
    await message.answer("Necha kunlik VIP berilsin? (Masalan: 7):")
    await state.set_state(GrantVipState.waiting_for_duration)

@dp.message(GrantVipState.waiting_for_duration)
async def process_grant_vip_duration(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Faqat raqam kiriting!")
        return
    days = int(message.text.strip())
    data = await state.get_data()
    target_id = data.get("grant_vip_user_id")

    new_expiry = grant_vip_to_user(target_id, days)
    role = get_user_role(message.from_user.id)

    await message.answer(
        f"✅ <a href='tg://user?id={target_id}'>{target_id}</a> ga <b>{days} kunlik VIP</b> berildi!\n"
        f"Amal qilish muddati: {datetime.fromtimestamp(new_expiry).strftime('%d.%m.%Y %H:%M')} gacha (real vaqtda hisoblanadi, tugagach avtomatik uziladi).",
        reply_markup=get_main_keyboard(role), parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎉 Tabriklaymiz! Sizga <b>{days} kunlik VIP status</b> berildi!",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await state.clear()


# COMMENTS SYSTEM
# =====================================================================
@dp.callback_query(F.data.startswith("comments_"))
async def cb_show_comments(callback: CallbackQuery):
    code = callback.data.split("_")[1]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, full_name, comment, created_at FROM movie_comments WHERE code = ? ORDER BY id DESC LIMIT 10", (code,))
    rows = cursor.fetchall()
    conn.close()
    
    text = f"💬 <b>{code}-kodli kino uchun izohlar:</b>\n\n"
    if not rows:
        text += "Hozircha izohlar yo'q. Birinchi bo'lib izoh qoldiring!"
    else:
        for u_id, name, com, date in rows:
            clean_name = name.replace("<", "&lt;").replace(">", "&gt;") if name else "Foydalanuvchi"
            text += f"👤 <a href='tg://user?id={u_id}'>{clean_name}</a>: {com}\n<i>({date})</i>\n\n"
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Izoh yozish", callback_data=f"writecom_{code}")],
        [InlineKeyboardButton(text="🔙 Kinoga qaytish", callback_data=f"watch_{code}")]
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("writecom_"))
async def cb_write_comment_prompt(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    code = callback.data.split("_")[1]
    await state.update_data(comment_code=code)
    await callback.message.answer("✍️ Ushbu kino haqida o'z fikringiz va izohingizni yuboring:")
    await state.set_state(UserState.waiting_for_comment_text)
    await callback.answer()

@dp.message(UserState.waiting_for_comment_text)
async def process_user_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    code = data.get('comment_code')
    if not code:
        await state.clear()
        return
        
    comment_text = message.text.strip()
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO movie_comments (code, user_id, full_name, comment) VALUES (?, ?, ?, ?)", (code, user_id, full_name, comment_text))
    conn.commit()
    conn.close()
    
    await state.clear()
    role = get_user_role(user_id)
    await message.answer("✅ Izohingiz muvaffaqiyatli qo'shildi!", reply_markup=get_main_keyboard(role))

# =====================================================================
# MOVIE SEARCH & MAPPING LOGIC
# =====================================================================
@dp.message(UserState.waiting_for_movie_code)
async def process_movie_code_fsm(message: Message, state: FSMContext):
    await handle_movie_search(message.text.strip(), message, state)

@dp.message(F.text.func(lambda text: text and text.isdigit()), StateFilter(None))
async def process_direct_number(message: Message, state: FSMContext):
    await state.clear()
    if is_user_restricted(message.from_user.id): return
    await handle_movie_search(message.text.strip(), message, state)

async def handle_movie_search(code_text: str, message: Message, state: FSMContext = None):
    user_id = message.from_user.id
    update_last_active(user_id)
    mark_movie_search(user_id)
    
    if not code_text.isdigit():
        await message.answer("❌ Noto'g'ri format! Faqat raqamli kino kodini kiriting.")
        if state: await state.clear()
        return
        
    code_int = int(code_text)
    
    if time.time() - last_request_time.get(user_id, 0) < 0.8:
        await message.answer("⏳ Iltimos, biroz kuting (anti-spam).")
        return
    last_request_time[user_id] = time.time()

    role = get_user_role(user_id)
    
    if is_vip_movie(str(code_int)) and role not in ['vip', 'assistant', 'super_admin']:
        if not spend_diamonds(user_id, 1):
            await message.answer(
                "🔒 Bu VIP kino turiga kiradi. Ko'rish uchun 1 ta Olmos kerak, lekin hisobingizda olmos yo'q.\n"
                "Olmosni do'stlaringizni taklif qilish yoki promo kod orqali qo'lga kiritishingiz mumkin:",
                reply_markup=get_vip_upgrade_keyboard()
            )
            if state: await state.clear()
            return

    likes, dislikes, views = get_movie_stats(str(code_int))
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (user_id, str(code_int)))
        is_saved = cursor.fetchone() is not None

    ok = await deliver_movie(user_id, str(code_int), role, is_saved, likes, dislikes)
    if ok:
        if state: await state.clear()
        return

    # Aniq kod topilmadi — keng qamrovli qidiruv: avval oldinga 5 ta, keyin orqaga 5 ta kod tekshiriladi
    found_code = None
    for direction_codes in (
        [code_int + i for i in range(1, 6)],
        [code_int - i for i in range(1, 6) if code_int - i > 0],
    ):
        for candidate in direction_codes:
            candidate_str = str(candidate)
            if is_vip_movie(candidate_str) and role not in ['vip', 'assistant', 'super_admin']:
                continue  # tasodifan VIP kinoni ochib, olmos sarflab qo'ymaslik uchun o'tkazib yuboramiz
            c_likes, c_dislikes, c_views = get_movie_stats(candidate_str)
            with db_connect() as (conn, cursor):
                cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (user_id, candidate_str))
                c_is_saved = cursor.fetchone() is not None
            if await deliver_movie(user_id, candidate_str, role, c_is_saved, c_likes, c_dislikes, skip_wait=True):
                found_code = candidate
                break
        if found_code:
            break

    if state: await state.clear()
    if found_code:
        try:
            await bot.send_message(user_id, f"ℹ️ {code_int}-kod topilmadi, shunga yaqin <b>{found_code}</b>-kodli kino topildi va yuborildi.", parse_mode="HTML")
        except Exception:
            pass
    else:
        await message.answer(f"❌ {code_int}-kodli kino topilmadi yoki maxfiy kanalda bunday post mavjud emas.")

@dp.callback_query(F.data.startswith("watch_"))
async def cb_watch(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    code = callback.data.split("_")[1]
    likes, dislikes, views = get_movie_stats(code)
    role = get_user_role(callback.from_user.id)
    
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (callback.from_user.id, code))
        is_saved = cursor.fetchone() is not None
    
    # Kanaldagi post (qisqa yoki to'liq video) HECH QACHON o'chirilmasligi kerak.
    # Faqat botning shaxsiy chatdagi ro'yxat xabari (Saqlanganlar/VIP Kinolar) bo'lsa tozalanadi.
    if callback.message.chat.id != MAIN_CHANNEL_ID and callback.message.chat.id != CHANNEL_ID:
        try:
            await callback.message.delete()
        except Exception:
            pass
        
    ok = await deliver_movie(callback.from_user.id, code, role, is_saved, likes, dislikes)
    if not ok:
        await callback.answer("Kino topilmadi.", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("save_"))
async def cb_save_movie(callback: CallbackQuery):
    user_id = callback.from_user.id
    code = callback.data.split("_")[1]
    
    if not use_vip_coin_for_service(user_id):
        await callback.answer("Bu funksiya uchun VIP obuna yoki VIP Coin kerak!", show_alert=True)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM saved_movies WHERE user_id = ? AND movie_code = ?", (user_id, code))
    exists = cursor.fetchone()
    
    if not exists:
        cursor.execute("INSERT INTO saved_movies (user_id, movie_code) VALUES (?, ?)", (user_id, code))
        conn.commit()
        is_saved = True
        msg = "Kino saqlanganlarga qo'shildi!"
    else:
        cursor.execute("DELETE FROM saved_movies WHERE user_id = ? AND movie_code = ?", (user_id, code))
        conn.commit()
        is_saved = False
        msg = "Kino saqlanganlardan o'chirildi!"
    conn.close()
    
    likes, dislikes, views = get_movie_stats(code)
    role = get_user_role(user_id)
    try:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=generate_movie_keyboard(code, likes, dislikes, is_saved, is_vip_viewer=role in ['vip', 'assistant', 'super_admin'])
        )
    except Exception:
        pass
        
    await callback.answer(msg, show_alert=True)

# =====================================================================
# GLOBAL ERROR HANDLER - bot bitta xatolik tufayli butunlay to'xtab
# qolmasligi uchun barcha kutilmagan xatoliklarni ushlab, logga yozadi
# =====================================================================

# =====================================================================
# SUPER ADMIN - QISQA VIDEO YUKLASH (ASOSIY KANALGA)
# =====================================================================
@dp.callback_query(F.data == "sa_upload_short")
async def sa_upload_short_start(callback: CallbackQuery, state: FSMContext):
    """Yangi kino qo'shish oqimini boshlaydi (Yuklash tugmasi / eski submenu callback)."""
    await callback.answer()
    await _ask_short_type(callback.message, state, as_new_message=True)

@dp.message(F.text == "📤 Yuklash")
async def msg_upload_short_start(message: Message, state: FSMContext):
    """Asosiy menyudagi 'Yuklash' tugmasi - super admin/yordamchi uchun."""
    await state.clear()
    role = get_user_role(message.from_user.id)
    if role not in ['super_admin', 'assistant']:
        return
    await _ask_short_type(message, state, as_new_message=True)

async def _ask_short_type(target, state: FSMContext, as_new_message: bool):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Oddiy", callback_data="short_type_oddiy"),
         InlineKeyboardButton(text="👑 VIP", callback_data="short_type_vip")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="sa_menu")]
    ])
    text = "🎬 <b>Yangi kino qo'shish</b>\n\nBu kino qanday turda joylansin?"
    if as_new_message:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SuperAdminState.waiting_for_short_type)

@dp.callback_query(SuperAdminState.waiting_for_short_type, F.data == "short_type_oddiy")
async def sa_short_type_oddiy(callback: CallbackQuery, state: FSMContext):
    await state.update_data(is_vip=False, vip_until=0, vip_label="")
    await callback.answer()
    await _ask_genre(callback.message, state)

@dp.callback_query(SuperAdminState.waiting_for_short_type, F.data == "short_type_vip")
async def sa_short_type_vip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 kun", callback_data="vipdur_5"), InlineKeyboardButton(text="10 kun", callback_data="vipdur_10")],
        [InlineKeyboardButton(text="15 kun", callback_data="vipdur_15"), InlineKeyboardButton(text="1 oy", callback_data="vipdur_30")],
        [InlineKeyboardButton(text="♾ Cheksiz", callback_data="vipdur_0")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="sa_menu")]
    ])
    await callback.message.edit_text(
        "👑 <b>VIP kino</b>\n\nQancha muddat VIP (pullik/cheklangan) bo'lib tursin?\n"
        "Muddat tugagach, kino avtomatik ravishda hammaga ochiladi.",
        reply_markup=kb, parse_mode="HTML"
    )
    await state.set_state(SuperAdminState.waiting_for_short_vip_duration)

@dp.callback_query(SuperAdminState.waiting_for_short_vip_duration, F.data.startswith("vipdur_"))
async def sa_short_vip_duration(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[1])
    labels = {5: "5 kun", 10: "10 kun", 15: "15 kun", 30: "1 oy", 0: "Cheksiz"}
    if days == 0:
        await state.update_data(is_vip=True, vip_until=0, vip_label="Cheksiz")
    else:
        vip_until = time.time() + days * 86400
        await state.update_data(is_vip=True, vip_until=vip_until, vip_label=labels[days])
    await callback.answer()
    await _ask_genre(callback.message, state)

async def _ask_genre(message: Message, state: FSMContext):
    genres_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=g, callback_data=f"sa_short_genre_{idx}") for idx, g in list(enumerate(GENRES_LIST))[j:j+2]]
            for j in range(0, len(GENRES_LIST), 2)
        ] + [[InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="sa_menu")]]
    )
    await message.edit_text("📂 <b>Janrni tanlang:</b>", reply_markup=genres_kb, parse_mode="HTML")
    await state.set_state(SuperAdminState.waiting_for_short_genre)

@dp.callback_query(SuperAdminState.waiting_for_short_genre, F.data.startswith("sa_short_genre_"))
async def sa_short_genre_selected(callback: CallbackQuery, state: FSMContext):
    """Janr tanlangandan keyin — qisqa videoni so'raydi."""
    genre_idx = int(callback.data.split("_")[-1])
    genre = GENRES_LIST[genre_idx]
    await state.update_data(genre=genre)
    await callback.answer()
    await callback.message.edit_text(
        f"✅ Janr: <b>{genre}</b>\n\n📹 Endi kinoning <b>qisqa treyler videosini</b> yuboring "
        f"(bu — asosiy kanalga reklama sifatida joylanadi):",
        parse_mode="HTML"
    )
    await state.set_state(SuperAdminState.waiting_for_short_video)

@dp.message(SuperAdminState.waiting_for_short_video, F.video)
async def sa_short_video_received(message: Message, state: FSMContext):
    """Qisqa video qabul qilinadi, endi to'liq (uzun) kino so'raladi."""
    await state.update_data(short_video_file_id=message.video.file_id)
    await message.answer(
        "✅ Qisqa video qabul qilindi.\n\n"
        "🎞 Endi kinoning <b>to'liq (uzun) videosini</b> yuboring "
        "(bu — maxfiy kanalga joylanadi, foydalanuvchilar shundan ko'radi):",
        parse_mode="HTML"
    )
    await state.set_state(SuperAdminState.waiting_for_full_video)

@dp.message(SuperAdminState.waiting_for_short_video)
async def sa_short_video_missing(message: Message, state: FSMContext):
    await message.answer("❌ Iltimos, qisqa treyler videosini «video» sifatida yuboring.")

@dp.message(SuperAdminState.waiting_for_full_video, F.video)
async def sa_full_video_received(message: Message, state: FSMContext):
    """To'liq video qabul qilinadi — endi kodni aniqlaymiz (birinchi marta qo'lda, keyin avtomatik +1)."""
    await state.update_data(full_video_file_id=message.video.file_id)

    last_code = get_last_main_channel_code()
    if last_code == 0:
        await message.answer(
            "🆕 Bu — birinchi video. Kino uchun <b>boshlang'ich KODI</b>ni kiriting (Masalan: 27).\n"
            "Bundan keyingi barcha videolar uchun kod <b>avtomatik +1</b> qilib boriladi.",
            parse_mode="HTML"
        )
        await state.set_state(SuperAdminState.waiting_for_short_code)
        return

    code = last_code + 1
    await state.update_data(pending_code=code)
    await _show_short_upload_confirmation(message, state)

@dp.message(SuperAdminState.waiting_for_full_video)
async def sa_full_video_missing(message: Message, state: FSMContext):
    await message.answer("❌ Iltimos, to'liq kino videosini «video» sifatida yuboring.")

@dp.message(SuperAdminState.waiting_for_short_code)
async def sa_short_code_received(message: Message, state: FSMContext):
    """Kod qo'lda kiritilganda (birinchi marta, yoki '✏️ Kodni tahrirlash' orqali)."""
    code_text = message.text.strip()
    if not code_text.isdigit():
        await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak!")
        return
    code = int(code_text)

    if is_code_already_posted(code):
        await message.answer(f"❌ Bu kod ({code}) allaqachon ishlatilgan! Boshqa kodni kiriting:")
        return

    await state.update_data(pending_code=code)
    await _show_short_upload_confirmation(message, state)

async def _show_short_upload_confirmation(message: Message, state: FSMContext):
    """Janr/yil/kod/tur bilan yakuniy tasdiqlash oynasini ko'rsatadi."""
    data = await state.get_data()
    genre = data.get("genre")
    code = data.get("pending_code")
    is_vip = data.get("is_vip", False)
    vip_label = data.get("vip_label", "")

    year = random.randint(2017, 2026)
    await state.update_data(pending_year=year)

    type_line = f"👑 VIP ({vip_label})" if is_vip else "👤 Oddiy (ochiq)"
    text = (
        f"🎬 Janr: <b>{genre}</b>\n"
        f"📅 Yil: <b>{year}</b> (tasodifiy)\n"
        f"🔛 Kod: <b>{code}</b>\n"
        f"🏷 Turi: <b>{type_line}</b>\n\n"
        "Shu ma'lumotlar bilan ikkala kanalga (asosiy + maxfiy) joylashni tasdiqlaysizmi?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="sa_short_confirm"),
         InlineKeyboardButton(text="✏️ Kodni tahrirlash", callback_data="sa_short_edit_code")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="sa_menu")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "sa_short_edit_code")
async def sa_short_edit_code(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("✏️ Yangi kodni kiriting:")
    await state.set_state(SuperAdminState.waiting_for_short_code)

@dp.callback_query(F.data == "sa_short_confirm")
async def sa_short_confirm(callback: CallbackQuery, state: FSMContext):
    """Admin tasdiqlaganda — to'liq video maxfiy kanalga, qisqa video asosiy kanalga joylanadi."""
    await callback.answer()
    data = await state.get_data()
    short_video_file_id = data.get("short_video_file_id")
    full_video_file_id = data.get("full_video_file_id")
    genre = data.get("genre")
    code = data.get("pending_code")
    year = data.get("pending_year")
    is_vip = data.get("is_vip", False)
    vip_until = data.get("vip_until", 0)
    vip_label = data.get("vip_label", "")

    genre_desc = GENRE_TEMPLATES.get(genre, "Bugun ko'rish uchun qiziqarli film!")

    try:
        # 1) To'liq kinoni MAXFIY kanalga joylaymiz
        secret_caption = f"🎬 {genre} {year} | Kod: {code}" + (f" | VIP: {vip_label}" if is_vip else "")
        secret_msg = await bot.send_video(
            chat_id=CHANNEL_ID,
            video=full_video_file_id,
            caption=secret_caption
        )

        # 2) Qisqa videoni ASOSIY kanalga joylaymiz
        watch_label = "🔒 Vip Tomosha" if is_vip else "🎬 Tomosha qilish"
        bot_username = await get_bot_username()
        watch_url = f"https://t.me/{bot_username}?start=watch_{code}"
        main_caption = (
            f"🎬 Kino janri: <b>{genre}</b> {year}\n"
            f"🔛 Kino KODI : {code} ✅\n\n"
            f"Kinoni to'ligini pastagi tugma orqali yuklab oling\n\n"
            f"🔐 {genre_desc}\n\n"
            f"🖇 Bot manzili: @Premium_kinolari_bot"
        )
        await bot.send_video(
            chat_id=MAIN_CHANNEL_ID,
            video=short_video_file_id,
            caption=main_caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=watch_label, url=watch_url)]]
            )
        )

        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO main_channel_posts (code, genre, year, posted_at, video_file_id, is_vip, vip_until, secret_message_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code, genre, year, time.time(), short_video_file_id, 1 if is_vip else 0, vip_until, secret_msg.message_id)
            )
            if is_vip:
                cursor.execute(
                    "INSERT OR REPLACE INTO vip_movies (code, vip_until) VALUES (?, ?)",
                    (str(code), vip_until if vip_until else None)
                )
            conn.commit()
        set_last_main_channel_code(code)

        await callback.message.edit_text(
            f"✅ <b>Kino ikkala kanalga ham joylandi!</b>\n\n"
            f"📌 Janr: {genre} | 📅 Yil: {year} | 🔛 Kod: {code}\n"
            f"🏷 Turi: {'👑 VIP (' + vip_label + ')' if is_vip else '👤 Oddiy'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menu", callback_data="sa_menu")]]),
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Xatolik yuz berdi:\n{str(e)}\n\n"
            "MAIN_CHANNEL_ID va CHANNEL_ID to'g'ri kiritilganligini, bot ikkala kanalga ham admin qilib qo'yilganini tekshiring.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="sa_menu")]])
        )

    await state.clear()


# =====================================================================
# BOZOR — VIP OBUNA SOTIB OLISH
# =====================================================================
def build_vip_shop_keyboard() -> InlineKeyboardMarkup:
    """Har bir muddat uchun bitta qator (vertikal, 1 ustunli) - narx to'liq ko'rinadi.
    Chegirma narx sozlangan bo'lsa, u qatorning boshida chegirma belgisi bilan chiqadi."""
    rows = []
    for days, label, price in VIP_FIXED_PRICES:
        discount = get_vip_discount_price(days)
        if discount is not None:
            text = f"🏷 Chegirma: {discount:,} so'm — {label} (avvalgi narx: {price:,} so'm)"
            callback_data = f"buyvip_discount_{days}"
        else:
            text = f"{label} — {price:,} so'm"
            callback_data = f"buyvip_fixed_{days}"
        rows.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(F.text == "🛒 Bozor")
async def msg_vip_shop(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if is_user_restricted(user_id): return
    update_last_active(user_id)
    await message.answer(
        "🛒 <b>Bozor — VIP obuna sotib olish</b>\n\n"
        "VIP a'zolik sizga: reklamasiz, kutishsiz va cheklovsiz barcha kinolarni tomosha qilish imkonini beradi.\n\n"
        "Muddatni tanlang:",
        reply_markup=build_vip_shop_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "vip_shop")
async def cb_vip_shop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "🛒 <b>Bozor — VIP obuna sotib olish</b>\n\nMuddatni tanlang:",
        reply_markup=build_vip_shop_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("buyvip_"))
async def cb_buy_vip_selected(callback: CallbackQuery, state: FSMContext):
    _, kind, days_str = callback.data.split("_")
    days = int(days_str)
    label = next(l for d, l, p in VIP_FIXED_PRICES if d == days)
    if kind == "discount":
        price = get_vip_discount_price(days)
    else:
        price = next(p for d, l, p in VIP_FIXED_PRICES if d == days)

    await state.update_data(vip_buy_days=days, vip_buy_price=price, vip_buy_label=label)
    await state.set_state(VipPurchaseState.waiting_for_receipt_photo)
    await callback.answer()

    card_number, card_holder = get_vip_card_info()
    holder_line = f" ({card_holder})" if card_holder else ""
    await callback.message.answer(
        f"💳 <b>To'lov</b>\n\n"
        f"Tanlandi: <b>{label}</b> — <b>{price:,} so'm</b>\n\n"
        f"Quyidagi karta raqamiga to'lovni amalga oshiring:\n"
        f"<code>{card_number}</code>{holder_line}\n\n"
        f"To'lovdan so'ng, <b>screenshot (rasm)</b> yuboring.\n"
        f"⏱ Admin so'rovingizni <b>1 soat ichida</b> ko'rib chiqadi.",
        parse_mode="HTML"
    )

@dp.message(VipPurchaseState.waiting_for_receipt_photo, F.photo)
async def process_vip_payment_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("vip_buy_days")
    price = data.get("vip_buy_price")
    label = data.get("vip_buy_label")
    user_id = message.from_user.id
    now = time.time()

    with db_connect() as (conn, cursor):
        cursor.execute(
            "INSERT INTO vip_purchase_requests (user_id, duration_label, duration_days, price, receipt_file_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, label, days, price, message.photo[-1].file_id, now)
        )
        req_id = cursor.lastrowid
        conn.commit()

    await message.answer(
        "✅ To'lov screenshoti qabul qilindi!\n"
        "⏱ Admin so'rovingizni <b>1 soat ichida</b> ko'rib chiqadi, so'ng VIP statusingiz faollashadi.",
        parse_mode="HTML"
    )
    await state.clear()

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"vipreq_ok_{req_id}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"vipreq_no_{req_id}")
        ]])
        sent = await bot.send_photo(
            SUPER_ADMIN_ID, message.photo[-1].file_id,
            caption=f"🛒 <b>Yangi VIP xarid so'rovi #{req_id}</b>\n\n"
                    f"Foydalanuvchi: <code>{user_id}</code>\nMuddat: {label}\nNarx: {price:,} so'm",
            reply_markup=kb, parse_mode="HTML"
        )
        # Admin xabarining ID'sini saqlaymiz — 1 soatda avtomatik tasdiqlanganda
        # eskirgan Tasdiqlash/Bekor qilish tugmalarini olib tashlash uchun kerak.
        with db_connect() as (conn, cursor):
            cursor.execute("UPDATE vip_purchase_requests SET admin_msg_id = ? WHERE id = ?", (sent.message_id, req_id))
            conn.commit()
    except Exception:
        pass

@dp.message(VipPurchaseState.waiting_for_receipt_photo)
async def process_vip_payment_screenshot_missing(message: Message, state: FSMContext):
    await message.answer("❌ Iltimos, to'lov screenshotini <b>rasm</b> sifatida yuboring.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("vipreq_ok_"))
async def cb_vip_request_approve(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    req_id = int(callback.data.split("_")[-1])
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT user_id, duration_days, duration_label, status FROM vip_purchase_requests WHERE id = ?", (req_id,))
        row = cursor.fetchone()
        if not row or row[3] != 'pending':
            await callback.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
            return
        user_id, days, label, _ = row
        cursor.execute("UPDATE vip_purchase_requests SET status = 'approved', decided_at = ? WHERE id = ?", (time.time(), req_id))
        conn.commit()

    grant_vip_to_user(user_id, days)
    await callback.answer("✅ Tasdiqlandi!")
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ TASDIQLANDI", parse_mode="HTML")
    try:
        await bot.send_message(user_id, f"🎉 To'lovingiz tasdiqlandi! Sizga <b>{label}</b> VIP obuna berildi.", parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("vipreq_no_"))
async def cb_vip_request_reject(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    req_id = int(callback.data.split("_")[-1])
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT user_id, status FROM vip_purchase_requests WHERE id = ?", (req_id,))
        row = cursor.fetchone()
        if not row or row[1] != 'pending':
            await callback.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
            return
        user_id, _ = row
        cursor.execute("UPDATE vip_purchase_requests SET status = 'rejected', decided_at = ? WHERE id = ?", (time.time(), req_id))
        conn.commit()

    await callback.answer("❌ Bekor qilindi")
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ BEKOR QILINDI", parse_mode="HTML")
    try:
        await bot.send_message(user_id, "❌ To'lovingiz tasdiqlanmadi. Savol bo'lsa, admin bilan bog'laning.")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("cancel_autovip_"))
async def cb_cancel_auto_vip(callback: CallbackQuery):
    """1 soat javobsiz qolib avtomatik tasdiqlangan VIP xaridni admin bekor qiladi.
    Faqat shu xariddagi kunlar ayiriladi (boshqa manbadagi VIP vaqti saqlanadi)."""
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    req_id = int(callback.data.split("_")[-1])
    with db_connect() as (conn, cursor):
        cursor.execute(
            "SELECT user_id, duration_days, status FROM vip_purchase_requests WHERE id = ?", (req_id,)
        )
        row = cursor.fetchone()
        if not row or row[2] != 'auto_approved':
            await callback.answer("Bu so'rovni bekor qilib bo'lmaydi (holati o'zgargan).", show_alert=True)
            return
        user_id, days, _ = row
        cursor.execute(
            "UPDATE vip_purchase_requests SET status = 'auto_approved_cancelled', decided_at = ? WHERE id = ?",
            (time.time(), req_id)
        )
        conn.commit()

    revoke_vip_grant(user_id, days)
    await callback.answer("🚫 VIP bekor qilindi.")
    try:
        # Matnni yangilaymiz; reply_markup berilmagani uchun tugma ham o'chadi (ikki marta bosilmaydi).
        await callback.message.edit_text(
            callback.message.html_text + "\n\n🚫 <b>ADMIN TOMONIDAN BEKOR QILINDI</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await bot.send_message(user_id, "⚠️ VIP obunangiz admin tomonidan bekor qilindi. Savol bo'lsa, admin bilan bog'laning.")
    except Exception:
        pass

# =====================================================================
# SUPER ADMIN — BOZORNI BOSHQARISH (chegirma narxlar, karta raqami)
# =====================================================================
@dp.callback_query(F.data == "sa_market_menu")
async def cb_sa_market_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.answer()
    card_number, card_holder = get_vip_card_info()
    lines = [f"💳 Karta: <code>{card_number}</code> ({card_holder or '—'})\n", "🏷 Chegirma narxlar:"]
    for days, label, price in VIP_FIXED_PRICES:
        discount = get_vip_discount_price(days)
        lines.append(f"  • {label}: qat'iy {price:,} so'm | chegirma {discount:,} so'm" if discount is not None else f"  • {label}: qat'iy {price:,} so'm | chegirma — sozlanmagan")

    with db_connect() as (conn, cursor):
        cursor.execute("SELECT COUNT(*) FROM vip_purchase_requests WHERE status = 'pending'")
        pending_count = cursor.fetchone()[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Karta raqamini sozlash", callback_data="sa_set_card")],
        [InlineKeyboardButton(text="🏷 Chegirma narxlarni sozlash", callback_data="sa_set_discount")],
        [InlineKeyboardButton(text=f"📋 Kutilayotgan so'rovlar ({pending_count})", callback_data="sa_pending_vip")],
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data="sa_menu")]
    ])
    await callback.message.answer("🛒 <b>Bozorni boshqarish</b>\n\n" + "\n".join(lines), reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "sa_set_card")
async def cb_sa_set_card_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("💳 Yangi karta raqamini kiriting (masalan: 8600 1234 5678 9012):")
    await state.set_state(BozorAdminState.waiting_for_card_number)

@dp.message(BozorAdminState.waiting_for_card_number)
async def process_set_card_number(message: Message, state: FSMContext):
    set_setting("vip_card_number", message.text.strip())
    await message.answer("✅ Karta raqami saqlandi. Endi karta egasining ismini kiriting (yoki '-' agar kerak bo'lmasa):")
    await state.set_state(BozorAdminState.waiting_for_card_holder)

@dp.message(BozorAdminState.waiting_for_card_holder)
async def process_set_card_holder(message: Message, state: FSMContext):
    holder = message.text.strip()
    set_setting("vip_card_holder", "" if holder == "-" else holder)
    role = get_user_role(message.from_user.id)
    await message.answer("✅ Karta ma'lumotlari saqlandi.", reply_markup=get_main_keyboard(role))
    await state.clear()

@dp.callback_query(F.data == "sa_set_discount")
async def cb_sa_set_discount_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"setdisc_{days}")] for days, label, price in VIP_FIXED_PRICES
    ] + [[InlineKeyboardButton(text="◀️ Orqaga", callback_data="sa_market_menu")]])
    await callback.message.answer("🏷 Qaysi muddat uchun chegirma narx belgilaysiz?", reply_markup=kb)

@dp.callback_query(F.data.startswith("setdisc_"))
async def cb_set_discount_duration(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[1])
    await state.update_data(discount_days=days)
    await callback.answer()
    await callback.message.answer("Yangi chegirma narxni so'mda kiriting (masalan: 1500):")
    await state.set_state(BozorAdminState.waiting_for_discount_price)

@dp.message(BozorAdminState.waiting_for_discount_price)
async def process_set_discount_price(message: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("discount_days")
    if not message.text.strip().isdigit():
        await message.answer("❌ Faqat raqam kiriting!")
        return
    price = int(message.text.strip())
    set_setting(f"vip_discount_{days}", price)
    role = get_user_role(message.from_user.id)
    label = next(l for d, l, p in VIP_FIXED_PRICES if d == days)
    await message.answer(f"✅ {label} uchun chegirma narx <b>{price:,} so'm</b> qilib belgilandi.", reply_markup=get_main_keyboard(role), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "sa_pending_vip")
async def cb_sa_pending_vip(callback: CallbackQuery):
    await callback.answer()
    with db_connect() as (conn, cursor):
        cursor.execute(
            "SELECT id, user_id, duration_label, price, created_at FROM vip_purchase_requests "
            "WHERE status = 'pending' ORDER BY created_at ASC LIMIT 20"
        )
        rows = cursor.fetchall()
    if not rows:
        await callback.message.answer("📭 Hozircha kutilayotgan VIP so'rovlar yo'q.")
        return
    lines = ["📋 <b>Kutilayotgan VIP so'rovlar:</b>\n"]
    for req_id, user_id, label, price, created_at in rows:
        left_h = max(0, 24 - int((time.time() - created_at) // 3600))
        lines.append(f"#{req_id} | <code>{user_id}</code> | {label} | {price:,} so'm | ⏱ {left_h} soat qoldi")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")

# =====================================================================
# UMUMIY FALLBACK — agar hech qanday state faol bo'lmasa va matn hech qanday
# tugma/buyruqqa mos kelmasa, foydalanuvchiga kod yuborishni eslatadi.
# Bu handler fayldagi ENG OXIRGI @dp.message() bo'lishi shart - shundagina
# faqat boshqa hech qaysi handler ushlamagan xabarlar shu yerga tushadi.
# =====================================================================
@dp.message(StateFilter(None), F.text)
async def fallback_plain_text(message: Message):
    text = message.text.strip()
    if text.startswith("/"):
        return
    if text.isdigit():
        await handle_movie_search(text, message, None)
        return
    await message.answer("❗️ Kod yuboring, masalan: <code>24</code>", parse_mode="HTML")


@dp.error()
async def global_error_handler(event: ErrorEvent):
    global ERROR_COUNT
    ERROR_COUNT += 1
    error_text = f"{type(event.exception).__name__}: {event.exception}"
    logger.exception(f"Kutilmagan xatolik yuz berdi: {event.exception}")
    try:
        with db_connect() as (conn, cursor):
            cursor.execute(
                "INSERT INTO bot_errors (error_text, occurred_at, sent_in_digest) VALUES (?, ?, 0)",
                (error_text[:500], time.time())
            )
            conn.commit()
    except Exception:
        pass
    try:
        upd = event.update
        chat_id = None
        if upd.message:
            chat_id = upd.message.chat.id
        elif upd.callback_query:
            chat_id = upd.callback_query.from_user.id
        if chat_id:
            await bot.send_message(chat_id, "⚠️ Kutilmagan xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring yoki /start bosing.")
    except Exception:
        pass
    return True

async def error_digest_loop():
    """Har 24 soatda: shu oraliqda yozilgan xatoliklarni Super Adminga yig'ib yuboradi."""
    while True:
        await asyncio.sleep(86400)
        try:
            with db_connect() as (conn, cursor):
                cursor.execute("SELECT id, error_text, occurred_at FROM bot_errors WHERE sent_in_digest = 0 ORDER BY occurred_at ASC")
                rows = cursor.fetchall()
                if rows:
                    cursor.execute("UPDATE bot_errors SET sent_in_digest = 1 WHERE sent_in_digest = 0")
                    conn.commit()
            if rows:
                lines = [f"🐞 <b>So'nggi 24 soatlik xatoliklar hisoboti</b> (jami {len(rows)} ta):\n"]
                for err_id, err_text, occurred_at in rows[-30:]:
                    t = datetime.fromtimestamp(occurred_at).strftime('%d.%m %H:%M')
                    lines.append(f"[{t}] {err_text}")
                text = "\n".join(lines)
                for chunk_start in range(0, len(text), 3500):
                    await bot.send_message(SUPER_ADMIN_ID, text[chunk_start:chunk_start+3500], parse_mode="HTML")
        except Exception as e:
            logger.error(f"error_digest_loop xatosi: {e}")

@dp.callback_query(F.data == "sa_error_report")
async def cb_sa_error_report(callback: CallbackQuery):
    if get_user_role(callback.from_user.id) != 'super_admin':
        await callback.answer("Sizda bu huquq yo'q!", show_alert=True)
        return
    await callback.answer()
    with db_connect() as (conn, cursor):
        cursor.execute("SELECT COUNT(*) FROM bot_errors WHERE occurred_at >= ?", (time.time() - 86400,))
        count_24h = cursor.fetchone()[0]
        cursor.execute("SELECT error_text, occurred_at FROM bot_errors ORDER BY occurred_at DESC LIMIT 10")
        recent = cursor.fetchall()
    if not recent:
        await callback.message.answer("✅ Hozircha hech qanday xatolik qayd etilmagan.")
        return
    lines = [f"🐞 <b>Xatoliklar hisoboti</b>\nSo'nggi 24 soatda: <b>{count_24h}</b> ta\n\nOxirgi 10 tasi:"]
    for err_text, occurred_at in recent:
        t = datetime.fromtimestamp(occurred_at).strftime('%d.%m %H:%M')
        lines.append(f"[{t}] {err_text}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")

# =====================================================================
# RENDER & BOT ASYNCHRONOUS RUNNER (FastAPI + Aiogram Polling)
# =====================================================================
async def run_bot():
    await dp.start_polling(bot, skip_updates=True)

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN topilmadi! Render/Server'da Environment Variables bo'limiga BOT_TOKEN qo'shing.")
        return
    logger.info("Bot v4.2.0 versiyasida ishga tushmoqda (barqarorlashtirilgan)...")
    asyncio.create_task(check_vip_expirations())
    asyncio.create_task(status_export_loop())
    asyncio.create_task(daily_reward_loop())
    asyncio.create_task(vip_purchase_loop())
    asyncio.create_task(vip_movie_expiry_loop())
    asyncio.create_task(error_digest_loop())
    asyncio.create_task(db_backup_loop())
    
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())