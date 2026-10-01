import asyncio
import os
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta

from playwright.async_api import async_playwright
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import base64
import hmac
import uuid
import math
import re


# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

URL = os.getenv("SOURCE_URL", "https://gamblingcounting.com/ru/roulette/pragmatic")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else None
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
state_lock = threading.RLock()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
if os.getenv("RAILWAY_ENVIRONMENT") and not DASHBOARD_PASSWORD:
    raise RuntimeError("Set DASHBOARD_PASSWORD in Railway Variables")


# ============================================================
# ІСТОРІЯ МАКСИМУМІВ + ФІЛЬТРИ ДЛЯ TELEGRAM
# ============================================================

STATS_MAX_HISTORY_FILE = os.path.join(DATA_DIR, "stats_max_history_v14.json")
stats_history_lock = threading.Lock()
filter_rules_lock = threading.Lock()

# {roulette: {metric: [{"value": 17, "time": "..."}]}}
stats_max_history = {}
previous_metric_values = {}
server_filter_rules = []
active_filter_signal_keys = set()

ROULETTE_LINKS = {
    "Romanian Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/romanian-roulette",
    "Speed Roulette 2": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/speed-roulette-2",
    "Roulette 2 Extra Time": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-green",
    "Speed Auto Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/speed-auto-roulette",
    "Auto Mega Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/auto-mega-roulette",
    "Roulette Macao": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-macao",
    "Roulette 1": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-azure",
    "Lucky 6 Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/lucky-6-roulette",
    "Mega Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/mega-roulette",
}

