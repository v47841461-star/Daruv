import functools
import hashlib
import html
import json
import os
import random
import sqlite3
import threading
import time
import traceback

import requests

print = functools.partial(print, flush=True)

# ============================ НАСТРОЙКИ ============================
TOKEN = "8986706200:AAHlBA1lyeA_pnwUfk4CNm5l2JYdRXpLQ50"
ADMIN_ID = 8716189150
BOT_USERNAME = "daruv_bot"
DB_FILE = os.environ.get("DB_FILE", "daruv.db")

UNMUTE_STARS = 5              # цена выкупа размута
PROTECT_SECONDS = 3600        # защита от мута после выкупа (1 час)
AUTOREPLY_DELAY = 300         # через сколько секунд молчания сработает автоответчик
AUTOREPLY_COOLDOWN = 6 * 3600 # не чаще раза в 6 часов на один чат
TROLL_MAX = 40                # максимум сообщений .troll

BASE = os.path.dirname(os.path.abspath(__file__))
API = f"https://api.telegram.org/bot{TOKEN}/"

# Слоты фото: ключ -> (название, файл рядом с bot.py)
SLOTS = {
    "connect": "Тутор подключения (/start)",
    "connected": "Вы успешно подключили (меню)",
    "help": "Инструкция",
    "commands": "Список команд / настройки",
}

# ============================ БАЗА ============================
lock = threading.RLock()
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row


