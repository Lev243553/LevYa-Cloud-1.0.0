import telebot
from telebot import types
import os
from dotenv import load_dotenv
from mystars_faas import MyStarsClient
import time
import logging
import random
import string
import sqlite3
import threading
import asyncio
import aiohttp
from pytoniq import WalletV4R2, LiteBalancer, begin_cell
from pytoniq_core import Address
from decimal import Decimal

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
MYSTARS_API_KEY = os.getenv("MYSTARS_API_KEY")
TON_WALLET = os.getenv("TON_WALLET")
CHANNEL_USERNAME = "@PlameChat"
BOT_USERNAME = "plamechatbot"
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY")

SBP_PHONE = "+79010418366"
SBP_NAME = "Лев Я."

TON_MNEMONIC = os.getenv("TON_WALLET_MNEMONIC", "").split()

bot = telebot.TeleBot(TOKEN, threaded=True)
client = MyStarsClient.production(MYSTARS_API_KEY)
logging.getLogger('TeleBot').setLevel(logging.CRITICAL)

DB_PATH = "plamechat.db"
WALLET_RAW = "0:cf42d0d4113310536f07a0ad8bc6e621f23481ab3ce2ca9f3fdb2595c206a02f"

USD_RUB_RATE = 90.0
FALLBACK_TON_PRICE = 1.30

STAR_PACKAGES = {
    50: {"usd": 1.20},
    100: {"usd": 2.20},
    250: {"usd": 5.50},
    500: {"usd": 11.00},
    1000: {"usd": 21.00}
}

