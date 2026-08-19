import os
import re
import json
import time
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

# Sources
SOURCES = [
    {"id": "dollar_sulaymaniyah", "name": "Sulaymaniyah"},
    {"id": "pi_jt", "name": "Tehran"}
]

USD_MARKUP = 1500  # Updated to 1500 higher as requested
USD_SULAYMANIYAH_SPREAD = 2000
EUR_SPREAD = 2000
TRY_SPREAD = 100

WORKING_HOURS_START = 11
WORKING_HOURS_END = 21

# Monitoring Config
MONITOR_DURATION_MINS = 30
CHECK_INTERVAL_SECS = 120

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
    "last_rates": {"eur_usd": 1.15, "usd_try": 32.5}
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
        return [m.get_text() for m in msgs[-15:]]
    except: return []

def extract_price(posts, min_v, max_v, source_type="Sulaymaniyah"):
    sell_price = None

    for txt in reversed(posts):
        txt_norm = txt.translate(PERSIAN_DIGITS)
        matches = [int(m.group(0).replace(",", "")) for m in NUM_RE.finditer(txt_norm)]
        valid_matches = [v for v in matches if min_v <= v <= max_v]

        if source_type == "Sulaymaniyah":
            exclude_keywords = ["خــرید", "پسفردایی", "چاورما", "کورتەی", "دەستپێکردن", "بەرزترین", "نزمترین", "کۆتایی", "مشهد", "هرات", "کف"]
            has_suly = "سلێمانی" in txt
            has_sell = "فروش" in txt
            has_excluded = any(ex in txt for ex in exclude_keywords)
            
            if valid_matches and has_suly and has_sell and not has_excluded:
                if not sell_price:
                    sell_price = valid_matches[0]
                    log.info(f"Matched Sulaymaniyah price: {sell_price} from text: {txt[:50]}...")
                    break
        elif source_type == "Tehran":
            if valid_matches and "تهران" in txt and "فروش" in txt:
                if not sell_price:
                    sell_price = valid_matches[0]
                    break

    return {"sell": sell_price}

def get_live_rate(source, target):
    try:
        r = requests.get(f"https://wise.com/rates/live?source={source}&target={target}", headers={"User-Agent": USER_AGENT}, timeout=10)
        val = float(r.json().get("value"))
        if val: return val
    except: pass
    try:
        r = requests.get(f"https://open.er-api.com/v6/latest/{source}", timeout=10)
        val = r.json().get("rates", {}).get(target)
        if val: return float(val)
    except: pass
    return None

def render_post(usd_buy, usd_sell, eur_buy, eur_sell, try_buy, try_sell, usd_try_rate, eur_usd_rate):
    usd_lira = usd_try_rate
    eur_lira = usd_try_rate * eur_usd_rate
    
    return f"""
🚀 <b>Barx Exchange - نرخ لحظه‌ای ارز</b>

🇹🇷 <b>بازار ترکیه (TRY):</b>
🇺🇸 دلار: {usd_lira:.4f} لیر
🇪🇺 یورو: {eur_lira:.4f} لیر

🇮🇷 <b>بازار ایران (تومان):</b>
🇺🇸 <b>دلار آمریکا:</b>
📥 خرید: {usd_buy:,}
📤 فروش: {usd_sell:,}

🇪🇺 <b>یورو:</b>
📥 خرید: {eur_buy:,}
📤 فروش: {eur_sell:,}

🇹🇷 <b>حواله لیر ترکیه:</b>
📥 خرید: {try_buy:,}
📤 فروش: {try_sell:,}

------------------------
📥 ثبت سفارش و مشاوره آنلاین:
🆔 {ORDER_CONTACT}

✨ {CHANNEL}
"""

def run_cycle():
    now_t = now_tehran()
    if not (WORKING_HOURS_START <= now_t.hour < WORKING_HOURS_END): return "idle"
    
    state = load_state()
    last_rates = state.get("last_rates", {"eur_usd": 1.15, "usd_try": 32.5})
    
    usd_prices = {"sell": None}
    # Priority 1: Sulaymaniyah (Primary source rule)
    suly_posts = fetch_channel_posts("dollar_sulaymaniyah")
    suly_extracted = extract_price(suly_posts, 150000, 250000, source_type="Sulaymaniyah")
    if suly_extracted["sell"]:
        usd_prices["sell"] = suly_extracted["sell"]
        log.info(f"Using Sulaymaniyah price: {usd_prices['sell']}")
    else:
        # Priority 2: Tehran (Fallback)
        tehran_posts = fetch_channel_posts("pi_jt")
        tehran_extracted = extract_price(tehran_posts, 150000, 250000, source_type="Tehran")
        if tehran_extracted["sell"]:
            usd_prices["sell"] = tehran_extracted["sell"]
            log.info(f"Sulaymaniyah not found. Using Tehran price: {usd_prices['sell']}")

    if not usd_prices["sell"]: return "no_price"

    usd_mid = usd_prices["sell"]

    usd_sell = usd_mid + USD_MARKUP
    usd_buy = usd_sell - USD_SULAYMANIYAH_SPREAD
    
    eur_usd_rate = get_live_rate("EUR", "USD") or last_rates.get("eur_usd", 1.15)
    last_rates["eur_usd"] = eur_usd_rate
    eur_sell = int(round((usd_sell * eur_usd_rate) / 100) * 100)
    eur_buy = eur_sell - EUR_SPREAD
    
    usd_try_rate = get_live_rate("USD", "TRY") or last_rates.get("usd_try", 32.5)
    last_rates["usd_try"] = usd_try_rate
    try_mid = usd_sell / usd_try_rate
    try_sell = int(round(try_mid / 10) * 10)
    try_buy = try_sell - TRY_SPREAD
    
    new_keys = {"usd_buy": usd_buy, "usd_sell": usd_sell, "eur_buy": eur_buy, "eur_sell": eur_sell, "try_buy": try_buy, "try_sell": try_sell}
    
    changed = False
    last_keys = state.get("last_keys", {})
    for k in new_keys:
        if new_keys[k] != last_keys.get(k):
            changed = True; break
            
    if changed:
        msg = render_post(usd_buy, usd_sell, eur_buy, eur_sell, try_buy, try_sell, usd_try_rate, eur_usd_rate)
        target_channel_posts = fetch_channel_posts(CHANNEL_ID.replace("@", ""))
        if target_channel_posts and msg in target_channel_posts:
            log.info("Skipping post: Message is identical to one of the last messages in the target channel.")
            return "skipped_duplicate"

        resp = tg_send_message(msg)
        if resp.get("ok"):
            state["last_keys"] = new_keys
            state["last_rates"] = last_rates
            state["last_post_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_state(state)
            return "posted"
    return "skipped"

if __name__ == "__main__":
    start_time = time.time()
    end_time = start_time + (MONITOR_DURATION_MINS * 60)
    
    print(f"Starting monitor for {MONITOR_DURATION_MINS} minutes...")
    while time.time() < end_time:
        try:
            res = run_cycle()
            print(f"Cycle result: {res}")
        except Exception as e:
            print(f"Error: {e}")
        
        time.sleep(CHECK_INTERVAL_SECS)