def q(sql, args=(), one=False):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def init_db():
    with lock:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined INTEGER);
        CREATE TABLE IF NOT EXISTS conns(user_id INTEGER PRIMARY KEY, conn_id TEXT, enabled INTEGER, can_reply INTEGER, can_delete INTEGER);
        CREATE TABLE IF NOT EXISTS mutes(owner_id INTEGER, chat_id INTEGER, notice_mid INTEGER, since INTEGER, PRIMARY KEY(owner_id, chat_id));
        CREATE TABLE IF NOT EXISTS protect(owner_id INTEGER, chat_id INTEGER, until INTEGER, PRIMARY KEY(owner_id, chat_id));
        CREATE TABLE IF NOT EXISTS anti(owner_id INTEGER, chat_id INTEGER, PRIMARY KEY(owner_id, chat_id));
        CREATE TABLE IF NOT EXISTS msgs(owner_id INTEGER, chat_id INTEGER, mid INTEGER, text TEXT, tries INTEGER, ts INTEGER, PRIMARY KEY(owner_id, chat_id, mid));
        CREATE TABLE IF NOT EXISTS photos(key TEXT PRIMARY KEY, file_id TEXT, sig TEXT);
        CREATE TABLE IF NOT EXISTS auto(user_id INTEGER PRIMARY KEY, enabled INTEGER, text TEXT);
        CREATE TABLE IF NOT EXISTS auto_sent(owner_id INTEGER, chat_id INTEGER, ts INTEGER, PRIMARY KEY(owner_id, chat_id));
        """)
        db.commit()


# ============================ TELEGRAM API ============================
tl = threading.local()


def tg(method, files=None, **params):
    """Возвращает result или None. Описание ошибки лежит в tl.err."""
    tl.err = ""
    for attempt in range(2):
        try:
            if files:
                data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                        for k, v in params.items()}
                r = requests.post(API + method, data=data, files=files, timeout=60)
            else:
                r = requests.post(API + method, json=params, timeout=60)
            res = r.json()
        except Exception as e:
            tl.err = str(e)
            print("API error", method, e)
            return None
        if res.get("ok"):
            return res["result"]
        tl.err = res.get("description", "")
        wait = res.get("parameters", {}).get("retry_after")
        if wait and attempt == 0:
            time.sleep(wait + 1)
            continue
        print("API fail", method, tl.err)
        return None
    return None


def esc(s):
    return html.escape(str(s or ""))


def now():
    return int(time.time())


def btn(text, cb=None, url=None, style=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = cb
    if style:
        b["style"] = style  # success / danger / primary (цветные кнопки)
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


EMPTY_KB = {"inline_keyboard": []}


# ============================ ФОТО ============================
def local_photo(key):
    p = os.path.join(BASE, key + ".jpg")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def photo_id(key):
    r = q("SELECT file_id, sig FROM photos WHERE key=?", (key,), one=True)
    if not r:
        return None
    if r["sig"] == "admin":
        return r["file_id"]
    data = local_photo(key)
    if data and hashlib.md5(data).hexdigest()[:12] == r["sig"]:
        return r["file_id"]
    return None


def cache_photo(key, msg, data):
    try:
        fid = msg["photo"][-1]["file_id"]
        q("INSERT OR REPLACE INTO photos VALUES(?,?,?)",
          (key, fid, hashlib.md5(data).hexdigest()[:12]))
    except Exception as e:
        print("cache_photo error", e)


def send_photo(chat_id, key, caption, markup=None):
    p = dict(chat_id=chat_id, caption=caption, parse_mode="HTML")
    if markup:
        p["reply_markup"] = markup
    fid = photo_id(key)
    if fid:
        r = tg("sendPhoto", photo=fid, **p)
        if r:
            return r
    data = local_photo(key)
    if data:
        r = tg("sendPhoto", files={"photo": (key + ".jpg", data)}, **p)
        if r:
            cache_photo(key, r, data)
            return r
    p2 = dict(chat_id=chat_id, text=caption, parse_mode="HTML")
    if markup:
        p2["reply_markup"] = markup
    return tg("sendMessage", **p2)


def show(chat_id, mid, key, caption, markup=None):
    """Заменяет сообщение на новый экран (старые кнопки исчезают)."""
    markup = markup or EMPTY_KB
    done = False
    if key:
        media = {"type": "photo", "caption": caption, "parse_mode": "HTML"}
        fid = photo_id(key)
        if fid:
            media["media"] = fid
            r = tg("editMessageMedia", chat_id=chat_id, message_id=mid, media=media, reply_markup=markup)
            done = bool(r) or "not modified" in tl.err
        else:
            data = local_photo(key)
            if data:
                media["media"] = "attach://p"
                r = tg("editMessageMedia", files={"p": (key + ".jpg", data)},
                       chat_id=chat_id, message_id=mid, media=media, reply_markup=markup)
                if r and isinstance(r, dict) and r.get("photo"):
                    cache_photo(key, r, data)
                done = bool(r) or "not modified" in tl.err
    else:
        r = tg("editMessageText", chat_id=chat_id, message_id=mid, text=caption,
               parse_mode="HTML", reply_markup=markup)
        done = bool(r) or "not modified" in tl.err
    if done:
        return
    tg("deleteMessage", chat_id=chat_id, message_id=mid)
    send_screen(chat_id, key, caption, markup)


def send_screen(chat_id, key, caption, markup=None):
    if key:
        send_photo(chat_id, key, caption, markup)
    else:
        p = dict(chat_id=chat_id, text=caption, parse_mode="HTML")
        if markup and markup.get("inline_keyboard"):
            p["reply_markup"] = markup
        tg("sendMessage", **p)


def go(chat_id, mid, key, caption, markup=None):
    if mid:
        show(chat_id, mid, key, caption, markup)
    else:
        send_screen(chat_id, key, caption, markup)


# ============================ ТЕКСТЫ ============================
TUTORIAL = (
    "🔌 <b>Подключение daruv</b>\n"
    "────────────────\n"
    "Привет, я <b>daruv</b>! Подключи меня к своему аккаунту — и я начну работать "
    "прямо в твоих личных чатах.\n\n"
    "<b>Как подключить:</b>\n"
    "▪️ Шаг 1: скопируй тег бота — <code>@daruv_bot</code>\n"
    "▪️ Шаг 2: действуй по фото выше\n"
    "  ▫️ включи «Отвечать на сообщения»\n"
    "  ▫️ включи «Удаление сообщений» (нужно для .mute)\n"
    "  ▫️ в «Чатах» выбери «Все личные чаты»\n"
    "  ▫️ нажми «Готово»\n\n"
    "Как только подключишь — я сам напишу тебе и открою меню!"
)

MAIN_TEXT = "✅ <b>Вы успешно подключили daruv</b>\n\nВыбери раздел ниже 👇"

HELP_TEXT = (
    "📖 <b>Что такое daruv</b>\n\n"
    "daruv — помощник для твоих личных чатов в Telegram. Работает через Telegram Business: "
    "пишешь команду в любом личном чате, а бот делает всё от твоего имени.\n\n"
    "<b>Как пользоваться</b>\n"
    "1️⃣ Подключи бота: Настройки → Telegram Business → Чат-боты → <code>@daruv_bot</code>\n"
    "2️⃣ Выдай права на ответы и удаление сообщений\n"
    "3️⃣ Открой личный чат и напиши команду, например <code>.mute</code>\n"
    "4️⃣ Команда исчезнет, а бот выполнит действие\n\n"
    "Все команды — в разделе «Список команд»."
)

PRANK_FOOTER = f"🃏 Пранк! Хочешь так же — заходи в @{BOT_USERNAME}"

CMDS = [
    ("🔇", ".mute",
     "Автоматически удаляет <b>все сообщения собеседника</b> в этом чате.\n\n"
     "<blockquote>Напиши <code>.mute</code> в личном чате — бот удалит команду и включит режим тишины.</blockquote>\n\n"
     "🟢 Кнопку «Убрать» видишь только ты.\n"
     f"⭐ Собеседник может выкупить размут за {UNMUTE_STARS} звёзд — мут снимется, "
     "а мутить его снова нельзя 1 час."),
    ("🛡", ".antimute",
     "Обход чужого мута: если другой бот удаляет <b>твои</b> сообщения, daruv отправит их заново.\n\n"
     "<blockquote>Напиши <code>.antimute</code> в нужном чате, чтобы включить. Повторно — выключить.</blockquote>\n\n"
     "⚠️ Пока режим включён, сообщения, которые ты удалил сам, тоже вернутся."),
    ("💬", ".spam",
     "Отправляет один и тот же текст много раз подряд.\n\n"
     "<blockquote><code>.spam хаха 10</code> — пришлёт «хаха» 10 раз.\nМаксимум — 100 сообщений.</blockquote>"),
    ("🔥", ".snos",
     "Визуальный «снос» — страшная анимация: ищу способы сноса, нашёл, оформляю жалобу… "
     "и в конце «Снос успешно активирован» с юзернеймом и ID собеседника.\n\n"
     "<blockquote>Напиши <code>.snos</code> в чате с человеком.</blockquote>\n\n"
     "🃏 Это пранк — в конце честно сказано."),
    ("🕵️", ".dox",
     "Пранк-досье: анимация «ищу информацию», юзернейм человека и длинный итог — "
     "место жительства: не знаю, имя: не знаю, машина: не знаю…\n\n"
     "<blockquote>Напиши <code>.dox</code> в чате с человеком.</blockquote>"),
    ("😈", ".troll",
     "Быстро шлёт серию шуточных «страшилок»: «Жди снос», «Я знаю твоё место жительства» и т.п.\n\n"
     f"<blockquote><code>.troll</code> — запустить\n<code>.trollstop</code> — остановить\n"
     f"Сам остановится после {TROLL_MAX} сообщений.</blockquote>"),
]


def cmd_page_text(i):
    icon, name, body = CMDS[i]
    return (f"{icon} <b>{name}</b>\n<i>Команда {i + 1} из {len(CMDS)}</i>\n"
            f"━━━━━━━━━━━━━━\n{body}")


def cmd_page_kb(i):
    n = len(CMDS)
    return kb(
        [btn("◀️", f"m:cmds:{(i - 1) % n}"), btn(f"{i + 1}/{n}", "noop"), btn("▶️", f"m:cmds:{(i + 1) % n}")],
        [btn("⬅️ В меню", "m:menu")],
    )


def main_kb(uid):
    rows = [
        [btn("📋 Список команд", "m:cmds:0"), btn("📖 Инструкция", "m:help")],
        [btn("⚙️ Настройки", "m:set")],
    ]
    if uid == ADMIN_ID:
        rows.append([btn("👑 Админка", "m:admin")])
    return kb(*rows)


TUTORIAL_KB = kb(
    [btn("⚙️ Настройки", url="tg://settings")],
    [btn("✅ Я подключил", "chk")],
)
BACK_MENU = kb([btn("⬅️ В меню", "m:menu")])

DEFAULT_AUTO = "Привет! Сейчас я не на связи. Отвечу, как только смогу 🙌"


def get_auto(uid):
    r = q("SELECT * FROM auto WHERE user_id=?", (uid,), one=True)
    if r:
        return bool(r["enabled"]), r["text"]
    return False, DEFAULT_AUTO


def settings_text(uid):
    en, text = get_auto(uid)
    return (
        "⚙️ <b>Настройки</b>\n━━━━━━━━━━━━━━\n"
        f"🤖 <b>Автоответчик:</b> {'🟢 включён' if en else '⚪️ выключен'}\n\n"
        f"Если ты не отвечаешь {AUTOREPLY_DELAY // 60} мин после сообщения собеседника, "
        "бот сам отправит ему:\n"
        f"<blockquote>{esc(text)}</blockquote>"
    )


def settings_kb(uid):
    en, _ = get_auto(uid)
    return kb(
        [btn("🔴 Выключить" if en else "🟢 Включить автоответчик", "s:tog")],
        [btn("✏️ Изменить текст", "s:txt")],
        [btn("⬅️ В меню", "m:menu")],
    )


# ============================ ПОЛЬЗОВАТЕЛИ / ПОДКЛЮЧЕНИЯ ============================
def upsert_user(u):
    uid = u["id"]
    q("INSERT OR IGNORE INTO users(user_id, joined) VALUES(?,?)", (uid, now()))
    q("UPDATE users SET username=?, first_name=? WHERE user_id=?",
      (u.get("username"), u.get("first_name"), uid))


def save_conn(bc):
    uid = bc["user"]["id"]
    upsert_user(bc["user"])
    rights = bc.get("rights")
    if rights is not None:
        can_reply = int(bool(rights.get("can_reply")))
        can_delete = int(bool(rights.get("can_delete_all_messages")))
    else:
        can_reply = int(bool(bc.get("can_reply", True)))
        can_delete = 1
    enabled = int(bool(bc.get("is_enabled", True)))
    q("INSERT OR REPLACE INTO conns VALUES(?,?,?,?,?)", (uid, bc["id"], enabled, can_reply, can_delete))
    return uid, enabled, can_reply, can_delete


def conn_of(owner):
    return q("SELECT * FROM conns WHERE user_id=? AND enabled=1", (owner,), one=True)


def owner_by_conn(cid):
    r = q("SELECT user_id FROM conns WHERE conn_id=? AND enabled=1", (cid,), one=True)
    if r:
        return r["user_id"]
    bc = tg("getBusinessConnection", business_connection_id=cid)
    if bc:
        uid, enabled, _, _ = save_conn(bc)
        return uid if enabled else None
    return None


def rights_warning():
    return ("⚠️ Выдай боту права «Отвечать на сообщения» и «Удаление всех сообщений» "
            "(Настройки → Telegram Business → Чат-боты), иначе команды не будут работать.")


def on_connection(bc):
    prev = q("SELECT enabled FROM conns WHERE user_id=?", (bc["user"]["id"],), one=True)
    uid, enabled, can_reply, can_delete = save_conn(bc)
    chat = bc.get("user_chat_id", uid)
    if not enabled:
        tg("sendMessage", chat_id=chat, text="❌ Бот отключён от аккаунта. Чтобы подключить снова — /start")
        return
    if prev and prev["enabled"]:
        if not (can_reply and can_delete):
            tg("sendMessage", chat_id=chat, text=rights_warning())
        return
    send_photo(chat, "connected", MAIN_TEXT, main_kb(uid))
    if not (can_reply and can_delete):
        tg("sendMessage", chat_id=chat, text=rights_warning())


# ============================ БИЗНЕС-ХЕЛПЕРЫ ============================
bot_deleted = set()


def biz_delete(cid, mids):
    for m in mids:
        bot_deleted.add((cid, m))
    if len(bot_deleted) > 5000:
        bot_deleted.clear()
    return tg("deleteBusinessMessages", business_connection_id=cid, message_ids=mids)


def biz_send(cid, chat, text, markup=None):
    p = dict(business_connection_id=cid, chat_id=chat, text=text, parse_mode="HTML")
    if markup:
        p["reply_markup"] = markup
    r = tg("sendMessage", **p)
    return r["message_id"] if r else None


def biz_edit(cid, chat, mid, text):
    return tg("editMessageText", business_connection_id=cid, chat_id=chat,
              message_id=mid, text=text, parse_mode="HTML")


def who_html(chat):
    if chat.get("username"):
        return "@" + esc(chat["username"])
    return esc(chat.get("first_name") or "человек")


# ============================ КОМАНДЫ ============================
COMMANDS = {".mute", ".antimute", ".spam", ".snos", ".dox", ".troll", ".trollstop"}
troll_events = {}


def is_muted(owner, chat):
    return q("SELECT 1 FROM mutes WHERE owner_id=? AND chat_id=?", (owner, chat), one=True) is not None


def notice_kb(owner, chat):
    return kb(
        [btn("✅ Убрать", f"um:{owner}:{chat}", style="success")],
        [btn(f"⭐ Размутить ({UNMUTE_STARS}⭐)", url=f"https://t.me/{BOT_USERNAME}?start=um_{owner}_{chat}",
             style="danger")],
    )


def do_mute(cid, owner, chat):
    pr = q("SELECT until FROM protect WHERE owner_id=? AND chat_id=?", (owner, chat["id"]), one=True)
    if pr and pr["until"] > now():
        left = (pr["until"] - now()) // 60 + 1
        tg("sendMessage", chat_id=owner,
           text=f"🛡 Этот собеседник выкупил защиту — замутить его нельзя ещё ~{left} мин.")
        return
    if is_muted(owner, chat["id"]):
        tg("sendMessage", chat_id=owner, text="🔇 Режим тишины в этом чате уже включён.")
        return
    notice = biz_send(cid, chat["id"], "🔇 <b>Режим тишины включён</b>", notice_kb(owner, chat["id"]))
    if not notice:
        tg("sendMessage", chat_id=owner, text="Не получилось включить мут. " + rights_warning())
        return
    q("INSERT OR REPLACE INTO mutes VALUES(?,?,?,?)", (owner, chat["id"], notice, now()))


def do_anti(owner, chat):
    r = q("SELECT 1 FROM anti WHERE owner_id=? AND chat_id=?", (owner, chat["id"]), one=True)
    w = who_html(chat)
    if r:
        q("DELETE FROM anti WHERE owner_id=? AND chat_id=?", (owner, chat["id"]))
        q("DELETE FROM msgs WHERE owner_id=? AND chat_id=?", (owner, chat["id"]))
        tg("sendMessage", chat_id=owner, parse_mode="HTML", text=f"🛡 Анти-мут для чата с {w} <b>выключен</b>.")
    else:
        q("INSERT OR REPLACE INTO anti VALUES(?,?)", (owner, chat["id"]))
        tg("sendMessage", chat_id=owner, parse_mode="HTML",
           text=f"🛡 Анти-мут для чата с {w} <b>включён</b>.\nЕсли твои сообщения удалят — я отправлю их заново.")


def do_spam(cid, chat, arg):
    txt, n = arg, 10
    parts = arg.rsplit(None, 1)
    if arg.isdigit():
        txt, n = "хаха", int(arg)
    elif len(parts) == 2 and parts[1].isdigit():
        txt, n = parts[0], int(parts[1])
    n = max(1, min(100, n))
    txt = txt.strip() or "хаха"
    fails = 0
    for _ in range(n):
        if biz_send(cid, chat["id"], esc(txt)):
            fails = 0
        else:
            fails += 1
            if fails >= 3:
                break
        time.sleep(0.12)


def animate(cid, chat, frames, final, delay=1.1):
    mid = biz_send(cid, chat["id"], frames[0])
    if not mid:
        return
    for f in frames[1:]:
        time.sleep(delay)
        biz_edit(cid, chat["id"], mid, f)
    time.sleep(delay)
    biz_edit(cid, chat["id"], mid, final)


def bar(p):
    full = p // 10
    return "▰" * full + "▱" * (10 - full) + f" {p}%"


def do_snos(cid, chat):
    w = who_html(chat)
    frames = [
        "🔍 Ищу способы сноса",
        "🔍 Ищу способы сноса.",
        "🔍 Ищу способы сноса..",
        "🔍 Ищу способы сноса...",
        "✅ Способ найден",
        f"📂 Собираю данные на {w}",
        f"📂 Собираю данные на {w}.",
        f"📂 Собираю данные на {w}..",
        "⚠️ Нашёл — оформляю жалобу",
        f"📝 Оформляю жалобу\n{bar(20)}",
        f"📝 Оформляю жалобу\n{bar(40)}",
        f"📝 Оформляю жалобу\n{bar(60)}",
        f"📝 Оформляю жалобу\n{bar(80)}",
        f"📝 Оформляю жалобу\n{bar(100)}",
        "📨 Отправляю жалобы в поддержку...",
        "🚨 Подключаю аккаунты...",
        "😈 Осталось совсем чуть-чуть...",
    ]
    final = (f"🔥 <b>Снос успешно активирован</b> на {w}\n"
             f"🆔 ID: <code>{chat['id']}</code>\n\n{PRANK_FOOTER}")
    animate(cid, chat, frames, final)


def do_dox(cid, chat):
    w = who_html(chat)
    frames = [
        f"🔍 Ищу информацию о {w}",
        f"🔍 Ищу информацию о {w}.",
        f"🔍 Ищу информацию о {w}..",
        f"🔍 Ищу информацию о {w}...",
        f"🌐 Проверяю базы данных\n{bar(25)}",
        f"🌐 Проверяю базы данных\n{bar(50)}",
        f"🌐 Проверяю базы данных\n{bar(75)}",
        f"🌐 Проверяю базы данных\n{bar(100)}",
        "🧩 Собираю досье...",
        "📄 Формирую отчёт...",
    ]
    final = (
        f"🕵️ <b>Досье на {w}</b>\n"
        f"🆔 ID: <code>{chat['id']}</code>\n"
        "━━━━━━━━━━━━━━\n"
        "📍 Место жительства: не знаю\n"
        "👤 Имя: не знаю\n"
        "📝 ФИО: не знаю\n"
        "🎂 Возраст: не знаю\n"
        "📞 Номер телефона: не знаю\n"
        "🚗 Машина: не знаю\n"
        "🏫 Школа / работа: не знаю\n"
        "💳 Банк: не знаю\n"
        "🐶 Питомец: не знаю\n"
        "🍕 Любимая еда: наверное, пицца\n"
        "━━━━━━━━━━━━━━\n"
        "✅ Поиск завершён. Найдено: ничего 😄\n\n"
        f"{PRANK_FOOTER}"
    )
    animate(cid, chat, frames, final)


TROLL_PHRASES = [
    "Жди снос 😈", "Я знаю твоё место жительства 🏠", "Я уже иду 🚶", "Не отвечаешь? Зря 😏",
    "Снос уже в пути 📨", "Я вижу тебя 👀", "Готовься 😈", "Время пошло ⏳",
    "Ты следующий 🎯", "Шучу... или нет? 🤡",
]


def do_troll(cid, chat, owner):
    key = (owner, chat["id"])
    if key in troll_events:
        return
    ev = threading.Event()
    troll_events[key] = ev
    try:
        for _ in range(TROLL_MAX):
            if ev.is_set():
                break
            biz_send(cid, chat["id"], random.choice(TROLL_PHRASES))
            ev.wait(0.4)
    finally:
        troll_events.pop(key, None)


def run_command(cid, owner, m, cmd, arg):
    chat = m["chat"]
    biz_delete(cid, [m["message_id"]])
    if cmd == ".mute":
        do_mute(cid, owner, chat)
    elif cmd == ".antimute":
        do_anti(owner, chat)
    elif cmd == ".spam":
        do_spam(cid, chat, arg)
    elif cmd == ".snos":
        do_snos(cid, chat)
    elif cmd == ".dox":
        do_dox(cid, chat)
    elif cmd == ".troll":
        do_troll(cid, chat, owner)
    elif cmd == ".trollstop":
        ev = troll_events.get((owner, chat["id"]))
        if ev:
            ev.set()


# ============================ БИЗНЕС-СООБЩЕНИЯ ============================
owner_active = {}
pending = {}  # (owner, chat) -> (ts, cid)


def on_business_message(m, edited=False):
    cid = m.get("business_connection_id")
    owner = owner_by_conn(cid)
    if not owner:
        return
    chat = m["chat"]
    sender = m.get("from", {}).get("id")
    text = m.get("text") or m.get("caption") or ""

    if sender == owner:
        owner_active[owner] = now()
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        if not edited and cmd in COMMANDS and m.get("text"):
            c = conn_of(owner)
            if c and not c["can_reply"]:
                tg("sendMessage", chat_id=owner, text=rights_warning())
                return
            arg = parts[1] if len(parts) > 1 else ""
            threading.Thread(target=run_command, args=(cid, owner, m, cmd, arg), daemon=True).start()
            return
        if text and q("SELECT 1 FROM anti WHERE owner_id=? AND chat_id=?", (owner, chat["id"]), one=True):
            q("INSERT OR REPLACE INTO msgs VALUES(?,?,?,?,?,?)",
              (owner, chat["id"], m["message_id"], text, 0, now()))
        return

    # сообщение собеседника
    if is_muted(owner, chat["id"]):
        biz_delete(cid, [m["message_id"]])
        return
    if not edited and not m.get("from", {}).get("is_bot"):
        en, _ = get_auto(owner)
        if en and (owner, chat["id"]) not in pending:
            pending[(owner, chat["id"])] = (now(), cid)


def on_deleted(d):
    cid = d.get("business_connection_id")
    chat = d["chat"]["id"]
    owner = owner_by_conn(cid)
    if not owner:
        return
    for mid in d.get("message_ids", []):
        if (cid, mid) in bot_deleted:
            bot_deleted.discard((cid, mid))
            continue
        row = q("SELECT * FROM msgs WHERE owner_id=? AND chat_id=? AND mid=?", (owner, chat, mid), one=True)
        if not row or row["tries"] >= 3:
            continue
        if not q("SELECT 1 FROM anti WHERE owner_id=? AND chat_id=?", (owner, chat), one=True):
            continue
        new = biz_send(cid, chat, esc(row["text"]))
        if new:
            q("INSERT OR REPLACE INTO msgs VALUES(?,?,?,?,?,?)",
              (owner, chat, new, row["text"], row["tries"] + 1, now()))


def process_pending():
    t = now()
    for key, (ts, cid) in list(pending.items()):
        if t - ts < AUTOREPLY_DELAY:
            continue
        pending.pop(key, None)
        owner, chat = key
        if owner_active.get(owner, 0) > ts:
            continue
        en, text = get_auto(owner)
        if not en:
            continue
        sent = q("SELECT ts FROM auto_sent WHERE owner_id=? AND chat_id=?", (owner, chat), one=True)
        if sent and t - sent["ts"] < AUTOREPLY_COOLDOWN:
            continue
        if biz_send(cid, chat, esc(text)):
            q("INSERT OR REPLACE INTO auto_sent VALUES(?,?,?)", (owner, chat, t))


# ============================ ОПЛАТА ЗВЁЗДАМИ ============================
def start_unmute(m, payload):
    uid = m["from"]["id"]
    chat = m["chat"]["id"]
    try:
        _, o, c = payload.split("_")
        owner, target = int(o), int(c)
    except Exception:
        tg("sendMessage", chat_id=chat, text="Ссылка устарела.")
        return
    if uid != target:
        tg("sendMessage", chat_id=chat, text="Эта кнопка предназначена другому человеку.")
        return
    if not is_muted(owner, target):
        tg("sendMessage", chat_id=chat, text="Тебя сейчас никто не мутит ✅")
        return
    tg("sendInvoice", chat_id=chat, title="Размут на 1 час",
       description="Снимет мут и на 1 час защитит тебя от повторного мута.",
       payload=f"um:{owner}:{target}", currency="XTR",
       prices=[{"label": "Размут", "amount": UNMUTE_STARS}])


def on_precheckout(pq):
    ok = True
    err = None
    try:
        _, o, c = pq["invoice_payload"].split(":")
        if not is_muted(int(o), int(c)):
            ok, err = False, "Ты уже не в муте."
    except Exception:
        ok, err = False, "Ошибка платежа."
    p = dict(pre_checkout_query_id=pq["id"], ok=ok)
    if err:
        p["error_message"] = err
    tg("answerPreCheckoutQuery", **p)


def on_paid(m):
    pay = m["successful_payment"]
    try:
        _, o, c = pay["invoice_payload"].split(":")
        owner, target = int(o), int(c)
    except Exception:
        return
    row = q("SELECT notice_mid FROM mutes WHERE owner_id=? AND chat_id=?", (owner, target), one=True)
    q("DELETE FROM mutes WHERE owner_id=? AND chat_id=?", (owner, target))
    q("INSERT OR REPLACE INTO protect VALUES(?,?,?)", (owner, target, now() + PROTECT_SECONDS))
    cn = conn_of(owner)
    if row and row["notice_mid"] and cn:
        biz_delete(cn["conn_id"], [row["notice_mid"]])
    tg("sendMessage", chat_id=target,
       text="✅ Размут куплен! Мут снят, и ближайший час тебя нельзя замутить.")
    u = q("SELECT username, first_name FROM users WHERE user_id=?", (target,), one=True)
    name = ("@" + u["username"]) if u and u["username"] else (u["first_name"] if u and u["first_name"] else str(target))
    tg("sendMessage", chat_id=owner, parse_mode="HTML",
       text=f"🔔 {esc(name)} выкупил размут за {UNMUTE_STARS}⭐. Мут снят, защита на 1 час.")


# ============================ АДМИНКА ============================
state = {}        # uid -> ("photo", key) | ("ar_text",) | ("bc",)
bc_pending = {}   # uid -> (chat, message_id)


def admin_kb():
    return kb(
        [btn("📊 Статистика", "a:stats"), btn("👥 Подключённые", "a:list")],
        [btn("📢 Рассылка", "a:bc"), btn("🖼 Фото", "a:photos")],
        [btn("⬅️ В меню", "m:menu")],
    )


def admin_panel(chat, mid=None):
    go(chat, mid, None, "👑 <b>Админка daruv</b>\n\nВыбери действие:", admin_kb())


def stats_text():
    c = lambda sql, a=(): q(sql, a, one=True)[0]
    return (
        "📊 <b>Статистика</b>\n━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: <b>{c('SELECT COUNT(*) FROM users')}</b>\n"
        f"🆕 За 24 часа: <b>{c('SELECT COUNT(*) FROM users WHERE joined>?', (now() - 86400,))}</b>\n"
        f"🔌 Подключено: <b>{c('SELECT COUNT(*) FROM conns WHERE enabled=1')}</b>\n"
        f"🔇 Активных мутов: <b>{c('SELECT COUNT(*) FROM mutes')}</b>\n"
        f"🛡 Анти-мут чатов: <b>{c('SELECT COUNT(*) FROM anti')}</b>\n"
        f"🤖 Автоответчик включён: <b>{c('SELECT COUNT(*) FROM auto WHERE enabled=1')}</b>"
    )


def list_text():
    rows = q("SELECT c.user_id, u.username, u.first_name FROM conns c "
             "LEFT JOIN users u ON u.user_id=c.user_id WHERE c.enabled=1 LIMIT 50")
    if not rows:
        return "👥 Пока никто не подключён."
    lines = []
    for r in rows:
        name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "—")
        lines.append(f"• {esc(name)} — <code>{r['user_id']}</code>")
    return "👥 <b>Подключённые</b> (до 50)\n━━━━━━━━━━━━━━\n" + "\n".join(lines)


def photos_kb():
    rows = []
    for k, name in SLOTS.items():
        r = q("SELECT sig FROM photos WHERE key=?", (k,), one=True)
        mark = "✅ " if r and r["sig"] == "admin" else ""
        rows.append([btn(mark + name, f"a:photo:{k}")])
    rows.append([btn("⬅️ Назад", "m:admin")])
    return kb(*rows)


def broadcast(admin_chat, src_chat, src_mid):
    users = q("SELECT user_id FROM users")
    ok = fail = 0
    for u in users:
        r = tg("copyMessage", chat_id=u["user_id"], from_chat_id=src_chat, message_id=src_mid)
        if r:
            ok += 1
        else:
            fail += 1
        time.sleep(0.06)
    tg("sendMessage", chat_id=admin_chat, parse_mode="HTML",
       text=f"📢 Рассылка завершена\n✅ Доставлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b>")


# ============================ КНОПКИ ============================
def on_callback(cq):
    uid = cq["from"]["id"]
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    answered = [False]

    def ack(text=None, alert=False):
        answered[0] = True
        p = dict(callback_query_id=cq["id"])
        if text:
            p.update(text=text, show_alert=alert)
        tg("answerCallbackQuery", **p)

    # --- мут: кнопка в бизнес-чате (нажать может и собеседник) ---
    if data.startswith("um:"):
        try:
            _, o, c = data.split(":")
            owner, target = int(o), int(c)
        except Exception:
            ack()
            return
        if uid != owner:
            ack("Убрать режим тишины может только владелец", True)
            return
        row = q("SELECT notice_mid FROM mutes WHERE owner_id=? AND chat_id=?", (owner, target), one=True)
        q("DELETE FROM mutes WHERE owner_id=? AND chat_id=?", (owner, target))
        cn = conn_of(owner)
        if row and row["notice_mid"] and cn:
            biz_delete(cn["conn_id"], [row["notice_mid"]])
        ack("🔊 Режим тишины снят")
        return

    state.pop(uid, None)
    if not chat or not mid:
        ack()
        return

    if data == "noop":
        pass
    elif data == "chk":
        if conn_of(uid):
            go(chat, mid, "connected", MAIN_TEXT, main_kb(uid))
        else:
            ack("Пока не вижу подключения. Настройки → Telegram Business → Чат-боты → @daruv_bot", True)
    elif data == "m:menu":
        go(chat, mid, "connected", MAIN_TEXT, main_kb(uid))
    elif data.startswith("m:cmds:"):
        i = int(data.split(":")[2]) % len(CMDS)
        go(chat, mid, "commands", cmd_page_text(i), cmd_page_kb(i))
    elif data == "m:help":
        go(chat, mid, "help", HELP_TEXT, BACK_MENU)
    elif data == "m:set" or data == "s:cancel":
        go(chat, mid, "commands", settings_text(uid), settings_kb(uid))
    elif data == "s:tog":
        en, text = get_auto(uid)
        q("INSERT OR REPLACE INTO auto VALUES(?,?,?)", (uid, 0 if en else 1, text))
        go(chat, mid, "commands", settings_text(uid), settings_kb(uid))
    elif data == "s:txt":
        state[uid] = ("ar_text",)
        go(chat, mid, "commands",
           "✏️ <b>Пришли новый текст автоответчика</b> одним сообщением (до 500 символов).",
           kb([btn("Отмена", "s:cancel")]))
    elif data.startswith("m:admin") or data.startswith("a:"):
        if uid != ADMIN_ID:
            ack()
            return
        if data == "m:admin":
            admin_panel(chat, mid)
        elif data == "a:stats":
            go(chat, mid, None, stats_text(), kb([btn("⬅️ Назад", "m:admin")]))
        elif data == "a:list":
            go(chat, mid, None, list_text(), kb([btn("⬅️ Назад", "m:admin")]))
        elif data == "a:photos":
            go(chat, mid, None, "🖼 <b>Фото бота</b>\n\nВыбери, какое фото заменить (✅ — уже заменено тобой):",
               photos_kb())
        elif data.startswith("a:photo:"):
            key = data.split(":")[2]
            if key in SLOTS:
                state[uid] = ("photo", key)
                go(chat, mid, None, f"📸 Пришли фото для: <b>{SLOTS[key]}</b>\n\n"
                                    "Отправь как обычное фото (не файлом).",
                   kb([btn("Отмена", "a:photos")]))
        elif data == "a:bc":
            state[uid] = ("bc",)
            go(chat, mid, None, "📢 <b>Рассылка</b>\n\nПришли сообщение (текст, фото с подписью — что угодно). "
                                "Я покажу, как оно выглядит, и спрошу подтверждение.",
               kb([btn("Отмена", "m:admin")]))
        elif data == "a:bc_go":
            p = bc_pending.pop(uid, None)
            if p:
                go(chat, mid, None, "📢 Рассылка запущена…", kb([btn("⬅️ Назад", "m:admin")]))
                threading.Thread(target=broadcast, args=(chat, p[0], p[1]), daemon=True).start()
        elif data == "a:bc_no":
            bc_pending.pop(uid, None)
            admin_panel(chat, mid)

    if not answered[0]:
        ack()


# ============================ ЛИЧНЫЕ СООБЩЕНИЯ ============================
def on_start(m, text):
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    parts = text.split(None, 1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload.startswith("um_"):
        start_unmute(m, payload)
        return
    if conn_of(uid):
        send_photo(chat, "connected", MAIN_TEXT, main_kb(uid))
    else:
        send_photo(chat, "connect", TUTORIAL, TUTORIAL_KB)


def on_message(m):
    if m["chat"].get("type") != "private":
        return
    chat = m["chat"]["id"]
    u = m.get("from") or {}
    uid = u.get("id")
    if not uid:
        return
    upsert_user(u)

    if m.get("successful_payment"):
        on_paid(m)
        return

    text = m.get("text") or ""
    if text.startswith("/start"):
        state.pop(uid, None)
        on_start(m, text)
        return
    if uid == ADMIN_ID and text.startswith("/admin"):
        state.pop(uid, None)
        admin_panel(chat)
        return
    if uid == ADMIN_ID and text.startswith("/stats"):
        tg("sendMessage", chat_id=chat, parse_mode="HTML", text=stats_text())
        return

    st = state.get(uid)
    if not st:
        return
    kind = st[0]

    if kind == "photo" and uid == ADMIN_ID:
        if not m.get("photo"):
            tg("sendMessage", chat_id=chat, text="Пришли именно фото (не файлом).")
            return
        key = st[1]
        fid = m["photo"][-1]["file_id"]
        q("INSERT OR REPLACE INTO photos VALUES(?,?,?)", (key, fid, "admin"))
        state.pop(uid, None)
        send_photo(chat, key, f"✅ Фото обновлено: <b>{SLOTS[key]}</b>", kb([btn("⬅️ К фото", "a:photos")]))
    elif kind == "ar_text":
        if not text:
            tg("sendMessage", chat_id=chat, text="Пришли текст одним сообщением.")
            return
        en, _ = get_auto(uid)
        q("INSERT OR REPLACE INTO auto VALUES(?,?,?)", (uid, 1, text[:500]))
        state.pop(uid, None)
        send_screen(chat, "commands", "✅ Текст сохранён, автоответчик включён.\n\n" + settings_text(uid),
                    settings_kb(uid))
    elif kind == "bc" and uid == ADMIN_ID:
        state.pop(uid, None)
        bc_pending[uid] = (chat, m["message_id"])
        tg("copyMessage", chat_id=chat, from_chat_id=chat, message_id=m["message_id"])
        n = q("SELECT COUNT(*) FROM users", one=True)[0]
        tg("sendMessage", chat_id=chat, parse_mode="HTML",
           text=f"Отправить это сообщение <b>{n}</b> пользователям?",
           reply_markup=kb([btn("✅ Отправить", "a:bc_go", style="success"), btn("❌ Отмена", "a:bc_no")]))


# ============================ ЗАПУСК ============================
ALLOWED = ["message", "callback_query", "business_connection", "business_message",
           "edited_business_message", "deleted_business_messages", "pre_checkout_query"]


def handle(u):
    if "message" in u:
        on_message(u["message"])
    elif "callback_query" in u:
        on_callback(u["callback_query"])
    elif "business_connection" in u:
        on_connection(u["business_connection"])
    elif "business_message" in u:
        on_business_message(u["business_message"])
    elif "edited_business_message" in u:
        on_business_message(u["edited_business_message"], edited=True)
    elif "deleted_business_messages" in u:
        on_deleted(u["deleted_business_messages"])
    elif "pre_checkout_query" in u:
        on_precheckout(u["pre_checkout_query"])


def main():
    init_db()
    me = tg("getMe")
    print("getMe:", me)
    if not me:
        print("❌ Токен неверный или хостинг не пускает на api.telegram.org")
        return
    tg("deleteWebhook")
    print("Бот запущен: @" + me["username"])
    offset = 0
    last_clean = 0
    while True:
        try:
            r = requests.post(API + "getUpdates", json={
                "offset": offset, "timeout": 25, "allowed_updates": ALLOWED}, timeout=40).json()
        except Exception as e:
            print("getUpdates error:", e)
            time.sleep(3)
            continue
        if not r.get("ok"):
            print("getUpdates ошибка:", r)
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            print("update:", [k for k in u if k != "update_id"])
            try:
                handle(u)
            except Exception:
                traceback.print_exc()
        try:
            process_pending()
            if now() - last_clean > 600:
                last_clean = now()
                q("DELETE FROM msgs WHERE ts<?", (now() - 86400,))
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