PREMIUM_PACKAGES = {
    3: {"usd": 5.00},
    6: {"usd": 9.00},
    12: {"usd": 15.00}
}

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, status TEXT DEFAULT 'base', 
        sub_until REAL DEFAULT 0, last_promo_date TEXT DEFAULT '', promos_today INTEGER DEFAULT 0, 
        referrals_count INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0,
        balance REAL DEFAULT 0.0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY, type TEXT, value INTEGER, creator_id INTEGER, max_uses INTEGER DEFAULT 1, used_by TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        code TEXT PRIMARY KEY, user_id INTEGER, username TEXT, product_type TEXT, 
        amount INTEGER, expected_ton REAL, expected_rub INTEGER, payment_method TEXT, status TEXT DEFAULT 'pending'
    )''')
    conn.commit()
    conn.close()

init_db()

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_get_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT username, status, sub_until, last_promo_date, promos_today, referrals_count, is_banned, balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"username": row[0], "status": row[1], "sub_until": row[2], "last_promo_date": row[3], "promos_today": row[4], "referrals_count": row[5], "is_banned": bool(row[6]), "balance": row[7]}
    return None

def db_create_user(user_id, username):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def db_update_user(user_id, **kwargs):
    conn = get_conn()
    c = conn.cursor()
    for key, val in kwargs.items():
        if key == "is_banned": val = 1 if val else 0
        c.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (val, user_id))
    conn.commit()
    conn.close()

def db_get_code(code):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT type, value, creator_id, max_uses, used_by FROM codes WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"type": row[0], "value": row[1], "creator_id": row[2], "max_uses": row[3], "used_by": row[4].split(",") if row[4] else []}
    return None

def db_create_code(code, ctype, value, creator_id, max_uses=1):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO codes (code, type, value, creator_id, max_uses) VALUES (?, ?, ?, ?, ?)", (code, ctype, value, creator_id, max_uses))
    conn.commit()
    conn.close()

def db_add_code_use(code, user_id):
    code_data = db_get_code(code)
    used = code_data["used_by"]
    used.append(str(user_id))
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE codes SET used_by = ? WHERE code = ?", (",".join(used), code))
    conn.commit()
    conn.close()

def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return True 

def get_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("⭐ Купить звёзды"), types.KeyboardButton("💎 Купить Premium"))
    markup.add(types.KeyboardButton("👤 Профиль"), types.KeyboardButton("💰 Баланс"))
    markup.add(types.KeyboardButton("🎁 Промокоды"), types.KeyboardButton("📜 Сертификаты"))
    markup.add(types.KeyboardButton("🔗 Рефералы"), types.KeyboardButton("📺 Канал"))
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("🛡 Админ-панель"))
    return markup

def gen_code(prefix):
    return prefix + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

STATUS_INFO = {
    "base": {"name": "Базовый", "discount": 0, "promos_per_day": 0},
    "pro": {"name": "PRO", "discount": 0.10, "promos_per_day": 1},
    "vip": {"name": "VIP", "discount": 0.20, "promos_per_day": 3}
}

async def get_ton_price():
    url = "https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["price"])
    except Exception as e:
        print(f"⚠️ Ошибка получения курса TON: {e}")
    return FALLBACK_TON_PRICE

def get_ton_price_sync():
    try:
        return asyncio.run(get_ton_price())
    except Exception:
        return FALLBACK_TON_PRICE

# --- ОПЛАТА В MYSTARS ---
async def pay_mystars_order(payment_instruction, is_premium=False):
    if not TON_MNEMONIC:
        print("❌ TON_WALLET_MNEMONIC не задан")
        return False
        
    for attempt in range(3):
        try:
            provider = LiteBalancer.from_mainnet_config(trust_level=1)
            await provider.start_up()
            
            wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=TON_MNEMONIC)
            pay_to = payment_instruction.pay_to_address
            amount_ton = Decimal(str(payment_instruction.amount))
            amount_nano = int(amount_ton * 10**9)
            memo = payment_instruction.memo

            body = begin_cell().store_uint(0, 32).store_string(memo).end_cell()
            seqno = await wallet.get_seqno()
            
            await wallet.transfer(
                destination=Address(pay_to),
                amount=amount_nano,
                body=body,
                seqno=seqno
            )
            await provider.close_all()
            print(f"✅ TON отправлен в MyStars! Сумма: {amount_ton}, Memo: {memo}")
            return True
        except Exception as e:
            print(f"⚠️ Попытка {attempt + 1} не удалась: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
            
    print("❌ Все 3 попытки отправки TON провалились.")
    return False

# --- ВЫВОД СРЕДСТВ (TON) ---
async def withdraw_ton(user_id, amount_ton, to_address):
    if not TON_MNEMONIC:
        print("❌ TON_WALLET_MNEMONIC не задан")
        return False
    try:
        provider = LiteBalancer.from_mainnet_config(trust_level=1)
        await provider.start_up()
        wallet = await WalletV4R2.from_mnemonic(provider=provider, mnemonics=TON_MNEMONIC)
        
        amount_nano = int(amount_ton * 10**9)
        body = begin_cell().store_uint(0, 32).store_string("Withdraw").end_cell()
        seqno = await wallet.get_seqno()
        
        await wallet.transfer(
            destination=Address(to_address),
            amount=amount_nano,
            body=body,
            seqno=seqno
        )
        await provider.close_all()
        print(f"✅ Вывод {amount_ton} TON на {to_address} успешен.")
        return True
    except Exception as e:
        print(f"❌ Ошибка вывода TON: {e}")
        return False

# --- АВТО-ПРОВЕРКА КРИПТО-ОПЛАТ ---
async def check_payments_loop():
    if not TONCENTER_API_KEY:
        return
    url = f"https://toncenter.com/api/v3/transactions?account={WALLET_RAW}&limit=20"
    headers = {"X-API-Key": TONCENTER_API_KEY}
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        await asyncio.sleep(30)
                        continue
                    data = await response.json()
                    for tx in data.get("transactions", []):
                        in_msg = tx.get("in_msg")
                        if not in_msg: continue
                        value = int(in_msg.get("value", 0))
                        if value == 0: continue
                        comment = ""
                        msg_data = in_msg.get("message_content", {})
                        if msg_data.get("decoded", {}).get("@type") == "text":
                            comment = msg_data["decoded"]["text"]
                        if comment:
                            comment = comment.strip().upper()
                            conn = get_conn()
                            c = conn.cursor()
                            c.execute("SELECT user_id, username, product_type, amount, expected_ton FROM orders WHERE code = ? AND status = 'pending' AND payment_method = 'crypto'", (comment,))
                            order = c.fetchone()
                            if order:
                                uid, uname, product_type, amount, expected_ton = order
                                received_ton = value / 10**9
                                if received_ton < (expected_ton * 0.95):
                                    conn.close()
                                    continue
                                c.execute("UPDATE orders SET status = 'processing' WHERE code = ?", (comment,))
                                conn.commit()
                                conn.close()
                                print(f"✅ Крипта {comment} от @{uname} ({received_ton} TON).")
                                try:
                                    if product_type == "stars":
                                        m_order = client.create_order(type="stars", recipient=uname, quantity=amount, payment_currency="ton", callback_url="https://plamechat.bothost.tech/webhook")
                                        success = await pay_mystars_order(m_order.payment)
                                    elif product_type == "premium":
                                        m_order = client.create_order(type="premium", recipient=uname, duration=amount, payment_currency="ton", callback_url="https://plamechat.bothost.tech/webhook")
                                        success = await pay_mystars_order(m_order.payment, is_premium=True)
                                    else:
                                        success = False

                                    if success:
                                        msg_text = f"🎉 Оплата получена! " + (f"Звёзды ({amount} шт.) летят на @{uname}." if product_type == "stars" else f"Premium на {amount} мес. активируется.")
                                        bot.send_message(uid, msg_text)
                                        c.execute("UPDATE orders SET status = 'done' WHERE code = ?", (comment,))
                                        conn.commit()
                                    else:
                                        c.execute("UPDATE orders SET status = 'pending' WHERE code = ?", (comment,))
                                        conn.commit()
                                except Exception as e:
                                    print(f"❌ Ошибка: {e}")
                                    c.execute("UPDATE orders SET status = 'pending' WHERE code = ?", (comment,))
                                    conn.commit()
                            conn.close()
        except Exception as e:
            print(f"Ошибка проверки TonCenter: {e}")
        await asyncio.sleep(20)

# --- СТАРТ ---
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    uname = message.from_user.username or f"id{uid}"
    args = message.text.split()
    ref_id = None
    cert_code = None
    if len(args) > 1:
        if args[1].startswith("ref_"):
            try: ref_id = int(args[1].split("_")[1])
            except: pass
        elif args[1].startswith("cert_"):
            cert_code = args[1].split("_")[1]

    u = db_get_user(uid)
    if not u:
        db_create_user(uid, uname)
        u = db_get_user(uid)
        if ref_id and ref_id != uid:
            ref_u = db_get_user(ref_id)
            if ref_u:
                db_update_user(ref_id, referrals_count=ref_u["referrals_count"] + 1)
                try: bot.send_message(ref_id, f"🎉 Новый реферал!")
                except: pass

    if u["is_banned"]: return bot.send_message(message.chat.id, "⛔ Вы забанены.")
    if not check_subscription(uid):
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📺 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME[1:]}"), types.InlineKeyboardButton("✅ Проверить", callback_data="check_sub"))
        return bot.send_message(message.chat.id, "Подпишись на канал.", reply_markup=markup)

    # --- ОБРАБОТКА СЕРТИФИКАТА ---
    if cert_code:
        c = db_get_code(cert_code)
        if not c or c["type"] != "cert":
            bot.send_message(message.chat.id, "❌ Сертификат не найден или уже использован.")
        else:
            if str(uid) in c["used_by"]:
                bot.send_message(message.chat.id, "❌ Ты уже активировал этот сертификат.")
            elif uid == c["creator_id"]:
                bot.send_message(message.chat.id, "❌ Нельзя активировать свой сертификат.")
            elif len(c["used_by"]) >= c["max_uses"]:
                bot.send_message(message.chat.id, "❌ Лимит активаций исчерпан.")
            else:
                db_add_code_use(cert_code, uid)
                u = db_get_user(uid)
                new_balance = u["balance"] + (c["value"] * 0.02)
                db_update_user(uid, balance=new_balance)
                bot.send_message(message.chat.id, f"🎉 <b>Сертификат активирован!</b>\n\nТебе начислено <b>{c['value']} Stars</b> (в TON на баланс).\nТвой новый баланс: <b>{new_balance:.3f} TON</b>.", parse_mode="HTML")

    bot.send_message(message.chat.id, f"👋 Привет, @{uname}!\n💎 Статус: <b>{STATUS_INFO[u['status']]['name']}</b>\n\nВыбери действие:", reply_markup=get_main_menu(uid), parse_mode="HTML")

# --- ПРОФИЛЬ ---
@bot.message_handler(func=lambda message: message.text == "👤 Профиль")
def profile(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    if not u: return start(message)
    sub = f"{int((u['sub_until'] - time.time()) / 86400)} дн." if u.get("sub_until", 0) > time.time() else "Нет"
    text = f"👤 ID: <code>{uid}</code>\n📛 @{u['username']}\n💎 Статус: <b>{STATUS_INFO[u['status']]['name']}</b>\n📅 Подписка: {sub}\n💰 Баланс: {u['balance']:.3f} TON\n👥 Рефералов: {u['referrals_count']}"
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# --- БАЛАНС И ВЫВОД ---
@bot.message_handler(func=lambda message: message.text == "💰 Баланс")
def balance_menu(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💸 Вывести TON", callback_data="withdraw_input"))
    bot.send_message(message.chat.id, f"💰 <b>Твой баланс:</b> {u['balance']:.3f} TON", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "withdraw_input")
def withdraw_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи адрес TON-кошелька, куда вывести средства:")
    bot.register_next_step_handler(msg, process_withdraw)

def process_withdraw(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    to_address = message.text.strip()
    
    if u["balance"] <= 0.1:
        return bot.send_message(message.chat.id, "❌ Минимальная сумма для вывода — 0.1 TON.")
    
    if not to_address.startswith("UQ") and not to_address.startswith("EQ"):
        return bot.send_message(message.chat.id, "❌ Неверный адрес TON (должен начинаться на UQ или EQ).")

    bot.send_message(message.chat.id, "⏳ Отправляю TON...")
    
    success = asyncio.run(withdraw_ton(uid, u["balance"], to_address))
    
    if success:
        db_update_user(uid, balance=0.0)
        bot.send_message(message.chat.id, f"✅ Успешно выведено {u['balance']:.3f} TON на адрес {to_address}.")
    else:
        bot.send_message(message.chat.id, "❌ Ошибка вывода. Проверь баланс или попробуй позже.")

# --- ПРОМОКОДЫ ---
@bot.message_handler(func=lambda message: message.text == "🎁 Промокоды")
def promo_menu(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    limit = STATUS_INFO[u["status"]]["promos_per_day"]
    text = "🎁 <b>Промокоды</b>\n\n"
    if limit == 0:
        text += "Твой статус не позволяет создавать промокоды."
    else:
        today = time.strftime('%Y-%m-%d')
        if u["last_promo_date"] != today:
            db_update_user(uid, promos_today=0, last_promo_date=today)
            u = db_get_user(uid)
        text += f"Лимит на сегодня: {limit - u['promos_today']} из {limit}.\nМакс. номинал: <b>30 Stars</b>."
    markup = types.InlineKeyboardMarkup(row_width=1)
    if limit > 0 and u["promos_today"] < limit:
        markup.add(types.InlineKeyboardButton("➕ Создать промокод", callback_data="create_promo"))
    markup.add(types.InlineKeyboardButton("🎟 Активировать", callback_data="activate_promo_input"))
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "create_promo")
def create_promo_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи номинал (макс. 30):")
    bot.register_next_step_handler(msg, process_promo_create)

def process_promo_create(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    try:
        val = int(message.text.strip())
        if val < 1 or val > 30: return bot.send_message(message.chat.id, "❌ От 1 до 30 звёзд!")
        code = gen_code("PROMO-")
        db_create_code(code, "promo", val, uid, 1)
        db_update_user(uid, promos_today=u["promos_today"] + 1)
        bot.send_message(message.chat.id, f"✅ Промокод: <code>{code}</code>\nНоминал: {val} Stars", parse_mode="HTML")
    except: bot.send_message(message.chat.id, "❌ Введи число.")

@bot.callback_query_handler(func=lambda call: call.data == "activate_promo_input")
def activate_promo_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи промокод:")
    bot.register_next_step_handler(msg, process_promo_activate)

def process_promo_activate(message):
    uid = message.from_user.id
    code = message.text.strip().upper()
    p = db_get_code(code)
    if not p or p["type"] != "promo": return bot.send_message(message.chat.id, "❌ Не найден.")
    if str(uid) in p["used_by"] or uid == p["creator_id"] or len(p["used_by"]) >= p["max_uses"]:
        return bot.send_message(message.chat.id, "❌ Нельзя активировать.")
    bot.send_message(message.chat.id, f"🎉 Активировано! Начислено {p['value']} Stars.")
    db_add_code_use(code, uid)

# --- СЕРТИФИКАТЫ ---
@bot.message_handler(func=lambda message: message.text == "📜 Сертификаты")
def certificates_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🛒 Купить сертификат", callback_data="cert_buy"), types.InlineKeyboardButton("🎟 Активировать", callback_data="cert_activate_input"))
    bot.send_message(message.chat.id, "📜 Сертификаты (ссылка одноразовая)", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "cert_buy")
def cert_buy_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи номинал сертификата:")
    bot.register_next_step_handler(msg, process_cert_buy)

def process_cert_buy(message):
    uid = message.from_user.id
    try:
        val = int(message.text.strip())
        if val < 10: return bot.send_message(message.chat.id, "❌ Минимум 10.")
        code = gen_code("CERT-")
        db_create_code(code, "cert", val, uid, 1)
        link = f"https://t.me/{BOT_USERNAME}?start=cert_{code}"
        bot.send_message(message.chat.id, f"✅ Сертификат на {val} Stars!\nСсылка:\n<code>{link}</code>", parse_mode="HTML")
    except: bot.send_message(message.chat.id, "❌ Введи число.")

@bot.callback_query_handler(func=lambda call: call.data == "cert_activate_input")
def cert_activate_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи код сертификата (CERT-XXXXXX):")
    bot.register_next_step_handler(msg, process_cert_activate)

def process_cert_activate(message):
    uid = message.from_user.id
    code = message.text.strip().upper()
    c = db_get_code(code)
    if not c or c["type"] != "cert": return bot.send_message(message.chat.id, "❌ Не найден.")
    if str(uid) in c["used_by"] or uid == c["creator_id"] or len(c["used_by"]) >= c["max_uses"]:
        return bot.send_message(message.chat.id, "❌ Нельзя активировать.")
    u = db_get_user(uid)
    new_balance = u["balance"] + (c["value"] * 0.02)
    db_update_user(uid, balance=new_balance)
    bot.send_message(message.chat.id, f"🎉 Сертификат активирован! Начислено {c['value']} Stars ({c['value']*0.02:.3f} TON на баланс).")
    db_add_code_use(code, uid)

# --- КУПИТЬ ЗВЁЗДЫ ---
@bot.message_handler(func=lambda message: message.text == "⭐ Купить звёзды")
def buy_stars_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for stars, prices in STAR_PACKAGES.items():
        markup.add(types.InlineKeyboardButton(f"⭐ {stars} Stars — ${prices['usd']}", callback_data=f"buy_stars_{stars}"))
    markup.add(types.InlineKeyboardButton("✏️ Своё количество (50–10000)", callback_data="buy_stars_custom"))
    bot.send_message(message.chat.id, "Выбери пакет звёзд:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "buy_stars_custom")
def buy_stars_custom_input(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи количество звёзд (от 50 до 10000):")
    bot.register_next_step_handler(msg, process_stars_custom)

def process_stars_custom(message):
    uid = message.from_user.id
    uname = message.from_user.username or f"id{uid}"
    try:
        stars = int(message.text.strip())
        if stars < 50 or stars > 10000:
            return bot.send_message(message.chat.id, "❌ Минимальное количество для покупки — 50 звёзд.")
        base_price_usd = stars * 0.025
        disc = STATUS_INFO[db_get_user(uid)["status"]]["discount"]
        final_price_usd = round(base_price_usd * (1 - disc), 2)
        
        ton_rate = get_ton_price_sync()
        final_price_ton = round(final_price_usd / ton_rate, 3)
        final_price_rub = int(final_price_usd * USD_RUB_RATE)

        pay_code = gen_code("PAY-")
        conn = get_conn()
        c = conn.cursor()
        c.execute("INSERT INTO orders (code, user_id, username, product_type, amount, expected_ton, expected_rub, payment_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                  (pay_code, uid, uname, 'stars', stars, final_price_ton, final_price_rub, 'pending'))
        conn.commit()
        conn.close()

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🪙 Криптовалюта (TON)", callback_data=f"pay_crypto_{pay_code}"))
        markup.add(types.InlineKeyboardButton("💳 Рубли (СБП)", callback_data=f"pay_rub_{pay_code}"))

        bot.send_message(message.chat.id, 
            f"✅ Заказ на {stars} Stars сформирован!\n"
            f"К оплате: <b>${final_price_usd}</b> или <b>{final_price_rub} ₽</b>\n\n"
            f"Выбери способ оплаты:", reply_markup=markup, parse_mode="HTML")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи число.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_stars_") and call.data != "buy_stars_custom")
def process_buy_stars(call):
    bot.answer_callback_query(call.id)
    stars = int(call.data.split("_")[2])
    uid = call.from_user.id
    uname = call.from_user.username or f"id{uid}"
    prices = STAR_PACKAGES[stars]
    disc = STATUS_INFO[db_get_user(uid)["status"]]["discount"]
    final_price_usd = round(prices["usd"] * (1 - disc), 2)
    
    ton_rate = get_ton_price_sync()
    final_price_ton = round(final_price_usd / ton_rate, 3)
    final_price_rub = int(final_price_usd * USD_RUB_RATE)

    pay_code = gen_code("PAY-")
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO orders (code, user_id, username, product_type, amount, expected_ton, expected_rub, payment_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
              (pay_code, uid, uname, 'stars', stars, final_price_ton, final_price_rub, 'pending'))
    conn.commit()
    conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🪙 Криптовалюта (TON)", callback_data=f"pay_crypto_{pay_code}"))
    markup.add(types.InlineKeyboardButton("💳 Рубли (СБП)", callback_data=f"pay_rub_{pay_code}"))

    bot.send_message(call.message.chat.id, 
        f"✅ Заказ на {stars} Stars сформирован!\n"
        f"К оплате: <b>${final_price_usd}</b> или <b>{final_price_rub} ₽</b>\n\n"
        f"Выбери способ оплаты:", reply_markup=markup, parse_mode="HTML")

# --- КУПИТЬ PREMIUM ---
@bot.message_handler(func=lambda message: message.text == "💎 Купить Premium")
def buy_premium_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for months, prices in PREMIUM_PACKAGES.items():
        markup.add(types.InlineKeyboardButton(f"💎 Premium {months} мес. — ${prices['usd']}", callback_data=f"buy_prem_{months}"))
    bot.send_message(message.chat.id, "Выбери срок Premium:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_prem_"))
def process_buy_premium(call):
    bot.answer_callback_query(call.id)
    months = int(call.data.split("_")[2])
    uid = call.from_user.id
    uname = call.from_user.username or f"id{uid}"
    prices = PREMIUM_PACKAGES[months]
    disc = STATUS_INFO[db_get_user(uid)["status"]]["discount"]
    final_price_usd = round(prices["usd"] * (1 - disc), 2)
    
    ton_rate = get_ton_price_sync()
    final_price_ton = round(final_price_usd / ton_rate, 3)
    final_price_rub = int(final_price_usd * USD_RUB_RATE)

    pay_code = gen_code("PAY-")
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO orders (code, user_id, username, product_type, amount, expected_ton, expected_rub, payment_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
              (pay_code, uid, uname, 'premium', months, final_price_ton, final_price_rub, 'pending'))
    conn.commit()
    conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🪙 Криптовалюта (TON)", callback_data=f"pay_crypto_{pay_code}"))
    markup.add(types.InlineKeyboardButton("💳 Рубли (СБП)", callback_data=f"pay_rub_{pay_code}"))

    bot.send_message(call.message.chat.id, 
        f"✅ Заказ на Premium {months} мес. сформирован!\n"
        f"К оплате: <b>${final_price_usd}</b> или <b>{final_price_rub} ₽</b>\n\n"
        f"Выбери способ оплаты:", reply_markup=markup, parse_mode="HTML")

# --- ВЫБОР СПОСОБА ОПЛАТЫ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def process_payment_method(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    method = parts[1]
    pay_code = parts[2]

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT product_type, amount, expected_ton, expected_rub FROM orders WHERE code = ? AND status = 'pending'", (pay_code,))
    order = c.fetchone()
    if not order:
        conn.close()
        return bot.send_message(call.message.chat.id, "❌ Заказ не найден.")
    product_type, amount, expected_ton, expected_rub = order
    c.execute("UPDATE orders SET payment_method = ? WHERE code = ?", (method, pay_code))
    conn.commit()
    conn.close()

    if method == "crypto":
        bot.send_message(call.message.chat.id, 
            f"🪙 <b>Оплата криптовалютой</b>\n\n"
            f"Переведи <b>ровно {expected_ton} TON</b> на кошелек:\n"
            f"<code>{TON_WALLET}</code>\n\n"
            f"⚠️ <b>ВАЖНО!</b> В комментарии укажи код:\n"
            f"<code>{pay_code}</code>\n\n"
            f"Бот автоматически проверит оплату.", parse_mode="HTML")
    elif method == "rub":
        text = (
            f"💳 <b>Оплата рублями (СБП)</b>\n\n"
            f"Сумма: <b>{expected_rub} ₽</b>\n"
            f"Получатель: <b>{SBP_NAME}</b>\n"
            f"Телефон для перевода: <code>{SBP_PHONE}</code>\n\n"
            f"⚠️ <b>ВАЖНО!</b> В комментарии к переводу укажи код:\n"
            f"<code>{pay_code}</code>\n\n"
            f"После перевода нажми кнопку ниже."
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"user_paid_rub_{pay_code}"))
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

# --- ПОДТВЕРЖДЕНИЕ ОПЛАТЫ РУБЛЯМИ (АДМИН) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("user_paid_rub_"))
def user_paid_rub(call):
    pay_code = call.data.split("_")[3]
    bot.answer_callback_query(call.id, "✅ Заявка отправлена. Ожидай проверку.")
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm_rub_{pay_code}"))
    
    bot.send_message(ADMIN_ID, 
        f"🔔 <b>Пользователь нажал 'Я оплатил' (СБП)</b>\n"
        f"Код: <code>{pay_code}</code>\n"
        f"Проверь поступление и подтверди.", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_rub_"))
def confirm_rub(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "❌ Нет доступа.")
    
    pay_code = call.data.split("_")[2]
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, username, product_type, amount FROM orders WHERE code = ? AND status = 'pending'", (pay_code,))
    order = c.fetchone()
    
    if not order:
        conn.close()
        return bot.answer_callback_query(call.id, "❌ Заказ не найден.")
    
    uid, uname, product_type, amount = order
    c.execute("UPDATE orders SET status = 'processing' WHERE code = ?", (pay_code,))
    conn.commit()
    conn.close()
    
    bot.answer_callback_query(call.id, "⏳ Закупаю...")
    
    async def do_buy():
        try:
            if product_type == "stars":
                m_order = client.create_order(type="stars", recipient=uname, quantity=amount, payment_currency="ton", callback_url="https://plamechat.bothost.tech/webhook")
                success = await pay_mystars_order(m_order.payment)
            elif product_type == "premium":
                m_order = client.create_order(type="premium", recipient=uname, duration=amount, payment_currency="ton", callback_url="https://plamechat.bothost.tech/webhook")
                success = await pay_mystars_order(m_order.payment, is_premium=True)
            else:
                success = False

            if success:
                msg_text = f"🎉 Оплата подтверждена! " + (f"Звёзды ({amount} шт.) летят на @{uname}." if product_type == "stars" else f"Premium на {amount} мес. активируется.")
                bot.send_message(uid, msg_text)
                bot.send_message(ADMIN_ID, f"✅ Заказ {pay_code} выдан.")
                conn2 = get_conn()
                c2 = conn2.cursor()
                c2.execute("UPDATE orders SET status = 'done' WHERE code = ?", (pay_code,))
                conn2.commit()
                conn2.close()
            else:
                bot.send_message(ADMIN_ID, f"❌ Ошибка закупки {pay_code}.")
                conn2 = get_conn()
                c2 = conn2.cursor()
                c2.execute("UPDATE orders SET status = 'pending' WHERE code = ?", (pay_code,))
                conn2.commit()
                conn2.close()
        except Exception as e:
            print(f"Ошибка: {e}")
    
    asyncio.run(do_buy())

# --- АДМИН-ПАНЕЛЬ ---
@bot.message_handler(func=lambda message: message.text == "🛡 Админ-панель")
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("💎 Подписка", callback_data="adm_sub"), types.InlineKeyboardButton("🚫 Бан", callback_data="adm_ban"))
    markup.add(types.InlineKeyboardButton("✅ Разбан", callback_data="adm_unban"), types.InlineKeyboardButton("📢 Рассылка", callback_data="adm_broadcast"))
    markup.add(types.InlineKeyboardButton("💬 ЛС", callback_data="adm_msg"), types.InlineKeyboardButton("🎁 Промокод", callback_data="adm_promo"))
    markup.add(types.InlineKeyboardButton("📜 Сертификат", callback_data="adm_cert"))
    bot.send_message(message.chat.id, "🛡 <b>Админ-панель</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_"))
def admin_actions(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "❌ Нет доступа.")
    bot.answer_callback_query(call.id)
    act = call.data.split("_")[1]
    if act == "sub":
        msg = bot.send_message(call.message.chat.id, "Введи: <code>@user pro 30</code>", parse_mode="HTML"); bot.register_next_step_handler(msg, admin_grant_sub)
    elif act == "ban":
        msg = bot.send_message(call.message.chat.id, "Введи @username:"); bot.register_next_step_handler(msg, admin_ban)
    elif act == "unban":
        msg = bot.send_message(call.message.chat.id, "Введи @username:"); bot.register_next_step_handler(msg, admin_unban)
    elif act == "broadcast":
        msg = bot.send_message(call.message.chat.id, "Введи текст:"); bot.register_next_step_handler(msg, admin_broadcast)
    elif act == "msg":
        msg = bot.send_message(call.message.chat.id, "Введи: <code>@user Текст</code>", parse_mode="HTML"); bot.register_next_step_handler(msg, admin_send_msg)
    elif act == "promo":
        msg = bot.send_message(call.message.chat.id, "Введи: <code>номинал лимит</code>"); bot.register_next_step_handler(msg, admin_create_promo)
    elif act == "cert":
        msg = bot.send_message(call.message.chat.id, "Введи: <code>номинал лимит</code>"); bot.register_next_step_handler(msg, admin_create_cert)

def admin_create_promo(message):
    try:
        parts = message.text.split()
        code = gen_code("PROMO-")
        db_create_code(code, "promo", int(parts[0]), ADMIN_ID, int(parts[1]))
        bot.send_message(message.chat.id, f"✅ Промокод: <code>{code}</code>", parse_mode="HTML")
    except: bot.send_message(message.chat.id, "❌ Ошибка.")

def admin_create_cert(message):
    try:
        parts = message.text.split()
        code = gen_code("CERT-")
        db_create_code(code, "cert", int(parts[0]), ADMIN_ID, int(parts[1]))
        link = f"https://t.me/{BOT_USERNAME}?start=cert_{code}"
        bot.send_message(message.chat.id, f"✅ Сертификат: <code>{link}</code>", parse_mode="HTML")
    except: bot.send_message(message.chat.id, "❌ Ошибка.")

def find_user(identifier):
    identifier = str(identifier).replace("@", "").lower()
    conn = get_conn()
    c = conn.cursor()
    if identifier.isdigit():
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (int(identifier),))
    else:
        c.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (identifier,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def admin_grant_sub(message):
    try:
        parts = message.text.split(); uid = find_user(parts[0])
        if not uid: return bot.send_message(message.chat.id, "❌ Не найден.")
        db_update_user(uid, status=parts[1].lower(), sub_until=time.time() + (int(parts[2]) * 86400))
        bot.send_message(message.chat.id, f"✅ Выдано.")
    except: bot.send_message(message.chat.id, "❌ Ошибка.")

def admin_ban(message):
    uid = find_user(message.text.strip())
    if uid:
        db_update_user(uid, is_banned=True)
        bot.send_message(message.chat.id, f"✅ Забанен.")
    else: bot.send_message(message.chat.id, "❌ Не найден.")

def admin_unban(message):
    uid = find_user(message.text.strip())
    if uid:
        db_update_user(uid, is_banned=False)
        bot.send_message(message.chat.id, f"✅ Разбанен.")
    else: bot.send_message(message.chat.id, "❌ Не найден.")

def admin_broadcast(message):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = c.fetchall()
    conn.close()
    ok, fail = 0, 0
    for row in users:
        try: bot.send_message(row[0], message.text); ok += 1
        except: fail += 1
    bot.send_message(message.chat.id, f"✅ Рассылка: {ok} ок, {fail} ошибок.")

def admin_send_msg(message):
    try:
        parts = message.text.split(maxsplit=1); uid = find_user(parts[0])
        if not uid: return bot.send_message(message.chat.id, "❌ Не найден.")
        bot.send_message(uid, f"💬 <b>Админ:</b>\n{parts[1]}", parse_mode="HTML")
        bot.send_message(message.chat.id, f"✅ Отправлено.")
    except: bot.send_message(message.chat.id, "❌ Ошибка.")

# --- ПРОЧЕЕ ---
@bot.message_handler(func=lambda message: message.text == "🔗 Рефералы")
def referrals_menu(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    bot.send_message(message.chat.id, f"🔗 Твоя ссылка:\n<code>{link}</code>\n\nПриглашено: {u['referrals_count']}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📤 Поделиться", url=f"https://t.me/share/url?url={link}")), parse_mode="HTML")

@bot.message_handler(func=lambda message: message.text == "📺 Канал")
def channel_menu(message):
    bot.send_message(message.chat.id, f"📺 {CHANNEL_USERNAME}", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Перейти", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")))

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
    bot.answer_callback_query(call.id)
    if check_subscription(call.from_user.id): start(call.message)
    else: bot.send_message(call.message.chat.id, "❌ Вы не подписаны.")

# --- ЗАПУСК ---
if __name__ == "__main__":
    print("🚀 Бот запущен.")
    if not TON_MNEMONIC:
        print("⚠️ ВНИМАНИЕ: TON_WALLET_MNEMONIC не задан в .env!")
    if not TONCENTER_API_KEY:
        print("⚠️ ВНИМАНИЕ: TONCENTER_API_KEY не задан в .env!")

    def run_checker():
        asyncio.run(check_payments_loop())
    
    t = threading.Thread(target=run_checker, daemon=True)
    t.start()
    print("🕒 Авто-проверка TON запущена (каждые 20 сек).")
    
    bot.infinity_polling(timeout=10, long_polling_timeout=5)