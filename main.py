#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BARX Live Monitor
-----------------
Policy (enforced):
- Working hours: 09:00 -> 00:00 Tehran time (Asia/Tehran). Outside hours => idle.
- At 00:00 (once per day), publish the "end of trading" message.
- USD: primary @pi_jt (Tehran forward price), fallback @dollar_tehran3bze (75%) + @tahran_sabza (25%).
- EUR: primary @pi_jt (Tehran forward price), fallback @navasanchannel, fallback @irancurrency.
- TRY lira rate: fetched from a public FX endpoint (TRY/USD -> derive).
- Smart posting: publish if any tracked key changes OR silence >= SILENCE_LIMIT_MIN.
- Buy/Sell spread: 1,000 Toman for USD & EUR; 100 Toman for TRY (buy lower).
- STRICT duplicate guard: if prices unchanged AND silence < SILENCE_LIMIT_MIN => SKIP (no repeat posts).
- If no fresh price available from any source => SKIP (never post stale/fallback prices).
- last_post_utc persisted in state.
- Order contact @barx_exchangee; channel @barxexchange.

Files:
- /home/ubuntu/barx_live_monitor.py   (this file)
- /home/ubuntu/barx_live_state.json   (state)
- /home/ubuntu/barx_live_monitor.log  (log)
- /home/ubuntu/.barx_env              (contains TELEGRAM_BOT_TOKEN=...)
"""

import os
import re
import json
import time
import html
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

# Primary source: pi_jt (Tehran forward dollar & euro)
USD_EUR_PRIMARY = "pi_jt"

# Fallback USD sources
USD_FALLBACK_A = "dollar_tehran3bze"   # weight 0.75
USD_FALLBACK_B = "tahran_sabza"        # weight 0.25
USD_WEIGHT_A = 0.75
USD_WEIGHT_B = 0.25

# Fallback EUR sources
EUR_FALLBACK_A = "navasanchannel"
EUR_FALLBACK_B = "irancurrency"

SILENCE_LIMIT_MIN = 30              # minutes; post every 30 min if price changed
WORKING_HOURS_START = 9             # 09:00 Tehran
WORKING_HOURS_END = 24              # 00:00 next day (exclusive)

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


# -------------------- pi_jt extractors (PRIMARY) --------------------

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


def extract_pi_jt_usd(posts: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """
    Extract Tehran forward USD buy/sell from @pi_jt.
    Posts are published separately: one post for buy (خرید/خریدار) and one for sell (فروش/فروشنده).
    We scan all recent posts, find the latest buy price and latest sell price independently,
    then enforce the 1000 Toman spread (buy = lower, sell = buy + USD_SPREAD).
    Returns (buy, sell) or (None, None).
    """
    latest_buy: Optional[int] = None
    latest_sell: Optional[int] = None

    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        # Must mention Tehran dollar (فردایی or نقدی), not Herat
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
            log.info("pi_jt USD buy post found: %d from: %s", val, txt[:80])
        if is_sell and latest_sell is None:
            latest_sell = val
            log.info("pi_jt USD sell post found: %d from: %s", val, txt[:80])
        if latest_buy is not None and latest_sell is not None:
            break

    if latest_buy is not None:
        buy = latest_buy
        sell = buy + USD_SPREAD
        log.info("pi_jt USD final: buy=%d sell=%d (spread enforced)", buy, sell)
        return buy, sell
    if latest_sell is not None:
        sell = latest_sell
        buy = sell - USD_SPREAD
        log.info("pi_jt USD final (sell-only): buy=%d sell=%d (spread enforced)", buy, sell)
        return buy, sell
    return None, None


def extract_pi_jt_eur(posts: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """
    Extract Tehran forward EUR buy/sell from @pi_jt.
    Posts are published separately: one post for buy (خرید/خریدار) and one for sell (فروش/فروشنده).
    We scan all recent posts, find the latest buy price and latest sell price independently,
    then enforce the 1000 Toman spread (buy = lower, sell = buy + EUR_SPREAD).
    Returns (buy, sell) or (None, None).
    """
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
            log.info("pi_jt EUR buy post found: %d from: %s", val, txt[:80])
        if is_sell and latest_sell is None:
            latest_sell = val
            log.info("pi_jt EUR sell post found: %d from: %s", val, txt[:80])
        if latest_buy is not None and latest_sell is not None:
            break

    if latest_buy is not None:
        buy = latest_buy
        sell = buy + EUR_SPREAD
        log.info("pi_jt EUR final: buy=%d sell=%d (spread enforced)", buy, sell)
        return buy, sell
    if latest_sell is not None:
        sell = latest_sell
        buy = sell - EUR_SPREAD
        log.info("pi_jt EUR final (sell-only): buy=%d sell=%d (spread enforced)", buy, sell)
        return buy, sell
    return None, None


# -------------------- Fallback USD extractor --------------------

def extract_usd_tomans_fallback(posts: List[Dict[str, Any]]) -> Optional[float]:
    """
    Fallback USD extractor for @dollar_tehran3bze / @tahran_sabza.
    Only extracts if the post explicitly contains دلار (dollar keyword).
    Range: 100k..300k Toman.
    """
    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        # Must explicitly mention dollar
        if "دلار" not in txt and "dollar" not in txt.lower():
            continue
        txt_norm = txt.translate(PERSIAN_DIGITS)
        for m in NUM_RE.finditer(txt_norm):
            raw = m.group(0).replace(",", "")
            if raw.isdigit() and len(raw) >= 5:
                val = int(raw)
                if 100_000 <= val <= 300_000:
                    return float(val)
    return None


# -------------------- Fallback EUR extractor --------------------

def extract_eur_tomans_fallback(posts: List[Dict[str, Any]]) -> Optional[float]:
    """
    Fallback EUR extractor for @navasanchannel / @irancurrency.
    Extracts the EUR sell price from posts that contain both USD and EUR prices.
    The posts typically list USD first then EUR, so we must find the number
    that comes AFTER the یورو keyword, not the first number in the post.
    Returns the sell price so the caller can apply spread.
    """
    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        if "یورو" not in txt and "EUR" not in txt:
            continue
        # Must not be a crypto/digital currency post
        if "دیجیتال" in txt or "کریپتو" in txt or "بیت" in txt:
            continue
        txt_norm = txt.translate(PERSIAN_DIGITS)
        # Strategy 1: explicit "یورو فروش : NUMBER" pattern (navasanchannel)
        m1 = re.search(r"یورو\s*فروش\s*[:\-]?\s*([\d,]{6,9})", txt_norm)
        if m1:
            val = int(m1.group(1).replace(",", ""))
            if 150_000 <= val <= 350_000:
                log.info("EUR fallback (sell pattern): %d from %s", val, txt[:60])
                return float(val)
        # Strategy 2: find یورو keyword, then grab the next 6-9 digit number after it
        eur_pos = txt_norm.find("یورو")
        if eur_pos >= 0:
            after_eur = txt_norm[eur_pos:]
            m2 = re.search(r"([\d,]{6,9})", after_eur)
            if m2:
                val_str = m2.group(1).replace(",", "")
                if val_str.isdigit():
                    val = int(val_str)
                    if 150_000 <= val <= 350_000:
                        log.info("EUR fallback (after-keyword): %d from %s", val, txt[:60])
                        return float(val)
    return None


# -------------------- Lira cross-rate --------------------

def try_lira_rates() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (USD_in_Lira, EUR_in_Lira).
    """
    try:
        r = requests.get(
            "https://api.exchangerate.host/latest",
            params={"base": "USD", "symbols": "TRY,EUR"},
            timeout=20,
        )
        j = r.json()
        rates = j.get("rates", {})
        usd_try = rates.get("TRY")
        usd_eur = rates.get("EUR")
        if usd_try and usd_eur:
            eur_try = usd_try / usd_eur
            return float(usd_try), float(eur_try)
    except Exception as e:
        log.info("exchangerate.host failed: %s", e)
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=20)
        j = r.json()
        rates = j.get("rates", {})
        usd_try = rates.get("TRY")
        usd_eur = rates.get("EUR")
        if usd_try and usd_eur:
            eur_try = usd_try / usd_eur
            return float(usd_try), float(eur_try)
    except Exception as e:
        log.info("open.er-api.com failed: %s", e)
    return None, None


