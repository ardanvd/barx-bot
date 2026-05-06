#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BARX Live Monitor - FIXED VERSION
-----------------
Policy (enforced):
- Working hours: 09:00 -> 00:00 Tehran time (Asia/Tehran). Outside hours => idle.
- At 00:00 (once per day), publish the "end of trading" message.
- USD: primary @dollar_sulaymaniyah (Sulaymaniyah market price), fallback @pi_jt (Tehran forward price).
- EUR: primary @pi_jt (Tehran forward price), fallback @navasanchannel, fallback @irancurrency.
- TRY lira rate: fetched from a public FX endpoint (TRY/USD -> derive).
- Smart posting: publish if any tracked key changes OR silence >= SILENCE_LIMIT_MIN.
- Buy/Sell spread: 1,000 Toman for USD & EUR; 100 Toman for TRY (buy lower).
- STRICT duplicate guard: if prices unchanged AND silence < SILENCE_LIMIT_MIN => SKIP (no repeat posts).
- If no fresh price available from any source => SKIP (never post stale/fallback prices).
- last_post_utc persisted in state.
- Order contact @barx_exchangee; channel @barxexchange.
"""

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
ENV_PATH = HOME / ".barx_env"

CHANNEL = "@barxexchange"
ORDER_CONTACT = "@barx_exchangee"

# Primary source: dollar_sulaymaniyah (Sulaymaniyah market price)
USD_PRIMARY = "dollar_sulaymaniyah"
USD_SULAYMANIYAH_MARKUP = 1100   # 800 transport + 300 profit
USD_SULAYMANIYAH_SPREAD = 2000   # buy = sell - 2000

# Secondary source: pi_jt (Tehran forward dollar & euro)
USD_EUR_PRIMARY = "pi_jt"

# Fallback USD sources
USD_FALLBACK_A = "dollar_tehran3bze"   # weight 0.75
USD_FALLBACK_B = "tahran_sabza"        # weight 0.25
USD_WEIGHT_A = 0.75
USD_WEIGHT_B = 0.25

# Fallback EUR sources
EUR_FALLBACK_A = "navasanchannel"
EUR_FALLBACK_B = "irancurrency"

SILENCE_LIMIT_MIN = 999999          # effectively disable periodic posting, only post on change
WORKING_HOURS_START = 0             # 00:00 Tehran (24-hour mode)
WORKING_HOURS_END = 24              # 00:00 next day (24-hour mode)

# Spreads (Toman) - buy is lower than sell
USD_SPREAD = 1000
EUR_SPREAD = 1000
TRY_SPREAD = 100

TEHRAN_TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# -------------------- Logging --------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("barx")


# -------------------- Env / Token --------------------

def load_env() -> Dict[str, str]:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL"):
        if k not in env and os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()
BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = ENV.get("TELEGRAM_CHANNEL", CHANNEL)


# -------------------- State --------------------

DEFAULT_STATE: Dict[str, Any] = {
    "last_post_utc": None,
    "last_keys": {
        "usd_buy": None, "usd_sell": None,
        "eur_buy": None, "eur_sell": None,
        "try_buy": None, "try_sell": None,
        "try_usd_lira": None, "try_eur_lira": None,
    },
    "last_source_clocks": {
        USD_PRIMARY: None,
        USD_EUR_PRIMARY: None,
        USD_FALLBACK_A: None,
        USD_FALLBACK_B: None,
        EUR_FALLBACK_A: None,
        EUR_FALLBACK_B: None,
    },
    "end_of_trading_date": None,
    "last_cycle_utc": None,
}


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            merged = json.loads(json.dumps(DEFAULT_STATE))
            for k, v in data.items():
                merged[k] = v
            return merged
        except Exception as e:
            log.warning("state file unreadable (%s), using defaults", e)
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# -------------------- Time helpers --------------------

def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def now_tehran() -> dt.datetime:
    return now_utc().astimezone(TEHRAN_TZ)


def is_within_working_hours(t_tehran: dt.datetime) -> bool:
    h = t_tehran.hour
    return WORKING_HOURS_START <= h < WORKING_HOURS_END


def minutes_since(iso_utc: Optional[str]) -> Optional[float]:
    if not iso_utc:
        return None
    try:
        prev = dt.datetime.fromisoformat(iso_utc)
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=dt.timezone.utc)
        return (now_utc() - prev).total_seconds() / 60.0
    except Exception:
        return None


# -------------------- Telegram API --------------------

TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send_message(text: str, chat: str = CHANNEL_ID) -> Dict[str, Any]:
    url = f"{TG_BASE}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status": r.status_code, "raw": r.text[:500]}


# -------------------- Source channel scraping --------------------

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬", "0123456789..")
NUM_RE = re.compile(r"[\d,]+")


def _normalize_int(s: str) -> Optional[int]:
    if s is None:
        return None
    s = s.translate(PERSIAN_DIGITS).replace(",", "").strip()
    try:
        return int(float(s))
    except Exception:
        return None


def fetch_channel_page(username: str) -> Optional[str]:
    url = f"https://t.me/s/{username}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        if r.status_code == 200:
            return r.text
        log.warning("fetch %s -> HTTP %s", username, r.status_code)
    except Exception as e:
        log.warning("fetch %s failed: %s", username, e)
    return None


def parse_latest_posts(html_text: str, limit: int = 12) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    msgs = soup.select(".tgme_widget_message_wrap")
    out: List[Dict[str, Any]] = []
    for m in msgs[-limit:]:
        text_el = m.select_one(".tgme_widget_message_text")
        time_el = m.select_one("time.time, .tgme_widget_message_date time")
        body = text_el.get_text("\n", strip=True) if text_el else ""
        ts = None
        if time_el and time_el.has_attr("datetime"):
            ts = time_el["datetime"]
        out.append({"text": body, "datetime": ts})
    return out


def latest_post_clock(posts: List[Dict[str, Any]]) -> Optional[str]:
    if not posts:
        return None
    return posts[-1].get("datetime")


def get_source_snapshot(username: str) -> Dict[str, Any]:
    html_text = fetch_channel_page(username)
    if not html_text:
        return {"ok": False, "posts": [], "clock": None}
    posts = parse_latest_posts(html_text)
    return {"ok": True, "posts": posts, "clock": latest_post_clock(posts)}


# -------------------- Extractors --------------------

def _extract_num_from_post(txt: str, min_val: int, max_val: int) -> Optional[int]:
    """Extract the first valid price number from a post text."""
    txt_norm = txt.translate(PERSIAN_DIGITS)
    for m in NUM_RE.finditer(txt_norm):
        raw = m.group(0).replace(",", "")
        if raw.isdigit() and len(raw) >= 5:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
    return None


def extract_usd_sulaymaniyah(posts: List[Dict[str, Any]]) -> Optional[int]:
    """
    Extract USD sell price from @dollar_sulaymaniyah posts.
    """
    latest_sell: Optional[int] = None
    latest_buy: Optional[int] = None

    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        val = _extract_num_from_post(txt, 100_000, 300_000)
        if val is None:
            continue
        
        is_sell = "فروش" in txt or "فروشنده" in txt
        is_buy = "خرید" in txt or "خریدار" in txt
        
        if is_sell and latest_sell is None:
            latest_sell = val
        if is_buy and latest_buy is None:
            latest_buy = val
            
        if latest_sell is not None:
            break
            
    return latest_sell or latest_buy


def extract_pi_jt_usd(posts: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """Extract Tehran forward USD buy/sell from @pi_jt."""
    latest_buy: Optional[int] = None
    latest_sell: Optional[int] = None

    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        is_tehran_usd = (
            ("دلار فردایی تهران" in txt or "دلار فردایی تـهران" in txt
             or "دلار نقدی تهران" in txt or "دلار نـقدی تهران" in txt
             or "دلار نـــقـدی تهران" in txt)
            and "هرات" not in txt
        )
        if not is_tehran_usd:
            continue
        val = _extract_num_from_post(txt, 100_000, 300_000)
        if val is None:
            continue
        is_buy = "خرید" in txt or "خریدار" in txt or "خریـدار" in txt or "خـریدار" in txt
        is_sell = "فروش" in txt or "فروشنده" in txt
        if is_buy and latest_buy is None:
            latest_buy = val
        if is_sell and latest_sell is None:
            latest_sell = val
        if latest_buy is not None and latest_sell is not None:
            break
            
    if latest_buy is not None:
        return latest_buy, latest_buy + USD_SPREAD
    if latest_sell is not None:
        return latest_sell - USD_SPREAD, latest_sell
    return None, None


def extract_pi_jt_eur(posts: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """Extract Tehran forward EUR buy/sell from @pi_jt."""
    latest_buy: Optional[int] = None
    latest_sell: Optional[int] = None

    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        is_tehran_eur = (
            "یورو" in txt
            and ("تهران" in txt or "تـهران" in txt)
            and "دیجیتال" not in txt and "کریپتو" not in txt and "بیت" not in txt
        )
        if not is_tehran_eur:
            continue
        val = _extract_num_from_post(txt, 150_000, 350_000)
        if val is None:
            continue
        is_buy = "خرید" in txt or "خریدار" in txt or "خریـدار" in txt or "خـریدار" in txt
        is_sell = "فروش" in txt or "فروشنده" in txt
        if is_buy and latest_buy is None:
            latest_buy = val
        if is_sell and latest_sell is None:
            latest_sell = val
        if latest_buy is not None and latest_sell is not None:
            break
            
    if latest_buy is not None:
        return latest_buy, latest_buy + EUR_SPREAD
    if latest_sell is not None:
        return latest_sell - EUR_SPREAD, latest_sell
    return None, None


def extract_eur_tomans_fallback(posts: List[Dict[str, Any]]) -> Optional[int]:
    """Fallback for EUR from other channels."""
    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        val = _extract_num_from_post(txt, 150_000, 350_000)
        if val:
            return val
    return None


def try_lira_rates() -> Tuple[Optional[float], Optional[float]]:
    """Fetch TRY/USD and TRY/EUR from Harem Altin."""
    try:
        url = 'https://www.haremaltin.com/dashboard/ajax/doviz'
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/112.0',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://www.haremaltin.com',
            'Referer': 'https://www.haremaltin.com/canli-piyasalar/',
        }
        data = {'dil_kodu': 'tr'}
        r = requests.post(url, headers=headers, data=data, timeout=15)
        if r.status_code == 200:
            res_data = r.json()
            if res_data.get('status') == 'success':
                prices = res_data.get('data', {})
                usd_data = prices.get('USDTL', {})
                eur_data = prices.get('EURTL', {})
                
                usd_lira = float(usd_data.get('satis').replace(',', '.')) if usd_data.get('satis') else None
                eur_lira = float(eur_data.get('satis').replace(',', '.')) if eur_data.get('satis') else None
                
                return usd_lira, eur_lira
    except Exception as e:
        log.warning("Harem Altin fetch failed: %s", e)
        
    # Fallback to public API if Harem fails
    try:
        r = requests.get("https://open.er-api.com/v6/latest/TRY", timeout=10)
        if r.status_code == 200:
            data = r.json()
            rates = data.get("rates", {})
            usd_lira = 1.0 / rates.get("USD") if rates.get("USD") else None
            eur_lira = 1.0 / rates.get("EUR") if rates.get("EUR") else None
            return usd_lira, eur_lira
    except:
        pass
        
    return None, None


def spread(mid: float, spread_val: int, step: int = 100) -> Tuple[int, int]:
    """Calculate buy/sell from mid price with spread."""
    sell = int(math.ceil(mid / step) * step)
    buy = sell - spread_val
    return buy, sell


def keys_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    for k in ("usd_buy", "usd_sell", "eur_buy", "eur_sell", "try_buy", "try_sell"):
        if old.get(k) != new.get(k):
            return True
    return False


def render_post(ub, us, eb, es, tb, ts, ul, el) -> str:
    """Render the Telegram post HTML."""
    return (
        f"🚀 <b>Barx Exchange - نرخ لحظه‌ای ارز</b>\n\n"
        f"🇹🇷 <b>بازار ترکیه (TRY):</b>\n"
        f"🇺🇸 دلار: {ul:.4f} لیر\n"
        f"🇪🇺 یورو: {el:.4f} لیر\n\n"
        f"🇮🇷 <b>بازار ایران (تومان):</b>\n"
        f"🇺🇸 <b>دلار آمریکا:</b>\n"
        f"📥 خرید: {ub:,}\n"
        f"📤 فروش: {us:,}\n\n"
        f"🇪🇺 <b>یورو:</b>\n"
        f"📥 خرید: {eb:,}\n"
        f"📤 فروش: {es:,}\n\n"
        f"🇹🇷 <b>حواله لیر ترکیه:</b>\n"
        f"📥 خرید: {tb:,}\n"
        f"📤 فروش: {ts:,}\n\n"
        f"------------------------\n"
        f"📥 ثبت سفارش و مشاوره آنلاین:\n"
        f"🆔 {ORDER_CONTACT}\n\n"
        f"✨ {CHANNEL}"
    )


def run_cycle():
    t_utc = now_utc()
    t_tehran = now_tehran()
    log.info("Cycle start: Tehran=%s UTC=%s", t_tehran, t_utc)

    if not is_within_working_hours(t_tehran):
        log.info("Outside working hours; skipping")
        return {"action": "idle", "detail": "outside_hours"}

    state = load_state()
    usd_buy_raw, usd_sell_raw = None, None
    eur_buy_raw, eur_sell_raw = None, None
    usd_source, eur_source = "none", "none"

    # -------- USD PRIMARY: Sulaymaniyah --------
    suly_snap = get_source_snapshot(USD_PRIMARY)
    if suly_snap["ok"]:
        suly_mid = extract_usd_sulaymaniyah(suly_snap["posts"])
        if suly_mid:
            usd_sell_raw = suly_mid + USD_SULAYMANIYAH_MARKUP
            usd_buy_raw = usd_sell_raw - USD_SULAYMANIYAH_SPREAD
            usd_source = USD_PRIMARY
            log.info("USD from Sulaymaniyah: mid=%d -> buy=%d sell=%d", suly_mid, usd_buy_raw, usd_sell_raw)

    # -------- USD SECONDARY: pi_jt --------
    if usd_buy_raw is None:
        pi_snap = get_source_snapshot(USD_EUR_PRIMARY)
        if pi_snap["ok"]:
            usd_buy_raw, usd_sell_raw = extract_pi_jt_usd(pi_snap["posts"])
            if usd_buy_raw:
                usd_source = USD_EUR_PRIMARY

    # -------- EUR PRIMARY: pi_jt --------
    

    # -------- EUR FALLBACK --------
    if eur_buy_raw is None:
        eur_fallback_a_snap = get_source_snapshot(EUR_FALLBACK_A)
        eur_mid = extract_eur_tomans_fallback(eur_fallback_a_snap["posts"]) if eur_fallback_a_snap["ok"] else None
        if eur_mid:
            eur_buy_raw, eur_sell_raw = spread(eur_mid, EUR_SPREAD)
            eur_source = EUR_FALLBACK_A

    if usd_buy_raw is None or eur_buy_raw is None:
        log.warning("No fresh price available; skipping")
        return {"action": "skip", "detail": "no_fresh_data"}

    # -------- Lira --------
    usd_lira, eur_lira = try_lira_rates()

    # 🔥 FIX EUR IRR
    if usd_buy_raw and usd_sell_raw and usd_lira and eur_lira:
        ratio = eur_lira / usd_lira

        eur_buy_raw = int(usd_buy_raw * ratio)
        eur_sell_raw = int(usd_sell_raw * ratio)
        eur_buy_raw = int(usd_buy_raw * 1.175)
        eur_sell_raw = int(usd_sell_raw * 1.175)
        last = state.get("last_keys", {})

        effective_usd_lira = usd_lira if (usd_lira and 20 <= usd_lira <= 100) else last.get("try_usd_lira") or 45.0
        display_usd_lira = math.floor(effective_usd_lira)
        try_mid = float(usd_sell_raw) / display_usd_lira
        try_buy, try_sell = spread(try_mid, TRY_SPREAD, step=10)

        new_keys = {
            "usd_buy": usd_buy_raw, "usd_sell": usd_sell_raw,
            "eur_buy": eur_buy_raw, "eur_sell": eur_sell_raw,
            "try_buy": try_buy, "try_sell": try_sell,
            "try_usd_lira": display_usd_lira,
            "try_eur_lira": round(eur_lira, 4) if eur_lira else last.get("try_eur_lira") or 52.0,
        }

        changed = keys_changed(last, new_keys)
        mins_silent = minutes_since(state.get("last_post_utc"))

        if changed: # Only post if price changed
            msg = render_post(
                usd_buy_raw, usd_sell_raw,
                eur_buy_raw, eur_sell_raw,
                try_buy, try_sell,
                new_keys["try_usd_lira"], new_keys["try_eur_lira"],
            )
            resp = tg_send_message(msg)
            if resp.get("ok"):
                state["last_keys"] = new_keys
                state["last_post_utc"] = t_utc.isoformat()
                save_state(state)
                return {"action": "posted", "detail": "change" if changed else "silence"}
            else:
                log.error("TG send failed: %s", resp)
                return {"action": "error", "detail": "tg_fail"}
        
        return {"action": "skip", "detail": "no_change"}
    
    return {"action": "skip", "detail": "no_data_for_lira_fix"}

def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN missing")
        return 2
    try:
        res = run_cycle()
        log.info("Cycle result: %s", res)
        return 0
    except Exception as e:
        log.exception("Fatal: %s", e)
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
