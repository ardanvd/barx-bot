#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BARX Live Monitor
-----------------
Policy (enforced):
- Working hours: 09:00 -> 00:00 Tehran time (Asia/Tehran). Outside hours => idle.
- At 00:00 (once per day), publish the "end of trading" message.
- USD: weighted avg of @dollar_tehran3bze (75%) + @tahran_sabza (25%).
  Bonbast is only a deviation guard / fallback.
- EUR: primary @navasanchannel, fallback @irancurrency.
- TRY lira rate: fetched from a public FX endpoint (TRY/USD -> derive).
- Smart posting: publish if any tracked key changes OR silence > 15 min AND
  source channels show fresh activity (newer post clock).
- Buy/Sell spread: 1,000 Toman for USD & EUR; 100 Toman for TRY (buy lower).
- Ordinary duplicate posts blocked: if no change AND silence <= 15 min => skip.
- last_post_utc persisted in state.
- Order contact @Arda_ist1; channel @barxexchange.

Files:
- /home/ubuntu/barx_live_monitor.py   (this file)
- /home/ubuntu/barx_live_state.json   (state)
- /home/ubuntu/barx_live_monitor.log  (log)
- /home/ubuntu/barx_memory.md         (human memory)
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
HOME  HOME = Path(".")
STATE_PATH = HOME / "barx_live_state.json"
LOG_PATH = HOME / "barx_live_monitor.log"
ENV_PATH = HOME / ".barx_env"

CHANNEL = "@barxexchange"
ORDER_CONTACT = "@Arda_ist1"

USD_PRIMARY = "dollar_tehran3bze"   # weight 0.75
USD_SECONDARY = "tahran_sabza"      # weight 0.25
USD_WEIGHT_PRIMARY = 0.75
USD_WEIGHT_SECONDARY = 0.25

EUR_PRIMARY = "navasanchannel"
EUR_BACKUP = "irancurrency"

SILENCE_LIMIT_MIN = 15              # minutes; > 15 min silence + activity => post
DEVIATION_GUARD_PCT = 3.0           # % deviation vs Bonbast allowed
WORKING_HOURS_START = 9             # 09:00 Tehran
WORKING_HOURS_END = 24              # 00:00 next day (exclusive)

# Spreads (Toman) - buy is lower than sell
USD_SPREAD = 1000
EUR_SPREAD = 1000
TRY_SPREAD = 100

TEHRAN_TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))  # Asia/Tehran (no DST in IR since 2022)

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
    # fall back to process env
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL"):
        if k not in env and os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()
BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = ENV.get("TELEGRAM_CHANNEL", CHANNEL)


# -------------------- State --------------------

DEFAULT_STATE: Dict[str, Any] = {
    "last_post_utc": None,             # ISO string
    "last_keys": {                     # last published tracked values
        "usd_buy": None, "usd_sell": None,
        "eur_buy": None, "eur_sell": None,
        "try_buy": None, "try_sell": None,
        "try_usd_lira": None, "try_eur_lira": None,
    },
    "last_source_clocks": {            # last seen source-channel post clocks
        USD_PRIMARY: None,
        USD_SECONDARY: None,
        EUR_PRIMARY: None,
        EUR_BACKUP: None,
    },
    "end_of_trading_date": None,       # YYYY-MM-DD of last EOT message
    "last_cycle_utc": None,
}


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            # merge with defaults so new keys don't break older states
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
    """
    Working window: 09:00:00 <= t < 24:00:00 (Tehran). 00:00 exact is handled
    separately as end-of-trading trigger.
    """
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

MSG_BLOCK_RE = re.compile(r"tgme_widget_message_wrap")

NUM_RE = re.compile(r"[\d,\.]+")
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬", "0123456789..")


def _normalize_num(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.translate(PERSIAN_DIGITS).replace(",", "").strip()
    try:
        return float(s)
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


def parse_latest_posts(html_text: str, limit: int = 8) -> List[Dict[str, Any]]:
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


def extract_usd_tomans(posts: List[Dict[str, Any]]) -> Optional[float]:
    """
    Best-effort extractor: scans latest posts for a number in Tomans range
    (typical IRR/USD 2024-2026: 80k..250k Toman). Returns the most recent
    plausible value found.
    """
    for p in reversed(posts):  # newest first (list order is oldest->newest)
        txt = p.get("text", "")
        if not txt:
            continue
        txt_norm = txt.translate(PERSIAN_DIGITS)
        # strip any Persian "/" inside numbers (decimal sep)
        for m in NUM_RE.finditer(txt_norm):
            val = _normalize_num(m.group(0))
            if val is None:
                continue
            if 80_000 <= val <= 300_000:
                return val
    return None


def extract_eur_tomans(posts: List[Dict[str, Any]]) -> Optional[float]:
    for p in reversed(posts):
        txt = p.get("text", "")
        if not txt:
            continue
        txt_norm = txt.translate(PERSIAN_DIGITS)
        for m in NUM_RE.finditer(txt_norm):
            val = _normalize_num(m.group(0))
            if val is None:
                continue
            if 100_000 <= val <= 400_000:
                return val
    return None


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


# -------------------- Bonbast guard --------------------

def bonbast_usd_toman() -> Optional[float]:
    """
    Bonbast scraping is unreliable (page returns numbers that are often sub-rates
    like coin prices, not USD). Guard is disabled until a robust JSON endpoint
    is wired in. We keep the function but return None so the weighted average
    from source channels is used directly. The deviation-guard logic still
    functions the moment this returns a valid number.
    """
    return None


# -------------------- Lira cross-rate --------------------

def try_lira_rates() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (USD_in_Lira, EUR_in_Lira) using exchangerate.host (free, no key).
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
    # fallback: open.er-api.com
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

def round_to_nearest(value: float, step: int = 50) -> int:
    return int(round(value / step) * step)


def compute_usd_mid(sources: Dict[str, Optional[float]]) -> Optional[float]:
    p = sources.get(USD_PRIMARY)
    s = sources.get(USD_SECONDARY)
    if p and s:
        return USD_WEIGHT_PRIMARY * p + USD_WEIGHT_SECONDARY * s
    if p:
        return p
    if s:
        return s
    return None


def apply_bonbast_guard(weighted: Optional[float], bonbast: Optional[float]) -> Optional[float]:
    if weighted is None:
        return bonbast
    if bonbast is None:
        return weighted
    # Only trust bonbast when it is within a sane IRR/USD range (50k..300k Toman)
    if not (50_000 <= bonbast <= 300_000):
        return weighted
    deviation = abs(weighted - bonbast) / bonbast * 100.0
    if deviation > DEVIATION_GUARD_PCT:
        log.warning(
            "deviation guard: weighted=%.0f bonbast=%.0f diff=%.2f%% > %.2f%% — using weighted (guard informational)",
            weighted, bonbast, deviation, DEVIATION_GUARD_PCT,
        )
        # keep the weighted value; guard is informational, not a hard override
        return weighted
    return weighted


def spread(mid: float, spread_value: int) -> Tuple[int, int]:
    """Returns (buy, sell) with buy = mid - spread/2, sell = mid + spread/2, rounded."""
    buy = round_to_nearest(mid - spread_value / 2)
    sell = round_to_nearest(mid + spread_value / 2)
    # enforce exact spread
    if sell - buy != spread_value:
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
    is_smart_silence_update: bool,
) -> str:
    header = (
        "⏱ Barx Exchange - آپدیت هوشمند بازار"
        if is_smart_silence_update
        else "🚀 Barx Exchange - نرخ لحظه‌ای ارز"
    )
    preamble = (
        "\n\nنرخ‌های اصلی نسبت به پست قبلی ثابت مانده‌اند، اما بازار همچنان فعال است.\n"
        if is_smart_silence_update
        else "\n"
    )
    msg = (
        f"{header}"
        f"{preamble}\n"
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


def sources_show_fresh_activity(
    prev_clocks: Dict[str, Optional[str]], new_clocks: Dict[str, Optional[str]]
) -> bool:
    for k, v in new_clocks.items():
        if v and prev_clocks.get(k) != v:
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
    # Trigger window: first 5 minutes after 00:00 each day, once per date.
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
        log.info(
            "outside working hours (Tehran %s); sleeping",
            t_tehran.strftime("%H:%M"),
        )
        save_state(state)
        result.update({"action": "idle", "detail": "outside_working_hours"})
        return result

    # -------- Gather USD --------
    usd_p_snap = get_source_snapshot(USD_PRIMARY)
    usd_s_snap = get_source_snapshot(USD_SECONDARY)
    usd_p_val = extract_usd_tomans(usd_p_snap["posts"]) if usd_p_snap["ok"] else None
    usd_s_val = extract_usd_tomans(usd_s_snap["posts"]) if usd_s_snap["ok"] else None
    bonb = bonbast_usd_toman()
    usd_mid_raw = compute_usd_mid({USD_PRIMARY: usd_p_val, USD_SECONDARY: usd_s_val})
    usd_mid = apply_bonbast_guard(usd_mid_raw, bonb)

    # -------- Gather EUR --------
    eur_p_snap = get_source_snapshot(EUR_PRIMARY)
    eur_mid = extract_eur_tomans(eur_p_snap["posts"]) if eur_p_snap["ok"] else None
    eur_b_snap = {"ok": False, "posts": [], "clock": None}
    if eur_mid is None:
        eur_b_snap = get_source_snapshot(EUR_BACKUP)
        if eur_b_snap["ok"]:
            eur_mid = extract_eur_tomans(eur_b_snap["posts"])

    # -------- TRY (lira) --------
    usd_lira, eur_lira = try_lira_rates()

    # -------- Fallbacks from last state if scraping missed --------
    last = state.get("last_keys", {})
    if usd_mid is None and last.get("usd_buy") and last.get("usd_sell"):
        usd_mid = (last["usd_buy"] + last["usd_sell"]) / 2.0
        log.info("USD fallback to previous state mid=%.0f", usd_mid)
    if eur_mid is None and last.get("eur_buy") and last.get("eur_sell"):
        eur_mid = (last["eur_buy"] + last["eur_sell"]) / 2.0
        log.info("EUR fallback to previous state mid=%.0f", eur_mid)

    if usd_mid is None or eur_mid is None:
        log.error("insufficient data: usd_mid=%s eur_mid=%s", usd_mid, eur_mid)
        save_state(state)
        result.update({"action": "skip", "detail": "no_data"})
        return result

    # -------- Compute buy/sell with spreads --------
    usd_buy, usd_sell = spread(usd_mid, USD_SPREAD)
    eur_buy, eur_sell = spread(eur_mid, EUR_SPREAD)

    # TRY havale (Toman per Lira): USD_Toman / USD_Lira, then +- spread/2
    if usd_lira and usd_mid:
        try_mid = usd_mid / usd_lira
        try_buy, try_sell = spread(try_mid, TRY_SPREAD)
    else:
        # reuse previous if available
        try_buy = last.get("try_buy") or 0
        try_sell = last.get("try_sell") or 0

    new_keys = {
        "usd_buy": usd_buy, "usd_sell": usd_sell,
        "eur_buy": eur_buy, "eur_sell": eur_sell,
        "try_buy": try_buy, "try_sell": try_sell,
        "try_usd_lira": round(usd_lira, 4) if usd_lira else last.get("try_usd_lira"),
        "try_eur_lira": round(eur_lira, 4) if eur_lira else last.get("try_eur_lira"),
    }

    new_source_clocks = {
        USD_PRIMARY: usd_p_snap.get("clock"),
        USD_SECONDARY: usd_s_snap.get("clock"),
        EUR_PRIMARY: eur_p_snap.get("clock"),
        EUR_BACKUP: eur_b_snap.get("clock"),
    }

    prev_clocks = state.get("last_source_clocks", {})
    changed = keys_changed(last, new_keys)
    mins_silent = minutes_since(state.get("last_post_utc"))
    activity = sources_show_fresh_activity(prev_clocks, new_source_clocks)

    decision = "skip"
    is_smart_update = False
    if changed:
        decision = "post"
    elif (mins_silent is None) or (mins_silent > SILENCE_LIMIT_MIN and activity):
        decision = "post"
        is_smart_update = True  # "market still active" variant
    else:
        decision = "skip"

    log.info(
        "cycle Tehran=%s changed=%s silent=%s activity=%s -> %s",
        t_tehran.strftime("%H:%M"),
        changed,
        f"{mins_silent:.1f}min" if mins_silent is not None else "never",
        activity,
        decision,
    )

    if decision == "post":
        msg = render_post(
            usd_buy, usd_sell,
            eur_buy, eur_sell,
            try_buy, try_sell,
            new_keys["try_usd_lira"], new_keys["try_eur_lira"],
            is_smart_silence_update=is_smart_update,
        )
        resp = tg_send_message(msg)
        if resp.get("ok"):
            state["last_keys"] = new_keys
            state["last_source_clocks"] = new_source_clocks
            state["last_post_utc"] = t_utc.isoformat()
            save_state(state)
            result.update({"action": "posted", "detail": "smart_update" if is_smart_update else "change"})
        else:
            log.error("tg send failed: %s", resp)
            save_state(state)
            result.update({"action": "error", "detail": f"send failed: {resp}"})
    else:
        # still refresh source clocks so next silence check is accurate
        state["last_source_clocks"] = new_source_clocks
        save_state(state)
        result.update({"action": "skip", "detail": "no_change_within_silence_window"})

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
