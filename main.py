import os
import re
import json
import time
import html
import math
import logging
import datetime as dt
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

import requests
from bs4 import BeautifulSoup

# -------------------- Config --------------------
HOME = Path(__file__).parent
STATE_PATH = HOME / "barx_live_state.json"
LOG_PATH = HOME / "barx_live_monitor.log"

CHANNEL = "@barxexchange"
ORDER_CONTACT = "@barx_exchangee"

USD_PRIMARY = "dollar_sulaymaniyah"
USD_SULAYMANIYAH_MARKUP = 500  # User requested: 500 more than Sulaymaniyah
USD_SULAYMANIYAH_SPREAD = 1000 # Standard spread
EUR_SPREAD = 1000
TRY_SPREAD = 100

WORKING_HOURS_START = 8
WORKING_HOURS_END = 24

TEHRAN_TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("barx")

# -------------------- Env / Token --------------------
def load_env():
    return {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHANNEL": os.environ.get("TELEGRAM_CHANNEL", CHANNEL)
    }

ENV = load_env()
BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = ENV.get("TELEGRAM_CHANNEL")

# -------------------- State --------------------
DEFAULT_STATE = {
    "last_post_utc": None,
    "last_keys": {"usd_buy": None, "usd_sell": None, "eur_buy": None, "eur_sell": None, "try_buy": None, "try_sell": None},
}

def load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return DEFAULT_STATE

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# -------------------- Helpers --------------------
def now_tehran(): return dt.datetime.now(tz=dt.timezone.utc).astimezone(TEHRAN_TZ)

def tg_send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "disable_web_page_preview": True, "parse_mode": "HTML"}
    try: return requests.post(url, json=payload, timeout=30).json()
    except: return {"ok": False}

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬", "0123456789..")
NUM_RE = re.compile(r"[\d,]+")

def fetch_channel_posts(username):
    try:
        r = requests.get(f"https://t.me/s/{username}", headers={"User-Agent": USER_AGENT}, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        msgs = soup.select(".tgme_widget_message_text")
        out = []
        for m in msgs[-10:]:
            out.append(m.get_text())
        return out
    except: return []

def extract_price(posts, min_v, max_v):
    for txt in reversed(posts):
        txt_norm = txt.translate(PERSIAN_DIGITS)
        for m in NUM_RE.finditer(txt_norm):
            val = int(m.group(0).replace(",", ""))
            if min_v <= val <= max_v: return val
    return None

def get_lira_usd_rate():
    try:
        r = requests.get("https://wise.com/rates/live?source=USD&target=TRY", headers={"User-Agent": USER_AGENT}, timeout=10)
        return float(r.json().get("value", 32.5))
    except: return 32.5

def render_post(usd_buy, usd_sell, eur_buy, eur_sell, try_buy, try_sell, lira_rate):
    now = now_tehran()
    return f"""
💎 <b>نرخ لحظه‌ای ارز بارکس</b>
🗓 {now.strftime('%Y/%m/%d')} | ⏰ {now.strftime('%H:%M')}

🇺🇸 <b>دلار آمریکا</b>
فروش: {usd_sell:,}
خرید: {usd_buy:,}

🇪🇺 <b>یورو اروپا</b>
فروش: {eur_sell:,}
خرید: {eur_buy:,}

🇹🇷 <b>لیر ترکیه</b>
فروش: {try_sell:,}
خرید: {try_buy:,}

📊 <b>ریت ترکیه:</b> {lira_rate:.2f}

------------------------
📥 ثبت سفارش و مشاوره آنلاین:
🆔 {ORDER_CONTACT}

✅ {CHANNEL}
"""

def run_cycle():
    now_t = now_tehran()
    if not (WORKING_HOURS_START <= now_t.hour < WORKING_HOURS_END): return "idle"
    
    state = load_state()
    
    # USD from Sulaymaniyah
    suly_posts = fetch_channel_posts(USD_PRIMARY)
    suly_mid = extract_price(suly_posts, 100000, 300000)
    if not suly_mid: return "no_price"
    
    # User requested: 500 more than Sulaymaniyah
    usd_sell = suly_mid + USD_SULAYMANIYAH_MARKUP
    usd_buy = usd_sell - USD_SULAYMANIYAH_SPREAD
    
    # EUR (USD * 1.085 approx)
    eur_sell = int(round((usd_sell * 1.085) / 100) * 100)
    eur_buy = eur_sell - EUR_SPREAD
    
    # Lira
    lira_rate = get_lira_usd_rate()
    try_mid = usd_sell / lira_rate
    try_sell = int(round(try_mid / 10) * 10)
    try_buy = try_sell - TRY_SPREAD
    
    new_keys = {"usd_buy": usd_buy, "usd_sell": usd_sell, "eur_buy": eur_buy, "eur_sell": eur_sell, "try_buy": try_buy, "try_sell": try_sell}
    
    # Check if changed
    changed = False
    last_keys = state.get("last_keys", {})
    for k in new_keys:
        if new_keys[k] != last_keys.get(k):
            changed = True; break
            
    if changed:
        msg = render_post(usd_buy, usd_sell, eur_buy, eur_sell, try_buy, try_sell, lira_rate)
        resp = tg_send_message(msg)
        if resp.get("ok"):
            state["last_keys"] = new_keys
            state["last_post_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_state(state)
            return "posted"
    return "skipped"

if __name__ == "__main__":
    start_time = time.time()
    while (time.time() - start_time) < 550:
        try: print(f"Cycle: {run_cycle()}")
        except Exception as e: print(f"Error: {e}")
        time.sleep(60)