# -------------------- Price computation --------------------

def spread(mid: float, spread_value: int, step: int = 50) -> Tuple[int, int]:
    if spread_value == 100:
        mid_rounded = int(round(mid / 10.0) * 10)
        sell = mid_rounded + 10
        buy = sell - 100
        return buy, sell
    buy = int(round((mid - spread_value / 2) / step) * step)
    sell = buy + spread_value
    return buy, sell


# -------------------- Message rendering --------------------

def fmt_int(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def fmt_decimal(x: Optional[float], digits: int = 4) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def render_post(
    usd_buy: int, usd_sell: int,
    eur_buy: int, eur_sell: int,
    try_buy: int, try_sell: int,
    try_usd_lira: Optional[float], try_eur_lira: Optional[float],
) -> str:
    msg = (
        f"🚀 Barx Exchange - نرخ لحظه‌ای ارز\n"
        f"\n"
        f"🇹🇷 بازار ترکیه (TRY):\n"
        f"🇺🇸 دلار: {fmt_decimal(try_usd_lira)} لیر\n"
        f"🇪🇺 یورو: {fmt_decimal(try_eur_lira)} لیر\n"
        f"\n"
        f"🇮🇷 بازار ایران (تومان):\n"
        f"🇺🇸 دلار آمریکا:\n"
        f"📥 خرید: {fmt_int(usd_buy)}\n"
        f"📤 فروش: {fmt_int(usd_sell)}\n"
        f"\n"
        f"🇪🇺 یورو:\n"
        f"📥 خرید: {fmt_int(eur_buy)}\n"
        f"📤 فروش: {fmt_int(eur_sell)}\n"
        f"\n"
        f"🇹🇷 حواله لیر ترکیه:\n"
        f"📥 خرید: {fmt_int(try_buy)}\n"
        f"📤 فروش: {fmt_int(try_sell)}\n"
        f"\n"
        f"------------------------\n"
        f"📥 ثبت سفارش و مشاوره آنلاین:\n"
        f"🆔 {ORDER_CONTACT}\n"
        f"\n"
        f"✨ {CHANNEL}"
    )
    return msg


def render_end_of_trading() -> str:
    return (
        "🔔 Barx Exchange - پایان معاملات امروز\n\n"
        "معاملات امروز به پایان رسید. از اعتماد و همراهی شما سپاس‌گزاریم.\n"
        "معاملات فردا از ساعت ۹:۰۰ صبح به وقت تهران از سر گرفته می‌شود.\n\n"
        "------------------------\n"
        "📥 ثبت سفارش و مشاوره آنلاین:\n"
        f"🆔 {ORDER_CONTACT}\n\n"
        f"✨ {CHANNEL}"
    )


# -------------------- Decision logic --------------------

def keys_changed(prev: Dict[str, Any], new: Dict[str, Any]) -> bool:
    tracked = ("usd_buy", "usd_sell", "eur_buy", "eur_sell", "try_buy", "try_sell")
    for k in tracked:
        if prev.get(k) != new.get(k):
            return True
    return False


# -------------------- Main cycle --------------------

def run_cycle() -> Dict[str, Any]:
    state = load_state()
    result: Dict[str, Any] = {"action": "none", "detail": ""}

    t_tehran = now_tehran()
    t_utc = now_utc()
    state["last_cycle_utc"] = t_utc.isoformat()

    today_tehran = t_tehran.strftime("%Y-%m-%d")

    # -------- End-of-trading at 00:00 Tehran --------
    if t_tehran.hour == 0 and t_tehran.minute < 5:
        if state.get("end_of_trading_date") != today_tehran:
            log.info("Sending end-of-trading message for %s", today_tehran)
            resp = tg_send_message(render_end_of_trading())
            if resp.get("ok"):
                state["end_of_trading_date"] = today_tehran
                state["last_post_utc"] = t_utc.isoformat()
                save_state(state)
                result.update({"action": "end_of_trading", "detail": today_tehran})
                return result
            else:
                log.error("EOT send failed: %s", resp)
                save_state(state)
                result.update({"action": "error", "detail": f"EOT send failed: {resp}"})
                return result

    # -------- Working-hours gate --------
    if not is_within_working_hours(t_tehran):
        log.info("outside working hours (Tehran %s); sleeping", t_tehran.strftime("%H:%M"))
        save_state(state)
        result.update({"action": "idle", "detail": "outside_working_hours"})
        return result

    # -------- Gather prices from PRIMARY source: pi_jt --------
    pi_snap = get_source_snapshot(USD_EUR_PRIMARY)
    usd_buy_raw: Optional[int] = None
    usd_sell_raw: Optional[int] = None
    eur_buy_raw: Optional[int] = None
    eur_sell_raw: Optional[int] = None
    usd_source = "none"
    eur_source = "none"

    if pi_snap["ok"]:
        usd_buy_raw, usd_sell_raw = extract_pi_jt_usd(pi_snap["posts"])
        eur_buy_raw, eur_sell_raw = extract_pi_jt_eur(pi_snap["posts"])
        if usd_buy_raw:
            usd_source = USD_EUR_PRIMARY
        if eur_buy_raw:
            eur_source = USD_EUR_PRIMARY

    # -------- Fallback USD if pi_jt didn't yield --------
    usd_fallback_a_snap = {"ok": False, "posts": [], "clock": None}
    usd_fallback_b_snap = {"ok": False, "posts": [], "clock": None}
    if usd_buy_raw is None:
        log.info("pi_jt USD not found, trying fallback sources")
        usd_fallback_a_snap = get_source_snapshot(USD_FALLBACK_A)
        usd_fallback_b_snap = get_source_snapshot(USD_FALLBACK_B)
        usd_a = extract_usd_tomans_fallback(usd_fallback_a_snap["posts"]) if usd_fallback_a_snap["ok"] else None
        usd_b = extract_usd_tomans_fallback(usd_fallback_b_snap["posts"]) if usd_fallback_b_snap["ok"] else None
        if usd_a and usd_b:
            usd_mid = USD_WEIGHT_A * usd_a + USD_WEIGHT_B * usd_b
        elif usd_a:
            usd_mid = usd_a
        elif usd_b:
            usd_mid = usd_b
        else:
            usd_mid = None

        if usd_mid:
            usd_buy_raw, usd_sell_raw = spread(usd_mid, USD_SPREAD)
            usd_source = f"{USD_FALLBACK_A}+{USD_FALLBACK_B}"
            log.info("USD fallback computed: mid=%.0f buy=%d sell=%d", usd_mid, usd_buy_raw, usd_sell_raw)

    # -------- Fallback EUR if pi_jt didn't yield --------
    eur_fallback_a_snap = {"ok": False, "posts": [], "clock": None}
    eur_fallback_b_snap = {"ok": False, "posts": [], "clock": None}
    if eur_buy_raw is None:
        log.info("pi_jt EUR not found, trying fallback sources")
        eur_fallback_a_snap = get_source_snapshot(EUR_FALLBACK_A)
        eur_mid = extract_eur_tomans_fallback(eur_fallback_a_snap["posts"]) if eur_fallback_a_snap["ok"] else None
        if eur_mid is None:
            eur_fallback_b_snap = get_source_snapshot(EUR_FALLBACK_B)
            eur_mid = extract_eur_tomans_fallback(eur_fallback_b_snap["posts"]) if eur_fallback_b_snap["ok"] else None
        if eur_mid:
            eur_buy_raw, eur_sell_raw = spread(eur_mid, EUR_SPREAD)
            eur_source = EUR_FALLBACK_A if eur_fallback_a_snap["ok"] else EUR_FALLBACK_B
            log.info("EUR fallback computed: mid=%.0f buy=%d sell=%d", eur_mid, eur_buy_raw, eur_sell_raw)

    # -------- STRICT: if no fresh price from any source => SKIP --------
    if usd_buy_raw is None or eur_buy_raw is None:
        log.warning(
            "No fresh price available: usd_source=%s eur_source=%s — SKIPPING to avoid stale post",
            usd_source, eur_source
        )
        save_state(state)
        result.update({"action": "skip", "detail": "no_fresh_data"})
        return result

    log.info("Prices fetched: USD buy=%d sell=%d (src=%s) | EUR buy=%d sell=%d (src=%s)",
             usd_buy_raw, usd_sell_raw, usd_source, eur_buy_raw, eur_sell_raw, eur_source)

    # -------- TRY (lira) --------
    usd_lira, eur_lira = try_lira_rates()
    last = state.get("last_keys", {})

    effective_usd_lira = usd_lira if (usd_lira and 30 <= usd_lira <= 60) else 44.91
    display_usd_lira = round(effective_usd_lira, 4)

    usd_mid_for_try = (usd_buy_raw + usd_sell_raw) / 2.0
    try_mid = usd_mid_for_try / display_usd_lira
    try_buy, try_sell = spread(try_mid, TRY_SPREAD, step=10)
    log.info("TRY mid calculated: %.2f / %.4f = %.2f -> Buy: %d, Sell: %d",
             usd_mid_for_try, display_usd_lira, try_mid, try_buy, try_sell)

    new_keys = {
        "usd_buy": usd_buy_raw, "usd_sell": usd_sell_raw,
        "eur_buy": eur_buy_raw, "eur_sell": eur_sell_raw,
        "try_buy": try_buy, "try_sell": try_sell,
        "try_usd_lira": display_usd_lira,
        "try_eur_lira": round(eur_lira, 4) if eur_lira else last.get("try_eur_lira"),
    }

    new_source_clocks = {
        USD_EUR_PRIMARY: pi_snap.get("clock"),
        USD_FALLBACK_A: usd_fallback_a_snap.get("clock"),
        USD_FALLBACK_B: usd_fallback_b_snap.get("clock"),
        EUR_FALLBACK_A: eur_fallback_a_snap.get("clock"),
        EUR_FALLBACK_B: eur_fallback_b_snap.get("clock"),
    }

    changed = keys_changed(last, new_keys)
    mins_silent = minutes_since(state.get("last_post_utc"))

    # -------- STRICT duplicate guard --------
    # Post ONLY if: price changed, OR silence >= SILENCE_LIMIT_MIN
    # NEVER post if price is same AND silence < SILENCE_LIMIT_MIN
    if changed:
        decision = "post"
        reason = "change"
    elif mins_silent is None or mins_silent >= SILENCE_LIMIT_MIN:
        decision = "post"
        reason = "silence_limit"
    else:
        decision = "skip"
        reason = "no_change_within_silence_window"

    log.info(
        "cycle Tehran=%s changed=%s silent=%s -> %s (%s)",
        t_tehran.strftime("%H:%M"),
        changed,
        f"{mins_silent:.1f}min" if mins_silent is not None else "never",
        decision,
        reason,
    )

    if decision == "post":
        msg = render_post(
            usd_buy_raw, usd_sell_raw,
            eur_buy_raw, eur_sell_raw,
            try_buy, try_sell,
            new_keys["try_usd_lira"], new_keys["try_eur_lira"],
        )
        resp = tg_send_message(msg)
        if resp.get("ok"):
            state["last_keys"] = new_keys
            state["last_source_clocks"] = new_source_clocks
            state["last_post_utc"] = t_utc.isoformat()
            save_state(state)
            result.update({"action": "posted", "detail": reason})
        else:
            log.error("tg send failed: %s", resp)
            save_state(state)
            result.update({"action": "error", "detail": f"send failed: {resp}"})
    else:
        state["last_source_clocks"] = new_source_clocks
        save_state(state)
        result.update({"action": "skip", "detail": reason})

    return result


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN missing; aborting")
        return 2
    try:
        res = run_cycle()
        log.info("cycle result: %s", res)
        return 0
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
