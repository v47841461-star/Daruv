import json
import os
import time

import functools

import requests

print = functools.partial(print, flush=True)

TOKEN = "8986706200:AAHlBA1lyeA_pnwUfk4CNm5l2JYdRXpLQ50"
ADMIN_ID = 8716189150
API = f"https://api.telegram.org/bot{TOKEN}/"
DB_FILE = "data.json"
PHOTO = "connect.jpg"

COMMANDS_TEXT = (
    "✅ Бот подключён!\n\n"
    "Команда:\n"
    ".mute — отправь в чате с человеком, и все его сообщения "
    "будут удаляться. Чтобы снять — нажми «Убрать»."
)


def load():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"conn": None, "muted": {}}


def save():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)


def api(method, **params):
    try:
        r = requests.post(API + method, json=params, timeout=60)
        return r.json()
    except Exception as e:
        print("API error:", e)
        return {}


def send_start(chat_id):
    caption = "Привет, я daruv 👋\nПодключи меня в настройки"
    kb = {"inline_keyboard": [
        [{"text": "⚙️ Настройки", "url": "tg://settings"}],
        [{"text": "✅ Я подключил", "callback_data": "connected"}],
    ]}
    if os.path.exists(PHOTO):
        with open(PHOTO, "rb") as f:
            requests.post(
                API + "sendPhoto",
                data={"chat_id": chat_id, "caption": caption,
                      "reply_markup": json.dumps(kb)},
                files={"photo": f}, timeout=60,
            )
    else:
        api("sendMessage", chat_id=chat_id, text=caption, reply_markup=kb)


def on_connection(bc):
    if bc["user"]["id"] != ADMIN_ID:
        return
    if bc.get("is_enabled", True):
        db["conn"] = bc["id"]
        save()
        text = COMMANDS_TEXT
        rights = bc.get("rights")
        if rights is not None and not rights.get("can_delete_all_messages"):
            text += "\n\n⚠️ Дай боту право удалять все сообщения, иначе .mute не сработает."
        api("sendMessage", chat_id=ADMIN_ID, text=text)
    else:
        db["conn"] = None
        save()
        api("sendMessage", chat_id=ADMIN_ID, text="❌ Бот отключён от аккаунта.")


def delete(conn, chat_id, ids):
    api("deleteBusinessMessages", business_connection_id=conn, message_ids=ids)


def on_business_message(m):
    conn = m.get("business_connection_id")
    chat_id = str(m["chat"]["id"])
    sender = m.get("from", {}).get("id")
    text = (m.get("text") or "").strip().lower()

    if sender == ADMIN_ID:
        if text == ".mute":
            delete(conn, chat_id, [m["message_id"]])
            res = api(
                "sendMessage",
                business_connection_id=conn,
                chat_id=m["chat"]["id"],
                text="🔇 Режим тишины включён",
                reply_markup={"inline_keyboard": [
                    [{"text": "Убрать", "callback_data": "unmute:" + chat_id}]
                ]},
            )
            notice = res.get("result", {}).get("message_id")
            db["muted"][chat_id] = notice
            save()
        return

    if chat_id in db["muted"]:
        delete(conn, chat_id, [m["message_id"]])


def on_callback(cq):
    uid = cq["from"]["id"]
    data = cq.get("data", "")
    if uid != ADMIN_ID:
        api("answerCallbackQuery", callback_query_id=cq["id"])
        return

    if data == "connected":
        if db.get("conn"):
            api("sendMessage", chat_id=ADMIN_ID, text=COMMANDS_TEXT)
            api("answerCallbackQuery", callback_query_id=cq["id"])
        else:
            api("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True,
                text="Бот ещё не подключён. Настройки → Telegram Business → Чат-боты.")

    elif data.startswith("unmute:"):
        chat_id = data.split(":", 1)[1]
        notice = db["muted"].pop(chat_id, None)
        save()
        if notice and db.get("conn"):
            delete(db["conn"], chat_id, [notice])
        api("answerCallbackQuery", callback_query_id=cq["id"], text="Убрано")


def handle(u):
    if "message" in u:
        m = u["message"]
        if m["chat"]["id"] == ADMIN_ID and (m.get("text") or "").startswith("/start"):
            send_start(ADMIN_ID)
    elif "callback_query" in u:
        on_callback(u["callback_query"])
    elif "business_connection" in u:
        on_connection(u["business_connection"])
    elif "business_message" in u:
        on_business_message(u["business_message"])
    elif "edited_business_message" in u:
        on_business_message(u["edited_business_message"])


def main():
    offset = 0
    me = api("getMe")
    print("getMe:", me)
    if not me.get("ok"):
        print("❌ Токен неверный или хостинг не пускает на api.telegram.org")
        return
    print("deleteWebhook:", api("deleteWebhook"))
    print("Бот запущен: @" + me["result"]["username"])
    while True:
        res = api(
            "getUpdates", offset=offset, timeout=50,
            allowed_updates=["message", "callback_query", "business_connection",
                             "business_message", "edited_business_message"],
        )
        if not res.get("ok"):
            print("getUpdates ошибка:", res)
            time.sleep(5)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            print("update:", u)
            try:
                handle(u)
            except Exception as e:
                print("Ошибка:", e)


db = load()
main()
