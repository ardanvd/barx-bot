import os
import requests
import datetime as dt

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = "@barxexchange"

def tg_send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"}
    return requests.post(url, json=payload, timeout=30).json()

if __name__ == "__main__":
    msg = f"🚀 <b>BARX BOT TEST</b>\n\nTarget Price: 190,500\nTime: {dt.datetime.now()}\n\n✅ TEST MARKER"
    print(f"Sending message: {msg}")
    resp = tg_send_message(msg)
    print(f"Response: {resp}")