def save_stats_max_history():
    try:
        with open(STATS_MAX_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(stats_max_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Не вдалося зберегти історію максимумів: {e}")

def reset_stats_max_history():
    global stats_max_history, previous_metric_values, active_filter_signal_keys
    with stats_history_lock:
        stats_max_history = {}
        previous_metric_values = {}
        active_filter_signal_keys = set()
        save_stats_max_history()

def record_completed_metric_streaks(stats):
    """Зберігає завершені лічильники невипадіння/ALT і TOP-5 для кожної комірки."""
    changed = False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with stats_history_lock:
        for roulette, row in stats.items():
            prev_row = previous_metric_values.setdefault(roulette, {})
            roulette_hist = stats_max_history.setdefault(roulette, {})
            for metric, raw in row.items():
                if metric == "spins":
                    continue
                try:
                    cur = int(raw)
                except Exception:
                    continue
                prev = prev_row.get(metric)
                # Коли поточна серія впала, попереднє значення є завершеною серією.
                if prev is not None and prev > 0 and cur < prev:
                    arr = roulette_hist.setdefault(metric, [])
                    arr.append({"value": int(prev), "time": now})
                    arr.sort(key=lambda x: int(x.get("value", 0)), reverse=True)
                    del arr[5:]  # зберігаємо TOP-5
                    changed = True
                prev_row[metric] = cur
        if changed:
            save_stats_max_history()

def rule_matches(rule, metric, value):
    if rule.get("metric") not in ("ALL", metric):
        return False
    try:
        n = float(rule.get("value", 0))
        v = float(value)
    except Exception:
        return False
    op = rule.get("op", "gte")
    if op == "eq":
        return v == n
    if op == "lte":
        return v <= n
    return v >= n

async def process_filter_telegram_signals(stats):
    """Надсилає Telegram один раз при вході комірки в умову фільтра."""
    global active_filter_signal_keys
    with filter_rules_lock:
        rules = [dict(r) for r in server_filter_rules]

    current_keys = set()
    new_signals = []

    for roulette, row in stats.items():
        for metric, raw in row.items():
            if metric == "spins":
                continue
            for rule in rules:
                if rule_matches(rule, metric, raw):
                    rid = str(rule.get("id", "rule"))
                    key = f"{rid}|{roulette}|{metric}"
                    current_keys.add(key)
                    if key not in active_filter_signal_keys:
                        op_txt = {"gte": "≥", "eq": "=", "lte": "≤"}.get(rule.get("op"), "≥")
                        new_signals.append((
                            roulette, metric, int(raw),
                            f"{metric} {op_txt} {rule.get('value', 0)}"
                        ))

    # Оновлюємо стан ДО await, щоб один сигнал не дублювався.
    active_filter_signal_keys = current_keys

    for roulette, metric, value, rule_text in new_signals:
        link = ROULETTE_LINKS.get(roulette, "")
        message = (
            f"🚨 СИГНАЛ ПО ФІЛЬТРУ\n\n"
            f"🎰 {roulette}\n"
            f"📊 {metric}\n"
            f"🔥 Значення: {value}\n"
            f"🎯 Фільтр: {rule_text}"
        )
        if link:
            message += f"\n\n🔗 {link}"
        await send_telegram_message(message)


# ============================================================
# РУЛЕТКИ, ЯКІ АНАЛІЗУЄМО
# ============================================================

TARGET_ROULETTES = [
    "Romanian Roulette",
    "Speed Roulette 2",
    "Roulette 2 Extra Time",
    "Speed Auto Roulette",
    "Auto Mega Roulette",
    "Roulette Macao",
    "Roulette 1",
    "Lucky 6 Roulette",
    "Mega Roulette"
]


# ============================================================
# ПАРАМЕТРИ
# ============================================================

MIN_DOZEN_STREAK = 5
MIN_COLUMN_STREAK = 5

CHECK_INTERVAL = 3
CLEAR_INTERVAL_MINUTES = 30


# ============================================================
# КЕШ ПОВІДОМЛЕНЬ
# ============================================================

sent_combinations = set()
last_clear_time = datetime.now()


# ============================================================
# ДЮЖИНИ
# ============================================================

def get_dozen(number: int):
    """
    1 дюжина: 1-12
    2 дюжина: 13-24
    3 дюжина: 25-36
    0: None
    """

    if 1 <= number <= 12:
        return 1

    if 13 <= number <= 24:
        return 2

    if 25 <= number <= 36:
        return 3

    return None


# ============================================================
# КОЛОНИ
# ============================================================

def get_column(number: int):
    """
    Колона 1:
    1, 4, 7 ... 34

    Колона 2:
    2, 5, 8 ... 35

    Колона 3:
    3, 6, 9 ... 36

    0: None
    """

    if number == 0:
        return None

    if number % 3 == 1:
        return 1

    if number % 3 == 2:
        return 2

    if number % 3 == 0:
        return 3

    return None


# ============================================================
# ПОШУК ПОТОЧНОЇ СЕРІЇ
# ============================================================

def get_streak(numbers, classifier):
    """
    numbers мають бути у форматі:

    [
        найновіше число,
        попереднє число,
        ...
    ]

    classifier:
    get_dozen або get_column

    Повертає:
    довжину серії,
    номер дюжини/колони
    """

    if not numbers:
        return 0, None

    first_value = classifier(numbers[0])

    # Наприклад, якщо останнє число = 0
    if first_value is None:
        return 0, None

    streak = 0

    for number in numbers:

        current_value = classifier(number)

        if current_value != first_value:
            break

        streak += 1

    return streak, first_value


# ============================================================
# ВІЗУАЛЬНА СТОРІНКА / АКТИВНІ ТА ЗАВЕРШЕНІ СЕРІЇ
# ============================================================
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.getenv("PORT", "8765"))
HISTORY_FILE = os.path.join(DATA_DIR, "series_history_v2.json")
history_lock = threading.Lock()
session_lock = threading.Lock()
session_generation = 0
session_numbers = {name: [] for name in TARGET_ROULETTES}
session_last_signature = {name: None for name in TARGET_ROULETTES}

def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

series_history = []

# Поточний стан для кожної рулетки. Серія з'являється тут від 5+
dashboard_state = {
    name: {
        "dozen": None,
        "column": None,
        "last_seen": None,
        "updated": None,
    }
    for name in TARGET_ROULETTES
}

def save_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(series_history, f, ensure_ascii=False, indent=2)

def finalize_active_series(roulette, kind_key):
    """Переносить активну серію в історію ОДИН раз після її завершення."""
    state = dashboard_state[roulette]
    active = state.get(kind_key)
    if not active:
        return

    minimum = MIN_DOZEN_STREAK if kind_key == "dozen" else MIN_COLUMN_STREAK
    if active["streak"] < minimum:
        state[kind_key] = None
        return

    event = {
        "id": f"{kind_key}-{roulette}-{active['group']}-{active['started_at']}",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "roulette": roulette,
        "kind": "Дюжина" if kind_key == "dozen" else "Колона",
        "group": active["group"],
        "streak": active["streak"],
        "numbers": active.get("numbers", []),
    }
    with history_lock:
        series_history.append(event)
        del series_history[:-2000]
        save_history()
    state[kind_key] = None


def get_session_numbers(roulette, page_numbers):
    """Після RESET старі числа, які ще видно на джерельному сайті, не враховуються.
    Перший знімок після reset стає базою. Далі додаються лише нові спіни."""
    if not page_numbers:
        return []

    sig = tuple(page_numbers[:8])
    with session_lock:
        old_sig = session_last_signature.get(roulette)

        # Перший знімок після запуску/reset: лише запам'ятати, нічого не рахувати.
        if old_sig is None:
            session_last_signature[roulette] = sig
            return list(session_numbers[roulette])

        if sig == old_sig:
            return list(session_numbers[roulette])

        # Знаходимо, скільки нових чисел додалося на початок стрічки.
        added = []
        for pos in range(1, len(page_numbers)):
            overlap = min(len(old_sig), len(page_numbers) - pos)
            if overlap >= min(3, len(old_sig)) and tuple(page_numbers[pos:pos+overlap]) == old_sig[:overlap]:
                added = page_numbers[:pos]
                break

        # Якщо збіг не знайшовся, безпечний fallback: беремо тільки найновіший спін.
        if not added:
            added = [page_numbers[0]]

        # newest-first
        session_numbers[roulette] = added + session_numbers[roulette]
        session_numbers[roulette] = session_numbers[roulette][:500]
        session_last_signature[roulette] = sig
        return list(session_numbers[roulette])


def update_dashboard_series(roulette, numbers, dozen_streak, dozen_number, column_streak, column_number):
    """Оновлюється лише коли з'явився новий результат рулетки."""
    if not numbers:
        return

    state = dashboard_state[roulette]
    newest = numbers[0]

    # Не обробляємо той самий спін повторно кожні 3 секунди.
    # Для надійності використовуємо кілька найновіших чисел як підпис стрічки.
    signature = tuple(numbers[:8])
    if state.get("last_seen") == signature:
        return
    state["last_seen"] = signature
    state["updated"] = datetime.now().strftime("%H:%M:%S")

    for kind_key, streak, group, minimum in (
        ("dozen", dozen_streak, dozen_number, MIN_DOZEN_STREAK),
        ("column", column_streak, column_number, MIN_COLUMN_STREAK),
    ):
        active = state.get(kind_key)

        if group is not None and streak >= minimum:
            # Якщо це продовження тієї самої активної серії — просто змінюємо 5→6→7...
            if active and active["group"] == group:
                active["streak"] = streak
                active["numbers"] = numbers[:streak]
                active["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                # Стара серія закінчилась, а нова вже встигла дійти до порога.
                if active:
                    finalize_active_series(roulette, kind_key)
                state[kind_key] = {
                    "group": group,
                    "streak": streak,
                    "numbers": numbers[:streak],
                    "started_at": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
        else:
            # Серія більше не 5+ — попередня активна завершилась.
            if active:
                finalize_active_series(roulette, kind_key)



RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACK_NUMBERS = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
TIER_NUMBERS = {27,13,36,11,30,8,23,10,5,24,16,33}
ORPH_NUMBERS = {1,20,14,31,9,17,34,6}
VOIS_NUMBERS = {22,18,29,7,28,12,35,3,26,0,32,15,19,4,21,2,25}
ZERO_NUMBERS = {12,35,3,26,0,32,15}

# Неперекривні групи по порядку європейського колеса.
EUROPEAN_WHEEL = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]
NEIGHBOR_GROUPS = [EUROPEAN_WHEEL[i:i+3] for i in range(0, len(EUROPEAN_WHEEL), 3)]

def _wait_for_set(nums, values):
    """Кількість останніх спінів від останнього попадання в групу. 0 = останній спін був у групі."""
    for i,n in enumerate(nums):
        if n in values:
            return i
    return len(nums)

def _alt_streak(nums, classifier):
    """Довжина поточного чергування категорій; 0/None перериває."""
    vals=[]
    for n in nums:
        v=classifier(n)
        if v is None: break
        vals.append(v)
    if len(vals)<2: return 0
    k=1
    for i in range(1,len(vals)):
        if vals[i] == vals[i-1]: break
        k += 1
    return k if k>=2 else 0

def build_winspin_stats():
    result={}
    with session_lock:
        snapshots={name:list(session_numbers.get(name,[])) for name in TARGET_ROULETTES}
    for name,nums in snapshots.items():
        row={"spins":len(nums)}
        row.update({
            "Red":_wait_for_set(nums,RED_NUMBERS), "Black":_wait_for_set(nums,BLACK_NUMBERS),
            "ALT_RB":_alt_streak(nums, lambda n: 'R' if n in RED_NUMBERS else ('B' if n in BLACK_NUMBERS else None)),
            "Even":_wait_for_set(nums,{n for n in range(1,37) if n%2==0}), "Odd":_wait_for_set(nums,{n for n in range(1,37) if n%2==1}),
            "ALT_EO":_alt_streak(nums, lambda n: None if n==0 else n%2),
            "Small":_wait_for_set(nums,set(range(1,19))), "Big":_wait_for_set(nums,set(range(19,37))),
            "ALT_SB":_alt_streak(nums, lambda n: None if n==0 else (1 if n<=18 else 2)),
            "1st":_wait_for_set(nums,set(range(1,13))), "2d":_wait_for_set(nums,set(range(13,25))), "3d":_wait_for_set(nums,set(range(25,37))),
            "ALT_D":_alt_streak(nums,get_dozen),
            "1col":_wait_for_set(nums,{n for n in range(1,37) if n%3==1}), "2col":_wait_for_set(nums,{n for n in range(1,37) if n%3==2}), "3col":_wait_for_set(nums,{n for n in range(1,37) if n%3==0}),
            "ALT_C":_alt_streak(nums,get_column),
            "TIER":_wait_for_set(nums,TIER_NUMBERS), "ORPH":_wait_for_set(nums,ORPH_NUMBERS), "VOIS":_wait_for_set(nums,VOIS_NUMBERS), "ZERO":_wait_for_set(nums,ZERO_NUMBERS),
        })
        # Streets / трійки: 1-3, 4-6 ... 34-36
        for start in range(1,37,3):
            row[f"S{start}-{start+2}"]=_wait_for_set(nums,{start,start+1,start+2})

        # Six lines / шістки: 1-6, 4-9 ... 31-36
        for start in range(1,32,3):
            row[f"L{start}-{start+5}"]=_wait_for_set(nums,set(range(start,start+6)))

        # Corners / кути: 1-2-4-5 ... 32-33-35-36
        for r in range(0,11):
            a=1+r*3
            for c in (0,1):
                x=a+c
                key=f"C{x}-{x+1}-{x+3}-{x+4}"
                row[key]=_wait_for_set(nums,{x,x+1,x+3,x+4})

        # Сусіди: неперекривні групи по 3 числа в порядку колеса.
        for idx, group in enumerate(NEIGHBOR_GROUPS, start=1):
            row[f"NGR{idx}"] = _wait_for_set(nums, set(group))
        # Individual number misses for the visual roulette table only.
        for number in range(37):
            row[f"NUM{number}"] = _wait_for_set(nums, {number})

        # Percentage statistics for dozens and columns on the MAIN roulette cards.
        # Percentages are calculated from ALL spins in the selected window;
        # zero therefore belongs to neither a dozen nor a column.
        row["PCT"] = {}
        for window in (36, 50, 100, 200):
            recent = nums[:window]
            total = len(recent)
            if total:
                d_counts = [
                    sum(1 for n in recent if 1 <= n <= 12),
                    sum(1 for n in recent if 13 <= n <= 24),
                    sum(1 for n in recent if 25 <= n <= 36),
                ]
                c_counts = [
                    sum(1 for n in recent if n != 0 and n % 3 == 1),
                    sum(1 for n in recent if n != 0 and n % 3 == 2),
                    sum(1 for n in recent if n != 0 and n % 3 == 0),
                ]
                row["PCT"][str(window)] = {
                    "sample": total,
                    "dozens": [round(x * 100 / total, 1) for x in d_counts],
                    "columns": [round(x * 100 / total, 1) for x in c_counts],
                }
            else:
                row["PCT"][str(window)] = {"sample": 0, "dozens": [0,0,0], "columns": [0,0,0]}
        result[name]=row
    return result

DASHBOARD_HTML = r"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Моніторинг рулеток</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#081321;color:#f8fafc;font-family:Arial,sans-serif;padding:20px}.wrap{max-width:1550px;margin:auto}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}.title h1{margin:0;font-size:32px}.sub{color:#b7c4d5;margin-top:6px}.online{background:#10243a;border:1px solid #29425f;border-radius:12px;padding:12px 16px}.dot{display:inline-block;width:12px;height:12px;border-radius:50%;background:#16d45b;margin-right:8px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.roulette{border:1px solid #29445f;background:#0c1b2c;border-radius:14px;padding:12px;min-width:0}.rhead{font-size:21px;font-weight:700;margin:0 0 10px 4px;display:flex;align-items:center;gap:8px;white-space:nowrap;overflow:hidden}.rhead .rname{overflow:hidden;text-overflow:ellipsis}.roulette-link{flex:none;font-size:11px;font-weight:700;color:#e7f5ff;background:#1c527c;border:1px solid #39739f;border-radius:7px;padding:4px 7px;text-decoration:none}.roulette-link:hover{background:#286a9b;color:#fff}
.series-pct-toolbar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:0 0 14px;padding:10px 12px;border:1px solid #29445f;background:#0c1b2c;border-radius:12px}
.series-pct-title{font-size:13px;font-weight:800;color:#dce7f4;margin-right:4px}
.series-pct-btn{border:1px solid #526a82;background:#17314b;color:#eaf4ff;border-radius:8px;padding:7px 11px;font-weight:800;cursor:pointer}
.series-pct-btn.active{background:#1769aa;border-color:#49a8ef;box-shadow:0 0 0 1px #49a8ef inset}
.series-pct-btn:hover{filter:brightness(1.12)}
.rpct{margin-top:10px;border-top:1px solid #29445f;padding-top:9px}
.rpct-head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:7px;font-size:11px;color:#9fb1c5}
.rpct-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.rpct-box{background:#10243a;border:1px solid #29445f;border-radius:9px;padding:8px}
.rpct-title{font-size:11px;font-weight:900;color:#dce7f4;margin-bottom:5px}
.rpct-row{display:grid;grid-template-columns:42px 1fr 43px;gap:5px;align-items:center;margin:4px 0;font-size:10px}
.rpct-track{height:6px;background:#263b50;border-radius:8px;overflow:hidden}
.rpct-fill{height:100%;background:#38bdf8;border-radius:8px}
.rpct-val{text-align:right;font-weight:900;font-size:11px}
@media(max-width:420px){.rpct-grid{grid-template-columns:1fr}}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:10px}.section{min-width:0}.box{border-radius:11px;padding:10px;text-align:center;min-height:112px;border:1px solid #315273;background:#142a43}.box.dozen{background:#103765}.box.column{background:#10462e}.box.active{box-shadow:0 0 0 1px #18d462 inset}.label{font-weight:700;font-size:16px}.current{margin-top:6px;color:#c9d5e4;font-size:13px}.value{font-size:38px;font-weight:800;line-height:1;margin-top:4px}.badge{display:inline-block;background:#11bd4d;padding:5px 8px;border-radius:7px;font-weight:700;font-size:12px;margin-left:6px;vertical-align:middle}.none{display:inline-block;background:#36506b;padding:5px 8px;border-radius:7px;font-size:12px;margin-left:6px}.group{font-size:13px;margin-top:5px;color:#dce7f4}
.hist-title{font-size:13px;margin:9px 2px 6px;color:#dce7f4}.history-groups{display:grid;gap:7px}.roulette-timeline{margin-top:9px;background:#10243a;border-radius:9px;padding:7px 8px}.roulette-timeline-title{font-size:11px;color:#cfe0f1;margin-bottom:5px}.roulette-timeline-row{display:flex;gap:5px;overflow-x:auto;padding-bottom:2px}.roulette-timeline-num{flex:0 0 auto;min-width:27px;text-align:center;background:#183b5d;border-radius:6px;padding:5px 6px;font-size:13px;font-weight:800}.roulette-timeline-empty{font-size:11px;color:#6f8498}.history-row{display:grid;grid-template-columns:62px repeat(3,1fr);gap:5px;align-items:center}.history-label{font-size:12px;font-weight:700;color:#dce7f4;text-align:center}.hitem{border-radius:9px;text-align:center;padding:7px 3px;background:#124579;min-width:0}.section.column-section .hitem{background:#12543a}.hlen{font-size:21px;font-weight:800}.hgroup{font-size:10px;color:#dce7f4;white-space:nowrap}.empty{opacity:.5}
.reset-btn{border:1px solid #ef4444;background:#7f1d1d;color:#fff;border-radius:10px;padding:11px 16px;font-weight:800;cursor:pointer}.reset-btn:hover{background:#991b1b}.reset-btn:disabled{opacity:.55;cursor:wait}
.stats{margin-top:16px;border:1px solid #29445f;background:#0c1b2c;border-radius:14px;padding:14px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat{background:#10243a;border-radius:10px;padding:12px}.stat b{display:block;font-size:26px;margin-top:4px}.green{color:#2fe568}.blue{color:#38bdf8}.red{color:#fb5252}
.distribution{margin-top:16px;border:1px solid #29445f;background:#0c1b2c;border-radius:14px;padding:14px}.distribution h2{margin:0 0 12px;font-size:21px}.dist-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px}.dist-item{background:#10243a;border:1px solid #29445f;border-radius:10px;padding:12px;text-align:center}.dist-label{font-size:13px;color:#b7c4d5}.dist-length{font-size:22px;font-weight:800;margin-top:3px}.dist-count{font-size:30px;font-weight:800;color:#38bdf8;margin-top:5px}.dist-empty{color:#7890a8;font-size:14px;padding:8px 0}
@media(max-width:1050px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:700px){body{padding:10px}.grid{grid-template-columns:1fr}.title h1{font-size:25px}.stats{grid-template-columns:1fr 1fr}}

.tabs{display:flex;gap:8px;margin:0 0 14px}.tab-btn{border:1px solid #315d86;background:#10243a;color:#eaf2fb;border-radius:9px;padding:10px 15px;font-weight:800;cursor:pointer}.tab-btn.active{background:#1769aa}.view-hidden{display:none!important}
.stats-table-wrap{overflow:auto;background:#eee;border:1px solid #555;border-radius:4px;max-height:78vh}.ws-table{border-collapse:collapse;min-width:1700px;width:100%;font-family:Arial,sans-serif;color:#222;background:#ddd;font-size:14px}.ws-table th,.ws-table td{border:1px solid #666;padding:8px 7px;text-align:center;white-space:nowrap}.ws-table th{background:#bdbdbd;font-weight:800;position:sticky;top:0;z-index:2}.ws-table td.name{text-align:left;color:#28609a;font-weight:700;position:sticky;left:0;background:#e1e1e1;z-index:1}.ws-table th.name{left:0;z-index:3}.ws-table tr:nth-child(even) td{background:#eee}.ws-table tr:nth-child(even) td.name{background:#eee}.ws-table .sep{border-left:6px solid #555}.ws-table td.warn1{background:#e7a080!important}.ws-table td.warn2{background:#ef6f61!important}.ws-table td.warn3{background:#d92b20!important;color:white;font-weight:800}.ws-note{margin-top:10px;color:#9fb1c5;font-size:12px}.ws-columns{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 10px}.ws-columns-title{font-weight:900;color:#d9e7f5;margin-right:4px}.ws-toggle{border:1px solid #526a82;background:#17314b;color:#eaf4ff;border-radius:8px;padding:7px 10px;font-weight:800;cursor:pointer}.ws-toggle.off{background:#2a2f36;color:#aeb8c2;border-color:#4a5159}.ws-toggle:hover{filter:brightness(1.12)}
.ws-filter{background:#10243a;border:1px solid #315d86;border-radius:10px;padding:12px;margin-bottom:12px;color:#eaf2fb}
.ws-filter-title{font-weight:800;font-size:16px;margin-bottom:9px}
.ws-filter-row{display:flex;gap:9px;align-items:end;flex-wrap:wrap}
.ws-field{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#b9c9d9}
.ws-field select,.ws-field input[type=number]{background:#081b2e;color:#fff;border:1px solid #315d86;border-radius:7px;padding:7px 8px;min-height:34px}
.ws-field input[type=color]{width:58px;height:34px;background:#081b2e;border:1px solid #315d86;border-radius:7px;padding:2px}
.ws-filter-check{display:flex;align-items:center;gap:6px;height:34px;font-size:13px}
.ws-save{height:34px;border:1px solid #39739f;background:#1769aa;color:#fff;border-radius:7px;padding:0 12px;font-weight:800;cursor:pointer}
.ws-filter-status{margin-top:8px;font-size:12px;color:#9fb1c5}
.ws-rules{margin-top:12px;border-top:1px solid #29445f;padding-top:10px}
.ws-rules-title{font-size:13px;font-weight:800;margin-bottom:7px;color:#dce9f5}
.ws-rules-list{display:flex;gap:7px;flex-wrap:wrap}
.ws-rule{display:flex;align-items:center;gap:8px;background:#18344f;border:1px solid #315d86;border-radius:8px;padding:6px 8px;font-size:12px}
.ws-rule-dot{width:12px;height:12px;border-radius:3px;border:1px solid rgba(255,255,255,.35)}
.ws-rule-x{border:0;background:#8d2630;color:#fff;width:21px;height:21px;border-radius:50%;font-weight:900;cursor:pointer;line-height:19px}
.ws-rules-empty{font-size:12px;color:#71879c}
.ws-group-title{background:#8f8f8f!important;color:#111;font-size:11px}

.ws-name-link{color:#28609a;text-decoration:underline;font-weight:700}.ws-name-link:hover{color:#0b4d8a}

.ws-max-btn{margin-left:7px;border:1px solid #39739f;background:#164e72;color:#fff;border-radius:6px;padding:3px 6px;font-size:10px;font-weight:800;cursor:pointer}
.ws-max-btn:hover{background:#1d658f}
.max-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;align-items:center;justify-content:center;padding:20px}
.max-modal.open{display:flex}.max-card{width:min(980px,96vw);max-height:86vh;overflow:auto;background:#0c1b2c;border:1px solid #315d86;border-radius:14px;padding:16px;color:#fff}
.max-head{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:12px}.max-head h2{margin:0;font-size:22px}
.max-close{border:0;background:#8d2630;color:#fff;border-radius:8px;padding:7px 11px;font-weight:900;cursor:pointer}
.max-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}.max-item{background:#10243a;border:1px solid #29445f;border-radius:9px;padding:9px}
.max-metric{font-weight:900;color:#7dd3fc;margin-bottom:5px}.max-values{font-size:13px;line-height:1.55}.max-empty{color:#8296aa;font-size:13px}

.roulette-board-panel{margin-top:18px;border:1px solid #29445f;background:#0c1b2c;border-radius:14px;padding:14px}
.rb-toolbar{display:flex;gap:12px;align-items:end;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px}
.rb-title{font-size:21px;font-weight:900}.rb-sub{font-size:12px;color:#9fb1c5;margin-top:4px}
.rb-select{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#b9c9d9}.rb-select select{background:#081b2e;color:#fff;border:1px solid #315d86;border-radius:7px;padding:8px;min-width:240px}
.rb-colors{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 14px}.rb-color-item{display:flex;align-items:center;gap:5px;background:#10243a;border:1px solid #29445f;border-radius:8px;padding:6px 8px;font-size:12px}.rb-color-item input{width:30px;height:27px;border:0;padding:0;background:transparent}
.rb-wrap{overflow-x:auto}.rb-board{min-width:760px;display:grid;grid-template-columns:64px repeat(12,minmax(54px,1fr));grid-template-rows:repeat(3,68px);gap:4px;background:#07101b;border:4px solid #d7b56d;border-radius:10px;padding:7px}
.rb-cell{border:1px solid rgba(255,255,255,.72);display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:3px;color:#fff;text-shadow:0 1px 2px #000}
.rb-cell.rednum{background:#9d1f24}.rb-cell.blacknum{background:#171b20}.rb-cell.zeronum{background:#12613b;grid-row:1/4;grid-column:1;min-height:212px}
.rb-number{font-size:21px;font-weight:900;line-height:1}.rb-miss{margin-top:7px;font-size:11px;background:rgba(0,0,0,.45);padding:3px 6px;border-radius:10px;white-space:nowrap}.rb-miss b{font-size:15px}.rb-hit{outline:3px solid #fff;outline-offset:-4px}.rb-note{font-size:12px;color:#9fb1c5;margin-top:9px}

</style></head>
<body><div class="wrap">
<header><div class="title"><h1>🎰 Моніторинг рулеток</h1><div class="sub">Активні та останні завершені серії дюжин і колон</div></div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><button class="reset-btn" id="resetBtn" onclick="resetSession()">⟲ Новий сеанс / скинути історію</button><div class="online"><span class="dot"></span>Оновлення кожні 3 сек · <span id="clock">--:--:--</span></div></div></header>
<div class="tabs"><button class="tab-btn active" id="seriesTab" onclick="showView('series')">🎯 Серії</button><button class="tab-btn" id="statsTab" onclick="showView('stats')">📊 Таблиця статистики</button></div><div id="seriesView"><div class="series-pct-toolbar">
 <span class="series-pct-title">📊 Відсотки за останні:</span>
 <button type="button" class="series-pct-btn active" data-pct-window="36">36</button>
 <button type="button" class="series-pct-btn" data-pct-window="50">50</button>
 <button type="button" class="series-pct-btn" data-pct-window="100">100</button>
 <button type="button" class="series-pct-btn" data-pct-window="200">200</button>
</div>
<div class="grid" id="grid"></div>
</div><div id="statsView" class="view-hidden">
<div class="ws-filter">
 <div class="ws-filter-title">🔔 Фільтр сигналу</div>
 <div class="ws-filter-row">
  
  <label class="ws-field">Показник<select id="filterMetric"></select></label>
  <label class="ws-field">Умова<select id="filterOp"><option value="gte">≥ не менше</option><option value="eq">= дорівнює</option><option value="lte">≤ не більше</option></select></label>
  <label class="ws-field">Значення<input id="filterValue" type="number" min="0" value="10"></label>
  <label class="ws-field">Колір комірки<input id="filterColor" type="color" value="#ffcc00"></label>
  <button class="ws-save" onclick="addFilterRule()">＋ Додати умову</button>
 </div>
 <div class="ws-filter-status" id="filterStatus">Фільтр вимкнено</div>
 <div class="ws-rules">
  <div class="ws-rules-title">Вибрані умови — працюють одночасно:</div>
  <div class="ws-rules-list" id="filterRulesList"></div>
 </div>
</div>
<div class="ws-columns"><span class="ws-columns-title">👁 Комірки:</span><div id="wsGroupToggles"></div></div>
<div class="stats-table-wrap"><table class="ws-table"><thead id="wsHead"></thead><tbody id="wsBody"></tbody></table></div>
<div class="roulette-board-panel">
 <div class="rb-toolbar">
  <div><div class="rb-title">🎯 Візуальний стіл рулетки</div><div class="rb-sub">Невипадіння чисел 0–36. Усі налаштування нижче діють тільки на цей стіл.</div></div>
  <label class="rb-select">Рулетка<select id="rbRoulette"></select></label>
 </div>
 <div class="rb-colors" id="rbColors"></div>
 <div class="rb-wrap"><div class="rb-board" id="rbBoard"></div></div>
 <div class="rb-note">Під кожним числом — кількість спінів від останнього випадіння. 0 = випало останнім.</div>
</div><div class="max-modal" id="maxModal" onclick="if(event.target===this)closeMaxima()"><div class="max-card"><div class="max-head"><h2 id="maxTitle">Максимуми</h2><button class="max-close" onclick="closeMaxima()">✕ Закрити</button></div><div id="maxContent"></div></div></div><div class="ws-note">Число в клітинці = скільки спінів минуло від останнього попадання в цю категорію. 0 означає, що категорія випала в останньому спіні. «Сусіди» = неперекривні групи по порядку колеса: 0·32·15, 19·4·21, 2·25·17 … останнє 26 окремо.</div></div><div class="stats"><div class="stat">Активні серії<b class="green" id="activeCount">0</b></div><div class="stat">Завершені серії<b class="blue" id="completedCount">0</b></div><div class="stat">Максимальна серія<b class="red" id="maxStreak">0</b></div><div class="stat">Останнє оновлення<b id="lastUpdate" style="font-size:18px">—</b></div></div>
<div class="distribution"><h2>📊 Статистика завершених серій</h2><div class="dist-grid" id="distribution"></div></div>
</div>
<script>
const rouletteOrder = __ROULETTE_ORDER__;
let payload={state:{},history:[],winspin:{},maxima:{}};
const PCT_WINDOW_KEY='roulettePctWindowV1';
let pctWindow=36;
try{const saved=Number(localStorage.getItem(PCT_WINDOW_KEY));if([36,50,100,200].includes(saved))pctWindow=saved}catch(e){}
function pctBox(title,labels,values){
 values=Array.isArray(values)?values:[0,0,0];
 return `<div class="rpct-box"><div class="rpct-title">${title}</div>${labels.map((label,i)=>{
   const v=Number(values[i]||0);
   return `<div class="rpct-row"><span>${label}</span><div class="rpct-track"><div class="rpct-fill" style="width:${Math.max(0,Math.min(100,v))}%"></div></div><span class="rpct-val">${v.toFixed(1)}%</span></div>`;
 }).join('')}</div>`;
}
function roulettePctStats(name){
 const r=(payload.winspin||{})[name]||{},p=((r.PCT||{})[String(pctWindow)])||{sample:0,dozens:[0,0,0],columns:[0,0,0]};
 const sample=Number(p.sample)||0;
 return `<div class="rpct"><div class="rpct-head"><b>Статистика дюжин і колон</b><span>${sample?pctWindow+' вибрано · '+sample+' є':'немає даних'}</span></div><div class="rpct-grid">${pctBox('ДЮЖИНИ',['1–12','13–24','25–36'],p.dozens)}${pctBox('КОЛОНИ',['Кол. 1','Кол. 2','Кол. 3'],p.columns)}</div></div>`;
}
function setupPctButtons(){
 document.querySelectorAll('[data-pct-window]').forEach(btn=>{
   const n=Number(btn.dataset.pctWindow);
   btn.classList.toggle('active',n===pctWindow);
   if(!btn.dataset.bound){btn.dataset.bound='1';btn.addEventListener('click',()=>{
     pctWindow=n;
     try{localStorage.setItem(PCT_WINDOW_KEY,String(n))}catch(e){}
     document.querySelectorAll('[data-pct-window]').forEach(x=>x.classList.toggle('active',Number(x.dataset.pctWindow)===pctWindow));
     render();
   })}
 });
}
let previousActiveKeys = new Set();
let firstRender = true;
let audioCtx = null;

// М'який триразовий сигнал, схожий на щебет птаха.
function birdChirp(){
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
    const now = audioCtx.currentTime;
    function note(start, fromHz, toHz, duration, volume){
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(fromHz, start);
      osc.frequency.exponentialRampToValueAtTime(toHz, start + duration);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(volume, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(start);
      osc.stop(start + duration + 0.03);
    }
    note(now,       1200, 2050, 0.16, 0.12);
    note(now + .19, 1450, 2450, 0.14, 0.10);
    note(now + .35, 1700, 2850, 0.12, 0.08);
  }catch(e){}
}

// Один клік/натискання на сторінці дозволяє браузеру відтворювати звук.
function unlockAudio(){
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if(audioCtx.state === 'suspended') audioCtx.resume();
  }catch(e){}
}
document.addEventListener('click', unlockAudio, {once:true});
document.addEventListener('keydown', unlockAudio, {once:true});
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function lastCompletedGroup(name,kind,group){
 return payload.history.filter(x=>x.roulette===name&&x.kind===kind&&Number(x.group)===group).slice(-3).reverse()
}
function rouletteTimeline(name){
 const arr=payload.history.filter(x=>x.roulette===name);
 const cells=arr.length
   ? arr.map(x=>`<div class="roulette-timeline-num">${Number(x.streak)||0}</div>`).join('')
   : '<div class="roulette-timeline-empty">Поки немає завершених серій</div>';
 return `<div class="roulette-timeline"><div class="roulette-timeline-title">Усі серії цієї рулетки (по часу)</div><div class="roulette-timeline-row">${cells}</div></div>`;
}
function activeBox(kindKey, active){
 const label=kindKey==='dozen'?'Дюжини':'Колони'; const cls=kindKey==='dozen'?'dozen':'column';
 if(active) return `<div class="box ${cls} active"><div class="label">${label}</div><div class="current">Поточна серія</div><div><span class="value">${active.streak}</span><span class="badge">АКТИВНА</span></div><div class="group">(${active.group} ${kindKey==='dozen'?'дюжина':'колона'})</div></div>`;
 return `<div class="box ${cls}"><div class="label">${label}</div><div class="current">Поточна серія</div><div><span class="value">—</span><span class="none">Немає 5+</span></div><div class="group">&nbsp;</div></div>`;
}
function historyGroups(name,kindKey){
 const kind=kindKey==='dozen'?'Дюжина':'Колона';
 let rows='';
 for(let group=1;group<=3;group++){
   const arr=lastCompletedGroup(name,kind,group);
   let cells=arr.map(x=>`<div class="hitem"><div class="hlen">${x.streak}</div><div class="hgroup">серія</div></div>`).join('');
   for(let i=arr.length;i<3;i++) cells+=`<div class="hitem empty"><div class="hlen">—</div><div class="hgroup">немає</div></div>`;
   rows+=`<div class="history-row"><div class="history-label">${group} ${kindKey==='dozen'?'дюж.':'кол.'}</div>${cells}</div>`;
 }
 return `<div class="history-groups">${rows}</div>`;
}
const rouletteLinks={"Romanian Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/romanian-roulette", "Speed Roulette 2": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/speed-roulette-2", "Roulette 2 Extra Time": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-green", "Speed Auto Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/speed-auto-roulette", "Auto Mega Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/auto-mega-roulette", "Roulette Macao": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-macao", "Roulette 1": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/roulette-azure", "Lucky 6 Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/lucky-6-roulette", "Mega Roulette": "https://first.ua/ua/igrovie-avtomaty/pragmatic-live/play/mega-roulette"};
function showView(which){
 const series=which==='series';
 document.getElementById('seriesView').classList.toggle('view-hidden',!series);
 document.getElementById('statsView').classList.toggle('view-hidden',series);
 document.getElementById('seriesTab').classList.toggle('active',series);
 document.getElementById('statsTab').classList.toggle('active',!series);
}

const FILTER_KEY='rouletteStatsFiltersV3';
let filterRules=[];
let previousFilterMatches=new Set();
let filterFirstRender=true;

function catMeow(){
 try{
  audioCtx=audioCtx||new (window.AudioContext||window.webkitAudioContext)();
  if(audioCtx.state==='suspended')audioCtx.resume();
  const now=audioCtx.currentTime, o=audioCtx.createOscillator(), g=audioCtx.createGain(), f=audioCtx.createBiquadFilter();
  o.type='sawtooth';f.type='lowpass';f.frequency.value=1250;
  o.frequency.setValueAtTime(760,now);o.frequency.exponentialRampToValueAtTime(430,now+.16);
  o.frequency.exponentialRampToValueAtTime(620,now+.31);o.frequency.exponentialRampToValueAtTime(330,now+.60);
  g.gain.setValueAtTime(.0001,now);g.gain.exponentialRampToValueAtTime(.13,now+.035);
  g.gain.exponentialRampToValueAtTime(.07,now+.30);g.gain.exponentialRampToValueAtTime(.0001,now+.64);
  o.connect(f);f.connect(g);g.connect(audioCtx.destination);o.start(now);o.stop(now+.66);
 }catch(e){}
}
function allMetricOptions(){
 const out=[['ALL','Усі показники']];
 wsGroups.forEach(g=>g.items.forEach(x=>out.push([x[0],x[1]])));
 return out;
}
function ruleTest(rule,metric,v){
 if(rule.metric!=='ALL'&&rule.metric!==metric)return false;
 v=Number(v)||0;const n=Number(rule.value)||0;
 return rule.op==='eq'?v===n:rule.op==='lte'?v<=n:v>=n;
}
function ruleText(rule){
 const found=allMetricOptions().find(x=>x[0]===rule.metric);
 const label=found?found[1]:rule.metric;
 const op=rule.op==='eq'?'=':rule.op==='lte'?'≤':'≥';
 return `${label} ${op} ${rule.value}`;
}
async function syncFiltersToServer(){const r=await fetch('/api/filters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules:filterRules})});if(!r.ok)alert('Не вдалося зберегти правила')}
function saveRules(){localStorage.setItem(FILTER_KEY,JSON.stringify(filterRules));renderRules();updateFilterStatus();syncFiltersToServer()}
function addFilterRule(){
 const rule={id:Date.now().toString(36)+Math.random().toString(36).slice(2,7),metric:filterMetric.value,op:filterOp.value,
 value:Math.max(0,Number(filterValue.value)||0),color:filterColor.value||'#ffcc00'};
 filterRules.push(rule);saveRules();previousFilterMatches=new Set();filterFirstRender=true;renderWinspin();
}
function removeFilterRule(id){
 filterRules=filterRules.filter(r=>r.id!==id);saveRules();previousFilterMatches=new Set();filterFirstRender=true;renderWinspin();
}
function renderRules(){
 if(!document.getElementById('filterRulesList'))return;
 filterRulesList.innerHTML=filterRules.length?filterRules.map(r=>`<div class="ws-rule"><span class="ws-rule-dot" style="background:${r.color}"></span><span>${ruleText(r)}</span><button class="ws-rule-x" onclick="removeFilterRule('${r.id}')" title="Видалити">×</button></div>`).join(''):'<div class="ws-rules-empty">Умов ще немає.</div>';
}
function loadFilter(){
 filterMetric.innerHTML=allMetricOptions().map(x=>`<option value="${x[0]}">${x[1]}</option>`).join('');
 try{const x=JSON.parse(localStorage.getItem(FILTER_KEY)||'[]');if(Array.isArray(x))filterRules=x}catch(e){filterRules=[]}
 renderRules();updateFilterStatus();}
function updateFilterStatus(){
 filterStatus.textContent=filterRules.length?`Активних умов: ${filterRules.length}. Всі відслідковуються одночасно.`:'Немає активних умов.';
}
const wsGroups=[
 {title:'Колір',items:[['Red','Red'],['Black','Black'],['ALT_RB','ALT']]},
 {title:'Парність',items:[['Even','Even'],['Odd','Odd'],['ALT_EO','ALT']]},
 {title:'Малі / великі',items:[['Small','Small'],['Big','Big'],['ALT_SB','ALT']]},
 {title:'Дюжини',items:[['1st','1st'],['2d','2d'],['3d','3d'],['ALT_D','ALT']]},
 {title:'Колони',items:[['1col','1col'],['2col','2col'],['3col','3col'],['ALT_C','ALT']]},
 {title:'Сектори',items:[['TIER','TIER'],['ORPH','ORPH'],['VOIS','VOIS'],['ZERO','ZERO']]},
 {title:'Streets',items:Array.from({length:12},(_,i)=>{const a=i*3+1;return [`S${a}-${a+2}`,`${a}-${a+2}`]})},
 {title:'Six lines',items:Array.from({length:11},(_,i)=>{const a=i*3+1;return [`L${a}-${a+5}`,`${a}-${a+5}`]})},
 {title:'Corners',items:(()=>{const z=[];for(let r=0;r<11;r++){const a=1+r*3;for(const c of [0,1]){const x=a+c;z.push([`C${x}-${x+1}-${x+3}-${x+4}`,`${x}.${x+1}.${x+3}.${x+4}`])}}return z})()},
 {title:'Сусіди',items:(()=>{
   const wheel=[0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26];
   const groups=[];
   for(let i=0;i<wheel.length;i+=3){const nums=wheel.slice(i,i+3);groups.push([`NGR${groups.length+1}`,nums.join('·')]);}
   return groups;
 })()}
];
const WS_COLLAPSE_KEY='rouletteCollapsedGroupsV1';
let collapsedGroups=new Set();
try{const a=JSON.parse(localStorage.getItem(WS_COLLAPSE_KEY)||'null');collapsedGroups=new Set(Array.isArray(a)?a:['Streets','Six lines','Corners','Сусіди'])}catch(e){collapsedGroups=new Set(['Streets','Six lines','Corners','Сусіди'])}
function visibleWsGroups(){return wsGroups.filter(g=>!collapsedGroups.has(g.title))}
function toggleWsGroup(title){if(collapsedGroups.has(title))collapsedGroups.delete(title);else collapsedGroups.add(title);localStorage.setItem(WS_COLLAPSE_KEY,JSON.stringify([...collapsedGroups]));renderGroupToggles();renderWinspin()}
function renderGroupToggles(){
 const el=document.getElementById('wsGroupToggles');if(!el)return;
 const bulky=['Streets','Six lines','Corners','Сусіди'];
 el.innerHTML=bulky.map(t=>`<button class="ws-toggle ${collapsedGroups.has(t)?'off':''}" onclick='toggleWsGroup(${JSON.stringify(t)})'>${collapsedGroups.has(t)?'＋':'−'} ${t}</button>`).join(' ');
}


const RB_SETTINGS_KEY='rouletteBoardSettingsV1';
const RB_THRESHOLDS=[10,20,30,40,50,60,70,80,90,100];
const RB_DEFAULT_COLORS=['#ffd54f','#ffb74d','#ff8a65','#ef5350','#ec407a','#ab47bc','#7e57c2','#5c6bc0','#29b6f6','#26a69a'];
let rbSettings={roulette:rouletteOrder[0]||'',colors:{}};
function rbLoadSettings(){
 try{const x=JSON.parse(localStorage.getItem(RB_SETTINGS_KEY)||'{}');if(x&&typeof x==='object'){if(rouletteOrder.includes(x.roulette))rbSettings.roulette=x.roulette;if(x.colors&&typeof x.colors==='object')rbSettings.colors=x.colors}}catch(e){}
 RB_THRESHOLDS.forEach((n,i)=>{if(!rbSettings.colors[n])rbSettings.colors[n]=RB_DEFAULT_COLORS[i]});
}
function rbSaveSettings(){try{localStorage.setItem(RB_SETTINGS_KEY,JSON.stringify(rbSettings))}catch(e){}}
function rbSetupControls(){
 const sel=document.getElementById('rbRoulette'),colors=document.getElementById('rbColors');if(!sel||!colors)return;
 if(!sel.options.length){sel.innerHTML=rouletteOrder.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');sel.value=rbSettings.roulette;sel.addEventListener('change',()=>{rbSettings.roulette=sel.value;rbSaveSettings();renderRouletteBoard()})}
 if(!colors.children.length){colors.innerHTML=RB_THRESHOLDS.map(n=>`<label class="rb-color-item"><span>${n}+</span><input type="color" data-rb="${n}" value="${rbSettings.colors[n]}"></label>`).join('');colors.querySelectorAll('input').forEach(inp=>inp.addEventListener('input',()=>{rbSettings.colors[inp.dataset.rb]=inp.value;rbSaveSettings();renderRouletteBoard()}))}
}
function rbHighlight(v){v=Number(v)||0;let color='';for(const n of RB_THRESHOLDS){if(v>=n)color=rbSettings.colors[n]}return color}
function renderRouletteBoard(){
 rbSetupControls();const board=document.getElementById('rbBoard');if(!board)return;
 const name=rbSettings.roulette||rouletteOrder[0],r=(payload.winspin||{})[name]||{};
 const red=new Set([1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]);
 let html='';const z=Number(r.NUM0??0),zc=rbHighlight(z);
 html+=`<div class="rb-cell zeronum ${z===0?'rb-hit':''}" style="${zc?`background:${zc};color:#111;text-shadow:none`:''}"><div class="rb-number">0</div><div class="rb-miss">невип. <b>${z}</b></div></div>`;
 for(const start of [3,2,1])for(let n=start;n<=36;n+=3){const v=Number(r[`NUM${n}`]??0),c=rbHighlight(v),base=red.has(n)?'rednum':'blacknum';html+=`<div class="rb-cell ${base} ${v===0?'rb-hit':''}" style="${c?`background:${c};color:#111;text-shadow:none`:''}"><div class="rb-number">${n}</div><div class="rb-miss">невип. <b>${v}</b></div></div>`}
 board.innerHTML=html;
}
rbLoadSettings();
function heatClass(v){v=Number(v)||0;return v>=20?'warn3':v>=12?'warn2':v>=8?'warn1':''}

function metricLabel(metric){
 for(const g of wsGroups){for(const x of g.items){if(x[0]===metric)return `${g.title}: ${x[1]}`}}
 return metric;
}
function openMaxima(name){
 const hist=(payload.maxima||{})[name]||{};
 maxTitle.textContent=`📈 TOP-5 попередніх максимумів — ${name}`;
 const keys=[];
 wsGroups.forEach(g=>g.items.forEach(x=>keys.push(x[0])));
 const cards=keys.map(metric=>{
   const arr=hist[metric]||[];
   const vals=arr.length?arr.map((x,i)=>`${i+1}. <b>${Number(x.value)||0}</b> <span style="color:#8296aa">${esc(x.time||'')}</span>`).join('<br>'):'<span class="max-empty">Ще немає завершених серій</span>';
   return `<div class="max-item"><div class="max-metric">${esc(metricLabel(metric))}</div><div class="max-values">${vals}</div></div>`;
 }).join('');
 maxContent.innerHTML=`<div class="max-grid">${cards}</div>`;
 maxModal.classList.add('open');
}
function closeMaxima(){maxModal.classList.remove('open')}
function renderWinspin(){
 renderGroupToggles();
 let h1='<tr><th rowspan="2">ID</th><th rowspan="2" class="name">Name</th>';
 visibleWsGroups().forEach(g=>h1+=`<th class="ws-group-title sep" colspan="${g.items.length}">${g.title}</th>`);h1+='</tr>';
 let h2='<tr>';visibleWsGroups().forEach(g=>g.items.forEach((x,i)=>h2+=`<th class="${i===0?'sep':''}">${x[1]}</th>`));h2+='</tr>';
 wsHead.innerHTML=h1+h2;

 const currentMatches=new Set();
 wsBody.innerHTML=rouletteOrder.map((name,idx)=>{
  const r=(payload.winspin||{})[name]||{},link=rouletteLinks[name];
  let s=`<tr><td>${idx+1}</td><td class="name">${link?`<a class="ws-name-link" href="${link}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`:esc(name)}<button class="ws-max-btn" onclick='openMaxima(${JSON.stringify(name)})'>📈 TOP-5</button></td>`;
  visibleWsGroups().forEach(g=>g.items.forEach((x,i)=>{
   const metric=x[0],v=Number(r[metric]??0),matches=filterRules.filter(rule=>ruleTest(rule,metric,v));
   matches.forEach(rule=>currentMatches.add(`${rule.id}|${name}|${metric}`));
   const chosen=matches.length?matches[matches.length-1]:null;
   const style=chosen?` style="background:${chosen.color}!important;color:#111;font-weight:900"`:'';
   s+=`<td class="${i===0?'sep ':''}${chosen?'':heatClass(v)}"${style}>${v}</td>`;
  }));
  return s+'</tr>';
 }).join('');
 if(!filterFirstRender){for(const key of currentMatches){if(!previousFilterMatches.has(key)){catMeow();break}}}
 previousFilterMatches=currentMatches;filterFirstRender=false;
}
function render(){
 renderWinspin();
 renderRouletteBoard();
 setupPctButtons();
 grid.innerHTML=rouletteOrder.map(name=>{const s=payload.state[name]||{};return `<div class="roulette"><div class="rhead"><span class="rname">${esc(name)}</span>${rouletteLinks[name]?`<a class="roulette-link" href="${rouletteLinks[name]}" target="_blank" rel="noopener noreferrer">🔗 Відкрити</a>`:""}</div><div class="cols"><div class="section">${activeBox('dozen',s.dozen)}<div class="hist-title">Останні 3 серії кожної дюжини</div>${historyGroups(name,'dozen')}</div><div class="section column-section">${activeBox('column',s.column)}<div class="hist-title">Останні 3 серії кожної колони</div>${historyGroups(name,'column')}</div></div>${rouletteTimeline(name)}${roulettePctStats(name)}</div>`}).join('');
 // Сигнал тільки коли з'явилася НОВА активна серія 5+.
 // Продовження цієї самої серії 5→6→7 не пищить повторно.
 const currentActiveKeys = new Set();
 Object.entries(payload.state).forEach(([roulette,s])=>{
   if(s.dozen && Number(s.dozen.streak)>=5)
     currentActiveKeys.add(`${roulette}|dozen|${s.dozen.number}`);
   if(s.column && Number(s.column.streak)>=5)
     currentActiveKeys.add(`${roulette}|column|${s.column.number}`);
 });
 if(!firstRender){
   for(const key of currentActiveKeys){
     if(!previousActiveKeys.has(key)){
       birdChirp();
       break;
     }
   }
 }
 previousActiveKeys = currentActiveKeys;
 firstRender = false;

 const states=Object.values(payload.state);
 activeCount.textContent=states.reduce((n,s)=>n+(s.dozen?1:0)+(s.column?1:0),0);
 completedCount.textContent=payload.history.length;
 const maxCompleted=payload.history.reduce((m,x)=>Math.max(m,Number(x.streak)||0),0);
 maxStreak.textContent=maxCompleted;
 lastUpdate.textContent=payload.updated||'—';
// Загальна статистика завершених серій: дюжини + колони разом.
 // Серія, що завершилась на 7, рахується лише один раз у "Серія 7".
 const counts={};
 payload.history.forEach(x=>{
   const len=Number(x.streak)||0;
   if(len>=5) counts[len]=(counts[len]||0)+1;
 });
 const top=Math.max(5,maxCompleted);
 let distHtml='';
 for(let len=5;len<=top;len++){
   distHtml+=`<div class="dist-item"><div class="dist-label">Завершені серії</div><div class="dist-length">Серія ${len}</div><div class="dist-count">${counts[len]||0}</div></div>`;
 }
 distribution.innerHTML=distHtml||'<div class="dist-empty">Завершених серій поки немає</div>';
}
async function resetSession(){
 if(!confirm('Почати новий сеанс? Вся накопичена історія завершених серій буде скинута до 0.')) return;
 const btn=document.getElementById('resetBtn');
 btn.disabled=true;
 try{
   const r=await fetch('/api/reset',{method:'POST'});
   if(!r.ok) throw new Error('reset failed');
   previousActiveKeys=new Set();
   firstRender=true;
   await load();
 }catch(e){
   alert('Не вдалося скинути історію.');
 }finally{
   btn.disabled=false;
 }
}
loadFilter();
async function load(){try{const r=await fetch('/api/dashboard?'+Date.now());if(!r.ok)throw Error(r.status);payload=await r.json();filterRules=payload.rules||[];renderRules();updateFilterStatus();render();document.querySelector(".dot").style.background="#16d45b"}catch(e){document.querySelector(".dot").style.background="#ef4444"}}
function tick(){clock.textContent=new Date().toLocaleTimeString('uk-UA')};tick();setInterval(tick,1000);load();setInterval(load,3000);
</script></body></html>""".replace("__ROULETTE_ORDER__", json.dumps(TARGET_ROULETTES, ensure_ascii=False))

class DashboardHandler(BaseHTTPRequestHandler):
    def authorized(self):
        if not DASHBOARD_PASSWORD:
            return True
        expected = "Basic " + base64.b64encode(f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}".encode()).decode()
        if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Roulette", charset="UTF-8"')
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if not self.authorized():
            return
        with state_lock:
            self.handle_get()

    def handle_get(self):
        if self.path.startswith("/api/dashboard"):
            with history_lock:
                body = json.dumps({
                    "state": dashboard_state,
                    "history": series_history,
                    "winspin": build_winspin_stats(),
                    "maxima": stats_max_history,
                    "rules": server_filter_rules,
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        if not self.authorized():
            return
        # Reject cross-site state changes; fetch from our dashboard is same-origin.
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 65536:
            self.send_response(413)
            self.end_headers()
            return
        with state_lock:
            self.handle_post()
            save_checkpoint()

    def handle_post(self):
        if self.path.startswith("/api/filters"):
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
                rules = data.get("rules", [])
                if not isinstance(rules, list):
                    rules = []
                cleaned = []
                for r in rules[:100]:
                    if not isinstance(r, dict):
                        continue
                    cleaned.append({
                        "id": validate_id(r.get("id", "")),
                        "metric": validate_metric(r.get("metric", "ALL")),
                        "op": validate_op(r.get("op", "gte")),
                        "value": validate_threshold(r.get("value", 0)),
                        "color": validate_color(r.get("color", "#ffcc00")),
                    })
                with filter_rules_lock:
                    server_filter_rules[:] = cleaned
                body = json.dumps({"ok": True, "rules": len(cleaned)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if self.path.startswith("/api/reset"):
            global session_generation
            with history_lock:
                series_history.clear()
                save_history()
                for state in dashboard_state.values():
                    state["dozen"] = None
                    state["column"] = None
                    state["last_seen"] = None
                    state["updated"] = datetime.now().strftime("%H:%M:%S")
            reset_stats_max_history()
            with session_lock:
                session_generation += 1
                for name in TARGET_ROULETTES:
                    session_numbers[name] = []
                    session_last_signature[name] = None
            body = json.dumps({"ok": True, "generation": session_generation},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_dashboard():
    try:
        server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"📊 Моніторинг: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
        print("💾 Сеанс відновлюється з DATA_DIR. Новий сеанс — кнопкою на сайті.")
    except OSError:
        raise


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram_message(text: str):
    if bot is None:
        return

    try:

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text
        )

        print("✅ Повідомлення відправлено в Telegram")

    except Exception as e:

        print(
            f"❌ Telegram помилка: {e}"
        )


# ============================================================
# ОСНОВНА ПРОГРАМА
# ============================================================

async def main():

    global last_clear_time

    # Web server and Telegram are started by run_service().


    # Профіль Chrome буде лежати біля Python-файлу
    user_data_dir = os.path.join(
        DATA_DIR,
        "browser_profile"
    )


    async with async_playwright() as p:

        # ====================================================
        # GOOGLE CHROME
        # ====================================================

        print()
        print("🚀 Запускаю Google Chrome...")


        browser = await p.chromium.launch_persistent_context(

            user_data_dir=user_data_dir,

            # Саме встановлений Google Chrome


            headless=True,

            viewport={"width": 1440, "height": 1000},

            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage", "--no-sandbox"
            ]
        )


        # ====================================================
        # СТОРІНКА
        # ====================================================

        if browser.pages:
            page = browser.pages[0]
        else:
            page = await browser.new_page()


        # ====================================================
        # ДІАГНОСТИКА
        # ====================================================

        page.on(
            "pageerror",
            lambda error: print(
                f"🔴 JavaScript error: {error}"
            )
        )


        page.on(
            "requestfailed",
            lambda request: print(
                f"❌ Request failed: "
                f"{request.method} "
                f"{request.url}"
            )
        )


        # ====================================================
        # ВІДКРИВАЄМО САЙТ
        # ====================================================

        print("🌐 Відкриваю сайт...")


        try:

            await page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            print("✅ Сайт відкрито")

        except Exception as e:

            print(
                f"⚠️ Помилка відкриття сайту: {e}"
            )


        # ====================================================
        # ЧЕКАЄМО РУЛЕТКИ
        # ====================================================

        print("⏳ Чекаю блоки рулеток...")


        try:

            await page.wait_for_selector(
                ".live-game__block",
                timeout=60000
            )

            print("✅ Блоки рулеток знайдено")

        except Exception:

            print(
                "⚠️ Блоки поки не знайдені."
            )


        # Додатково даємо сайту завантажитися
        await page.wait_for_timeout(10000)


        # ====================================================
        # РУЧНИЙ СТАРТ
        # ====================================================

        print()
        print("==============================================")
        print("Перевір браузер.")
        print("Дочекайся, поки рулетки повністю завантажаться.")
        print("Якщо потрібно — вибери потрібну вкладку.")
        print("==============================================")
        print()


        # Unattended server startup.


        print()
        print("🚀 АНАЛІЗ ЗАПУЩЕНО")
        print("CTRL+C — зупинити")
        print()


        # ====================================================
        # ОСНОВНИЙ ЦИКЛ
        # ====================================================

        while True:

            try:

                # =================================================
                # ОЧИЩЕННЯ КЕШУ
                # =================================================

                if (
                    datetime.now() - last_clear_time
                    > timedelta(
                        minutes=CLEAR_INTERVAL_MINUTES
                    )
                ):

                    sent_combinations.clear()

                    last_clear_time = datetime.now()

                    print(
                        "🧹 Кеш повідомлень очищено"
                    )


                # =================================================
                # ЗНАХОДИМО ВСІ РУЛЕТКИ
                # =================================================

                roulette_blocks = page.locator(
                    ".live-game__block"
                )

                count = await roulette_blocks.count()


                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                    f"🎰 Знайдено рулеток: {count}"
                )


                # =================================================
                # КОЖНА РУЛЕТКА
                # =================================================

                for i in range(count):

                    block = roulette_blocks.nth(i)


                    # ------------------------------------------------
                    # НАЗВА
                    # ------------------------------------------------

                    try:

                        name = await block.locator(
                            "span.live-game__block__title__text"
                        ).inner_text(
                            timeout=3000
                        )

                        name = name.strip()

                    except Exception:
                        continue


                    # Аналізуємо лише потрібні рулетки
                    if name not in TARGET_ROULETTES:
                        continue


                    # ------------------------------------------------
                    # ЧИСЛА
                    # ------------------------------------------------

                    try:

                        numbers_raw = await block.locator(
                            ".live-game__block__last__roulette "
                            ".roulette-number"
                        ).all_inner_texts()

                    except Exception as e:

                        print(
                            f"⚠️ {name}: "
                            f"помилка читання чисел: {e}"
                        )

                        continue


                    numbers = []


                    for raw in numbers_raw:

                        raw = raw.strip()

                        if not raw:
                            continue


                        cleaned = raw.split()[0]


                        try:

                            number = int(cleaned)

                        except ValueError:

                            continue


                        # Зберігаємо також 0
                        # тому що 0 має ПЕРЕРИВАТИ серію
                        if 0 <= number <= 36:

                            numbers.append(number)


                    if not numbers:

                        print(
                            f"⚠️ {name}: чисел немає"
                        )

                        continue


                    # ------------------------------------------------
                    # ПОКАЗУЄМО ЧИСЛА
                    # ------------------------------------------------

                    print(
                        f"🎯 {name}: "
                        f"{numbers[:10]}"
                    )


                    # =================================================
                    # ДЮЖИНА
                    # =================================================

                    fresh_numbers = get_session_numbers(name, numbers)

                    dozen_streak, dozen_number = get_streak(
                        fresh_numbers,
                        get_dozen
                    )


                    if (
                        dozen_number is not None
                        and dozen_streak >= MIN_DOZEN_STREAK
                    ):

                        # Наприклад:
                        # DOZEN-Speed Roulette 2-2-5

                        combo_key = (
                            f"DOZEN-"
                            f"{name}-"
                            f"{dozen_number}-"
                            f"{dozen_streak}"
                        )


                        if combo_key not in sent_combinations:

                            streak_numbers = fresh_numbers[
                                :dozen_streak
                            ]


                            numbers_text = ", ".join(
                                str(n)
                                for n in streak_numbers
                            )


                            message = (
                                f"🎯 Рулетка: {name}\n\n"
                                f"🔥 ДЮЖИНА {dozen_number}\n"
                                f"Серія: {dozen_streak} разів\n\n"
                                f"Числа:\n"
                                f"{numbers_text}"
                            )


                            await send_telegram_message(
                                message
                            )



                            print()
                            print(
                                "📩 СИГНАЛ ДЮЖИНИ:"
                            )
                            print(message)
                            print()


                            sent_combinations.add(
                                combo_key
                            )


                    # =================================================
                    # КОЛОНА
                    # =================================================

                    column_streak, column_number = get_streak(
                        fresh_numbers,
                        get_column
                    )


                    # Оновлюємо візуальну сторінку: активна серія 5→6→7...
                    # і переносимо її в завершені після переривання.
                    update_dashboard_series(
                        name, fresh_numbers,
                        dozen_streak, dozen_number,
                        column_streak, column_number
                    )


                    if (
                        column_number is not None
                        and column_streak >= MIN_COLUMN_STREAK
                    ):

                        combo_key = (
                            f"COLUMN-"
                            f"{name}-"
                            f"{column_number}-"
                            f"{column_streak}"
                        )


                        if combo_key not in sent_combinations:

                            streak_numbers = fresh_numbers[
                                :column_streak
                            ]


                            numbers_text = ", ".join(
                                str(n)
                                for n in streak_numbers
                            )


                            message = (
                                f"🎯 Рулетка: {name}\n\n"
                                f"🔥 КОЛОНА {column_number}\n"
                                f"Серія: {column_streak} разів\n\n"
                                f"Числа:\n"
                                f"{numbers_text}"
                            )


                            await send_telegram_message(
                                message
                            )



                            print()
                            print(
                                "📩 СИГНАЛ КОЛОНИ:"
                            )
                            print(message)
                            print()


                            sent_combinations.add(
                                combo_key
                            )


                # =================================================
                # СТАТИСТИКА МАКСИМУМІВ + TELEGRAM ПО ФІЛЬТРАХ
                # =================================================
                current_stats = build_winspin_stats()
                save_checkpoint()
                record_completed_metric_streaks(current_stats)
                await process_filter_telegram_signals(current_stats)

                # =================================================
                # 3 СЕКУНДИ
                # =================================================

                await asyncio.sleep(
                    CHECK_INTERVAL
                )


            except KeyboardInterrupt:

                print()
                print("🛑 Програму зупинено")

                break


            except Exception as e:

                print(
                    f"⚠️ Помилка циклу: {e}"
                )

                await asyncio.sleep(5)


        await browser.close()


# ============================================================
# START
CHECKPOINT = os.path.join(DATA_DIR, 'checkpoint.json')

def validate_threshold(value):
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > 100000:
        raise ValueError('Поріг має бути від 0 до 100000')
    return value

def save_checkpoint():
    with state_lock, session_lock, history_lock, stats_history_lock, filter_rules_lock:
        data = dict(numbers=session_numbers, signatures=session_last_signature,
                    state=dashboard_state, history=series_history, maxima=stats_max_history,
                    previous=previous_metric_values, rules=server_filter_rules,
                    active=list(active_filter_signal_keys), sent=list(sent_combinations))
        tmp = CHECKPOINT + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CHECKPOINT)

def restore_checkpoint():
    global active_filter_signal_keys
    if not os.path.exists(CHECKPOINT):
        return
    with open(CHECKPOINT, encoding='utf-8') as f:
        data = json.load(f)
    session_numbers.update(data['numbers'])
    session_last_signature.update({k:tuple(v) if v else None for k,v in data['signatures'].items()})
    dashboard_state.update(data['state'])
    for row in dashboard_state.values():
        if row.get('last_seen'):
            row['last_seen'] = tuple(row['last_seen'])
    series_history[:] = data['history']
    stats_max_history.update(data['maxima'])
    previous_metric_values.update(data['previous'])
    server_filter_rules[:] = data['rules']
    active_filter_signal_keys = set(data.get('active', []))
    sent_combinations.update(data.get('sent', []))

TELEGRAM_METRICS = ['ALL'] + list(build_winspin_stats()[TARGET_ROULETTES[0]])
TELEGRAM_METRICS = [m for m in TELEGRAM_METRICS if m not in ('spins', 'PCT')]

def keyboard(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=key) for label,key in row] for row in rows])

def menu_keyboard():
    rows = [[('＋ Додати правило','add')], [('📋 Мої правила','rules'),('📊 Статус','status')]]
    url = os.getenv('PUBLIC_URL', '')
    if url.startswith('https://'):
        markup = keyboard(rows)
        return InlineKeyboardMarkup(list(markup.inline_keyboard)+[[InlineKeyboardButton('🌐 Відкрити сайт',url=url)]])
    return keyboard(rows)

async def telegram_menu_loop():
    """Only the configured private Telegram chat can control this service."""
    if bot is None:
        return
    from telegram import BotCommand
    pending = {}
    offset = None
    async with Bot(token=TELEGRAM_BOT_TOKEN) as controller:
        await controller.delete_webhook(drop_pending_updates=True)
        await controller.set_my_commands([BotCommand('start','Відкрити меню'),BotCommand('menu','Правила та статус')])
        while True:
            try:
                updates = await controller.get_updates(offset=offset, timeout=20, read_timeout=30)
                for update in updates:
                    offset = update.update_id + 1
                    chat = update.effective_chat
                    if not chat or str(chat.id) != TELEGRAM_CHAT_ID or chat.type != 'private':
                        if update.callback_query:
                            await update.callback_query.answer('Немає доступу')
                        continue
                    query = update.callback_query
                    if query:
                        await query.answer()
                        action = query.data or ''
                        if action == 'add' or action.startswith('page:'):
                            page = int(action.split(':')[1]) if ':' in action else 0
                            metrics = TELEGRAM_METRICS[page*18:(page+1)*18]
                            rows = [[(m, 'metric:'+m)] for m in metrics]
                            nav = []
                            if page: nav.append(('←','page:'+str(page-1)))
                            if (page+1)*18 < len(TELEGRAM_METRICS): nav.append(('→','page:'+str(page+1)))
                            if nav: rows.append(nav)
                            rows.append([('Меню','menu')])
                            await query.edit_message_text('Виберіть показник (ALL — усі):', reply_markup=keyboard(rows))
                        elif action.startswith('metric:'):
                            metric = action.split(':',1)[1]
                            if metric not in TELEGRAM_METRICS: continue
                            pending[chat.id] = {'metric': metric}
                            await query.edit_message_text('Виберіть умову:',reply_markup=keyboard([[('≥','op:gte'),('=','op:eq'),('≤','op:lte')],[('Скасувати','menu')]]))
                        elif action.startswith('op:'):
                            if chat.id not in pending:
                                await controller.send_message(chat.id,'Почніть заново: /menu')
                                continue
                            pending[chat.id]['op'] = action.split(':')[1]
                            await query.edit_message_text('Напишіть поріг числом, наприклад 15. /menu — скасувати.')
                        elif action == 'rules':
                            with filter_rules_lock:
                                rules = [dict(r) for r in server_filter_rules]
                            # Paginated list avoids Telegram message limits.
                            rows = [[(f"✕ {r['metric']} {r['op']} {r['value']:g}", 'delete:'+r['id'])] for r in rules]
                            await query.edit_message_text('Правила для всіх рулеток. Натисніть правило, щоб видалити.' if rules else 'Правил поки немає.',reply_markup=keyboard(rows+[[('Меню','menu')]]))
                        elif action.startswith('delete:'):
                            rid=action.split(':',1)[1]
                            with filter_rules_lock:
                                server_filter_rules[:] = [r for r in server_filter_rules if r['id'] != rid]
                            save_checkpoint()
                            await query.edit_message_text('Правило видалено.',reply_markup=menu_keyboard())
                        elif action == 'status':
                            with session_lock:
                                text='\n'.join(f'{n}: {len(nums)} спінів' for n,nums in session_numbers.items())
                            await query.edit_message_text(text+'\n\nСигнал надходить при вході в умову.',reply_markup=menu_keyboard())
                        else:
                            pending.pop(chat.id,None)
                            await query.edit_message_text('Керування моніторингом',reply_markup=menu_keyboard())
                    elif update.message:
                        text=(update.message.text or '').strip()
                        if text.startswith('/'):
                            pending.pop(chat.id,None)
                            await update.message.reply_text('Керування моніторингом',reply_markup=menu_keyboard())
                        elif chat.id in pending and 'op' in pending[chat.id]:
                            try:
                                value=validate_threshold(text.replace(',','.'))
                            except ValueError:
                                await update.message.reply_text('Введіть число від 0 до 100000.')
                                continue
                            rule=dict(pending[chat.id],value=value,id=uuid.uuid4().hex,color='#ffcc00')
                            with filter_rules_lock:
                                if len(server_filter_rules) >= 100:
                                    await update.message.reply_text('Ліміт 100 правил. Спочатку видаліть зайві.')
                                    continue
                                server_filter_rules.append(rule)
                            pending.pop(chat.id,None)
                            save_checkpoint()
                            await update.message.reply_text('✅ Правило збережено. Воно також з’явиться на сайті.',reply_markup=menu_keyboard())
                        else:
                            await update.message.reply_text('Відкрийте меню: /menu')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'Telegram menu error: {type(exc).__name__}', flush=True)
                await asyncio.sleep(5)

async def monitor_supervisor():
    while True:
        try:
            await main()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f'Monitor restarting: {type(exc).__name__}',flush=True)
        await asyncio.sleep(10)

async def telegram_supervisor():
    if bot is None: return
    while True:
        try:
            await telegram_menu_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f'Telegram restarting: {type(exc).__name__}', flush=True)
        await asyncio.sleep(10)


def validate_metric(value):
    if value not in TELEGRAM_METRICS: raise ValueError('Unknown metric')
    return value

def validate_op(value):
    if value not in ('gte','eq','lte'): raise ValueError('Unknown operator')
    return value

def validate_id(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', value): raise ValueError('Invalid id')
    return value

def validate_color(value):
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', str(value)): raise ValueError('Invalid color')
    return value

async def run_service():
    restore_checkpoint()
    start_dashboard()
    tasks = [asyncio.create_task(monitor_supervisor()), asyncio.create_task(telegram_supervisor())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        save_checkpoint()

# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(run_service())
    except KeyboardInterrupt:
        pass
